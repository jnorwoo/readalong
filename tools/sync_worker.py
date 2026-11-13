#!/usr/bin/env python3
"""
sync_worker.py - make sync files on this PC for books added from the web app.

In the web app (phone or computer), "Add a book" with an audiobook + its ebook
uploads both to your Google Drive and queues a sync job. This helper watches
for those jobs, downloads the files, runs book_sync.py (WhisperX, on your GPU),
and puts the finished book into your Drive library. Progress shows in the web app.

    python tools/sync_worker.py          # keep running and check every 20 seconds
    python tools/sync_worker.py --once   # do any waiting jobs, then exit

First run asks for the Google client ID and secret (the same ones as in
docs/google-drive.md) and shows a code to enter at google.com/device. They're
saved in ~/.readalong/worker.json, outside the repo.

Only the standard library is used here; book_sync.py needs whisperx and ffmpeg.
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCOPE = "https://www.googleapis.com/auth/drive.file"
DEFAULT_CONFIG = Path.home() / ".readalong" / "worker.json"
WORK_DIR = Path.home() / ".readalong" / "jobs"
PYTHON = sys.executable      # the Python that runs book_sync.py; set by sync_python() at startup


def log(msg: str) -> None:
    print(time.strftime("%H:%M:%S ") + msg, flush=True)


def clip(s: str, limit: int = 100) -> str:
    """appProperties allow 124 bytes per key + value."""
    s = " ".join(str(s or "").split())
    while len(s.encode("utf-8")) > limit:
        s = s[:-1]
    return s


# --------------------------------------------------------------------------
# Google sign-in (device code) and a tiny Drive client
# --------------------------------------------------------------------------

class SignedOut(Exception):
    pass


class Drive:
    def __init__(self, config_path: Path):
        self.path = config_path
        self.cfg = json.loads(config_path.read_text()) if config_path.exists() else {}
        self.oauth = self.cfg.get("oauth_base", "https://oauth2.googleapis.com")
        self.apibase = self.cfg.get("api_base", "https://www.googleapis.com")
        self.access, self.expires = None, 0.0

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.cfg, indent=2))
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass

    # -- auth --
    def _post_form(self, url: str, form: dict) -> tuple[int, dict]:
        req = urllib.request.Request(url, data=urllib.parse.urlencode(form).encode(), method="POST")
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return r.status, json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            try:
                return e.code, json.loads(e.read() or b"{}")
            except ValueError:
                return e.code, {}

    def setup(self) -> None:
        if not self.cfg.get("client_id"):
            print("Enter the Google client ID and secret from your Google Cloud project")
            print("(Google Auth Platform -> Clients; see docs/google-drive.md).")
            self.cfg["client_id"] = input("Client ID: ").strip()
            self.cfg["client_secret"] = getpass.getpass("Client secret (hidden): ").strip()
            self.save()
        if not self.cfg.get("refresh_token"):
            self.sign_in()

    def sign_in(self) -> None:
        code, d = self._post_form(self.oauth + "/device/code", {"client_id": self.cfg["client_id"], "scope": SCOPE})
        if code != 200:
            sys.exit(f"error: Google sign-in failed: {d.get('error_description') or d.get('error') or code}")
        url = d.get("verification_url") or d.get("verification_uri") or "https://www.google.com/device"
        print(f"\nTo connect this PC to your Google Drive, go to {url}\nand enter the code:  {d['user_code']}\n")
        interval = d.get("interval", 5)
        deadline = time.time() + d.get("expires_in", 1800)
        while time.time() < deadline:
            time.sleep(interval)
            code, t = self._post_form(self.oauth + "/token", {
                "client_id": self.cfg["client_id"], "client_secret": self.cfg.get("client_secret", ""),
                "device_code": d["device_code"], "grant_type": "urn:ietf:params:oauth:grant-type:device_code"})
            if code == 200:
                self.cfg["refresh_token"] = t["refresh_token"]
                self.access, self.expires = t["access_token"], time.time() + t.get("expires_in", 3600)
                self.save()
                log("connected to Google Drive.")
                return
            err = t.get("error")
            if err == "authorization_pending":
                continue
            if err == "slow_down":
                interval += 5
                continue
            sys.exit(f"error: sign-in failed: {t.get('error_description') or err}")
        sys.exit("error: the code expired. Run again for a new one.")

    def token(self) -> str:
        if self.access and time.time() < self.expires - 60:
            return self.access
        code, t = self._post_form(self.oauth + "/token", {
            "client_id": self.cfg["client_id"], "client_secret": self.cfg.get("client_secret", ""),
            "refresh_token": self.cfg.get("refresh_token", ""), "grant_type": "refresh_token"})
        if code != 200:
            if t.get("error") == "invalid_grant":
                self.cfg.pop("refresh_token", None)
                self.save()
                raise SignedOut()
            raise RuntimeError(f"couldn't refresh Google sign-in: {t.get('error_description') or t.get('error') or code}")
        self.access, self.expires = t["access_token"], time.time() + t.get("expires_in", 3600)
        return self.access

    # -- API --
    def request(self, method: str, path: str, params: dict | None = None, body: bytes | None = None,
                headers: dict | None = None, stream_to: Path | None = None, progress=None, retry: bool = True):
        url = self.apibase + path + ("?" + urllib.parse.urlencode(params) if params else "")
        h = {"Authorization": "Bearer " + self.token(), **(headers or {})}
        req = urllib.request.Request(url, data=body, method=method, headers=h)
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                if stream_to is None:
                    raw = r.read()
                    return json.loads(raw) if raw else {}
                total, got = int(r.headers.get("Content-Length") or 0), 0
                with open(stream_to, "wb") as f:
                    while chunk := r.read(1 << 20):
                        f.write(chunk)
                        got += len(chunk)
                        if progress:
                            progress(got, total)
                return {}
        except urllib.error.HTTPError as e:
            if e.code == 401 and retry:
                self.access = None
                return self.request(method, path, params, body, headers, stream_to, progress, retry=False)
            raise RuntimeError(f"Google Drive error {e.code}: {e.read()[:200]!r}") from None

    def find(self, query: str, fields: str = "id,name,size,createdTime,appProperties") -> list[dict]:
        return self.request("GET", "/drive/v3/files", {
            "q": query + " and trashed=false", "fields": f"files({fields})", "pageSize": "1000"}).get("files", [])

    def meta(self, file_id: str, fields: str = "id,name,size,mimeType") -> dict:
        return self.request("GET", f"/drive/v3/files/{file_id}", {"fields": fields})

    def patch(self, file_id: str, meta: dict) -> None:
        self.request("PATCH", f"/drive/v3/files/{file_id}", body=json.dumps(meta).encode(),
                     headers={"Content-Type": "application/json"})

    def download(self, file_id: str, dest: Path, progress=None) -> None:
        size = int(self.meta(file_id).get("size") or 0)
        self.request("GET", f"/drive/v3/files/{file_id}", {"alt": "media"}, stream_to=dest,
                     progress=(lambda got, total: progress(got, total or size)) if progress else None)

    def read_json(self, file_id: str) -> dict:
        return self.request("GET", f"/drive/v3/files/{file_id}", {"alt": "media"})

    def create(self, meta: dict, data: bytes, mime: str) -> str:
        b = "readalong" + uuid.uuid4().hex
        body = (f"--{b}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n{json.dumps(meta)}\r\n"
                f"--{b}\r\nContent-Type: {mime}\r\n\r\n").encode() + data + f"\r\n--{b}--\r\n".encode()
        return self.request("POST", "/upload/drive/v3/files", {"uploadType": "multipart", "fields": "id"},
                            body=body, headers={"Content-Type": f"multipart/related; boundary={b}"})["id"]


def sync_python() -> str:
    """The Python that has WhisperX: this one, or the project's .venv if this one doesn't.

    The helper itself only needs the standard library, so it may well be started
    with the system Python while WhisperX lives in the project's virtual environment.
    """
    venv = ROOT / ".venv"
    candidates = [sys.executable, str(venv / "Scripts" / "python.exe"), str(venv / "bin" / "python")]
    for py in candidates:
        if not Path(py).exists():
            continue
        ok = subprocess.run([py, "-c", "import importlib.util, sys; "
                             "sys.exit(0 if importlib.util.find_spec('whisperx') else 1)"],
                            capture_output=True).returncode == 0
        if ok:
            return py
    sys.exit("error: WhisperX isn't installed for this Python, and there's no .venv in the readalong "
             "folder that has it. Install it (see README: pip install -r requirements.txt), or start "
             "this helper with the Python that has it, e.g. .venv\\Scripts\\python tools\\sync_worker.py")


def tagged(v: str) -> str:
    return f"appProperties has {{ key='ra' and value='{v}' }}"


# --------------------------------------------------------------------------
# Jobs
# --------------------------------------------------------------------------

CHUNK_LINE = re.compile(r"\[file (\d+)/(\d+) chunk (\d+)/(\d+)\]")


class Job:
    """Reports progress to the job's appProperties, at most every few seconds."""

    def __init__(self, drive: Drive, f: dict):
        self.drive, self.id = drive, f["id"]
        self.props = f.get("appProperties") or {}
        self.last = 0.0

    def report(self, stage: str, progress: float, force: bool = False, **extra) -> None:
        if not force and time.time() - self.last < 5:
            return
        self.last = time.time()
        props = {"stage": clip(stage), "progress": str(int(max(0, min(100, progress)))), **extra}
        try:
            self.drive.patch(self.id, {"appProperties": props})
        except Exception as e:  # progress is best effort; the job carries on
            log(f"  (couldn't update progress: {e})")


def run_job(drive: Drive, f: dict, worker: str, sync_args: list[str]) -> None:
    job = Job(drive, f)
    spec = drive.read_json(f["id"])
    title = spec.get("title") or "book"
    log(f"job: {title}")
    job.report("Starting on " + worker, 1, force=True, status="working", worker=clip(worker, 60), error=None)
    work = WORK_DIR / f["id"]
    work.mkdir(parents=True, exist_ok=True)
    audio = work / ("audio" + (Path(spec.get("audioName", "")).suffix.lower() or ".m4b"))
    ebook = work / ("book" + (Path(spec.get("ebookName", "")).suffix.lower() or ".epub"))

    # 1. Download (0-10%). Skipped if a previous run already got the files. The ebook
    #    comes first and is checked before the (much larger) audiobook is fetched.
    for src, dest, lo, hi, what in [(spec["ebook"], ebook, 0, 1, "ebook"), (spec["audio"], audio, 1, 10, "audiobook")]:
        size = int(drive.meta(src).get("size") or 0)
        if not (dest.exists() and size and dest.stat().st_size == size):
            log(f"  downloading {what}...")
            drive.download(src, dest, lambda got, total, lo=lo, hi=hi, what=what: job.report(
                f"Downloading {what} to your PC", lo + (hi - lo) * got / max(1, total)))
        if what == "ebook":
            check = subprocess.run(
                [PYTHON, "-c", "import sys; sys.path.insert(0, 'tools'); import book_sync as b; "
                 "from pathlib import Path; b.load_book(Path(sys.argv[1]))", str(ebook)],
                cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace")
            if check.returncode != 0:
                lines = [l for l in (check.stderr or check.stdout).splitlines() if l.strip()]
                raise RuntimeError(lines[-1] if lines else "couldn't read the ebook")

    # 2. Sync (10-95%): run book_sync.py and follow its log.
    job.report("Transcribing (starting WhisperX)", 10, force=True)
    out = work / "book"
    cmd = [PYTHON, "-u", str(ROOT / "tools" / "book_sync.py"), str(audio), "--book", str(ebook),
           "--out", str(out), "--cache-dir", str(work / "cache")]
    if spec.get("language"):
        cmd += ["--language", spec["language"]]
    cmd += sync_args
    tail: list[str] = []
    proc = subprocess.Popen(cmd, cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, encoding="utf-8", errors="replace")
    for line in proc.stdout:
        line = line.rstrip()
        if not line.strip():
            continue
        tail = (tail + [line])[-30:]
        m = CHUNK_LINE.search(line)
        if m:
            fi, fn, ci, cn = map(int, m.groups())
            done = ((fi - 1) + (ci - 1) / cn) / fn
            log("  " + line.strip())
            job.report(f"Transcribing: part {ci} of {cn}", 10 + 75 * done, force=True)
        elif line.startswith("transcript:"):
            log("  " + line.strip())
            job.report("Lining up the text with the audio", 87, force=True)
        elif line.startswith(("matched", "warning", "error")):
            log("  " + line.strip())
    if proc.wait() != 0:
        msg = next((l for l in reversed(tail) if l.lower().startswith("error")), tail[-1] if tail else "sync failed")
        raise RuntimeError(msg)

    # 3. Add the book to the library (95-100%). The audio is already in Drive.
    job.report("Adding to your library", 95, force=True)
    sync_path = out.parent / f"{out.name}.sync.json"
    data = json.loads(sync_path.read_text(encoding="utf-8"))
    data["title"] = title
    data["audio"] = spec.get("audioName") or data.get("audio")
    folder = drive.find(tagged("root") + " and mimeType='application/vnd.google-apps.folder'", "id")
    book_id = drive.create({
        "name": Path(spec.get("audioName") or "book").stem + ".sync.json",
        "parents": [folder[0]["id"]] if folder else [],
        "mimeType": "application/json",
        "appProperties": {"ra": "book", "audio": spec["audio"], "title": clip(title, 110),
                          "author": clip(spec.get("author", ""), 110),
                          "duration": str(round(data.get("duration") or 0))},
    }, json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8"), "application/json")
    job.report("Ready in your library", 100, force=True, status="done", book=book_id)
    shutil.rmtree(work, ignore_errors=True)
    log(f"  done: '{title}' is in your library.")


def heartbeat(drive: Drive, worker: str) -> None:
    """Lets the web app show that this PC is ready for jobs."""
    files = drive.find(tagged("worker"), "id")
    props = {"ra": "worker", "seen": str(int(time.time() * 1000)), "name": clip(worker, 60)}
    if files:
        drive.patch(files[0]["id"], {"appProperties": props})
    else:
        folder = drive.find(tagged("root") + " and mimeType='application/vnd.google-apps.folder'", "id")
        drive.create({"name": "sync helper.json", "parents": [folder[0]["id"]] if folder else [],
                      "mimeType": "application/json", "appProperties": props}, b"{}", "application/json")


def pending_jobs(drive: Drive, worker: str) -> list[dict]:
    queued = drive.find(tagged("job") + " and appProperties has { key='status' and value='queued' }")
    # Jobs this PC started but didn't finish (stopped or crashed) are picked up again.
    mine = [f for f in drive.find(tagged("job") + " and appProperties has { key='status' and value='working' }")
            if (f.get("appProperties") or {}).get("worker") == clip(worker, 60)]
    return sorted(mine + queued, key=lambda f: str(f.get("createdTime", "")))


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description="Make sync files for books queued in the web app.")
    ap.add_argument("--once", action="store_true", help="do waiting jobs, then exit")
    ap.add_argument("--interval", type=float, default=20, help="seconds between checks (default 20)")
    ap.add_argument("--config", type=Path, default=DEFAULT_CONFIG, help=f"settings file (default {DEFAULT_CONFIG})")
    ap.add_argument("--sync-args", default="", help='extra book_sync.py options, e.g. "--model medium"')
    args = ap.parse_args(argv)

    global PYTHON
    PYTHON = sync_python()   # check before signing in, so a missing WhisperX shows up at once
    drive = Drive(args.config)
    drive.setup()
    worker = socket.gethostname()
    sync_args = args.sync_args.split()
    if PYTHON != sys.executable:
        log(f"using {PYTHON} for syncing (it has WhisperX).")
    log(f"sync helper on {worker} is watching your Google Drive for books to sync. Ctrl+C to stop.")
    while True:
        try:
            heartbeat(drive, worker)
            for f in pending_jobs(drive, worker):
                try:
                    run_job(drive, f, worker, sync_args)
                except Exception as e:
                    msg = re.sub(r"^error:\s*", "", str(e))
                    log(f"  failed: {msg}")
                    Job(drive, f).report("Failed", 0, force=True, status="error", error=clip(msg, 115))
                heartbeat(drive, worker)
        except SignedOut:
            log("Google sign-in expired; signing in again.")
            drive.sign_in()
            continue
        except (urllib.error.URLError, RuntimeError, OSError) as e:
            log(f"couldn't reach Google Drive ({e}); trying again shortly.")
        if args.once:
            break
        time.sleep(args.interval)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print()
