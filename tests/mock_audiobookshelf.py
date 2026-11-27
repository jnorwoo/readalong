#!/usr/bin/env python3
r"""
A fake Audiobookshelf server for testing web/abs.js and sync_worker.py without a real one.

    python tests/mock_audiobookshelf.py --library "C:\path\to\books"

Each folder inside --library becomes a book: its audio files (in number order) are the
tracks and an .epub/.pdf/.txt is its ebook. Served at http://localhost:8766/audiobookshelf
(the base path, like a server behind a reverse proxy). The API key is "test-key".

Only the parts Read-along uses are imitated, including Audiobookshelf's CORS behaviour:
requests from an origin in --allow (default http://localhost:8000) get
Access-Control-Allow-Origin, others don't.

To imitate the Audiobookshelf phone app playing a book (for follow mode):

    POST /mock/play   {"item": "<id>", "pos": 120, "rate": 1.25, "every": 10, "clock": 0}
    POST /mock/pause

While "playing", the book's progress is reported every `every` seconds, like the app.
"clock" sets the server's clock that many seconds ahead of this computer's.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

BASE = "/audiobookshelf"
TOKEN = "test-key"
AUDIO_EXT = {".mp3": "audio/mpeg", ".m4b": "audio/mp4", ".m4a": "audio/mp4", ".ogg": "audio/ogg", ".flac": "audio/flac"}
EBOOK_EXT = {".epub": "epub", ".pdf": "pdf", ".txt": "txt"}

LOCK = threading.Lock()
ITEMS: dict[str, dict] = {}
PROGRESS: dict[str, dict] = {}
PLAYING: dict = {}
ALLOW: list[str] = []
CLOCK = {"offset": 0.0}   # seconds the server's clock is ahead (POST /mock/play {"clock": 30})


def natural_key(p: Path):
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", p.name)]


def duration(p: Path) -> float:
    if not shutil.which("ffprobe"):
        return 60.0
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of",
                          "default=noprint_wrappers=1:nokey=1", str(p)], capture_output=True, text=True).stdout
    try:
        return float(out.strip())
    except ValueError:
        return 60.0


def load_library(root: Path) -> None:
    for n, d in enumerate(sorted((x for x in root.iterdir() if x.is_dir()), key=natural_key)):
        audio = sorted((p for p in d.iterdir() if p.suffix.lower() in AUDIO_EXT), key=natural_key)
        if not audio:
            continue
        ebook = next((p for p in d.iterdir() if p.suffix.lower() in EBOOK_EXT), None)
        author, _, title = d.name.partition(" - ")
        if not title:
            author, title = "", d.name
        item_id = str(uuid.uuid5(uuid.NAMESPACE_URL, str(d.resolve())))
        tracks, start, files = [], 0.0, {}
        for i, p in enumerate(audio):
            ino = str(1000 + i)
            dur = duration(p)
            files[ino] = p
            tracks.append({"index": i + 1, "ino": ino, "duration": dur, "startOffset": start, "title": p.name,
                           "contentUrl": f"/api/items/{item_id}/file/{ino}", "mimeType": AUDIO_EXT[p.suffix.lower()],
                           "metadata": {"filename": p.name, "ext": p.suffix.lower(), "size": p.stat().st_size}})
            start += dur
        ebook_file = None
        if ebook:
            files["9999"] = ebook
            ebook_file = {"ino": "9999", "ebookFormat": EBOOK_EXT[ebook.suffix.lower()],
                          "metadata": {"filename": ebook.name, "ext": ebook.suffix.lower(), "size": ebook.stat().st_size}}
        ITEMS[item_id] = {"id": item_id, "title": title.strip(), "author": author.strip(), "duration": start,
                          "tracks": tracks, "ebookFile": ebook_file, "files": files}
        print(f"  book {n + 1}: {title.strip()} ({len(tracks)} track(s){', ebook' if ebook else ''})  id {item_id}")


def minified(it: dict) -> dict:
    return {"id": it["id"], "mediaType": "book", "media": {
        "metadata": {"title": it["title"], "authorName": it["author"]},
        "numTracks": len(it["tracks"]), "numAudioFiles": len(it["tracks"]), "duration": it["duration"],
        "ebookFormat": it["ebookFile"]["ebookFormat"] if it["ebookFile"] else None}}


def expanded(it: dict) -> dict:
    m = minified(it)
    m["media"].update({"tracks": it["tracks"], "audioFiles": it["tracks"], "ebookFile": it["ebookFile"], "chapters": []})
    return m


def set_progress(item: str, pos: float) -> None:
    it = ITEMS.get(item)
    dur = it["duration"] if it else 0
    PROGRESS[item] = {"id": "p-" + item, "libraryItemId": item, "episodeId": None, "duration": dur,
                      "progress": min(1, pos / dur) if dur else 0, "currentTime": pos,
                      "isFinished": bool(dur and dur - pos < 10), "lastUpdate": int((time.time() + CLOCK["offset"]) * 1000),
                      "startedAt": int(time.time() * 1000)}


def player_thread() -> None:
    """Imitates the Audiobookshelf app reporting progress while it plays."""
    while True:
        time.sleep(0.2)
        with LOCK:
            if not PLAYING:
                continue
            now = time.time()
            if now - PLAYING["last"] >= PLAYING["every"]:
                pos = PLAYING["pos"] + (now - PLAYING["t0"]) * PLAYING["rate"]
                set_progress(PLAYING["item"], pos)
                PLAYING["last"] = now


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def cors(self):
        origin = self.headers.get("Origin")
        if origin and origin in ALLOW:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Access-Control-Allow-Methods", "GET, POST, PATCH, PUT, DELETE, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "*")
            self.send_header("Access-Control-Allow-Credentials", "true")

    def reply(self, code: int, body=b"", ctype="application/json", headers=None):
        if not isinstance(body, (bytes, bytearray)):
            body = json.dumps(body).encode()
        self.send_response(code)
        self.cors()
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def authed(self, q) -> bool:
        tok = (q.get("token") or [""])[0]
        auth = self.headers.get("Authorization", "")
        return tok == TOKEN or auth == "Bearer " + TOKEN

    def send_file(self, path: Path, ctype: str):
        size = path.stat().st_size
        rng = self.headers.get("Range")
        start, end = 0, size - 1
        if rng and (m := re.match(r"bytes=(\d*)-(\d*)", rng)):
            if m.group(1):
                start = int(m.group(1))
                end = int(m.group(2)) if m.group(2) else size - 1
            else:
                start = size - int(m.group(2))
        end = min(end, size - 1)
        self.send_response(206 if rng else 200)
        self.cors()
        self.send_header("Content-Type", ctype)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(end - start + 1))
        if rng:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        if self.command == "HEAD":
            return
        with open(path, "rb") as f:
            f.seek(start)
            left = end - start + 1
            try:
                while left > 0:
                    chunk = f.read(min(1 << 16, left))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    left -= len(chunk)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                pass

    def do_OPTIONS(self):
        self.send_response(200 if self.headers.get("Origin") in ALLOW else 404)
        self.cors()
        self.send_header("Content-Length", "0")
        self.end_headers()

    def body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        try:
            return json.loads(self.rfile.read(n) or b"{}")
        except ValueError:
            return {}

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        if not u.path.startswith(BASE):
            return self.reply(404, {"error": "not found"})
        path = u.path[len(BASE):] or "/"
        if path == "/status":
            return self.reply(200, {"app": "audiobookshelf", "serverVersion": "2.38.0", "isInit": True})
        if not path.startswith("/api/"):
            return self.reply(200, b"<html>Audiobookshelf (mock)</html>", "text/html")
        if not self.authed(q):
            return self.reply(401, b"Unauthorized", "text/plain")
        with LOCK:
            if path == "/api/me":
                return self.reply(200, {"id": "u1", "username": "tester", "mediaProgress": list(PROGRESS.values())})
            if path == "/api/libraries":
                return self.reply(200, {"libraries": [{"id": "lib1", "name": "Audiobooks", "mediaType": "book"}]})
            if path == "/api/libraries/lib1/items":
                limit = int((q.get("limit") or ["0"])[0] or 0)
                page = int((q.get("page") or ["0"])[0] or 0)
                items = sorted(ITEMS.values(), key=lambda it: it["title"].lower())
                if limit:
                    items = items[page * limit:(page + 1) * limit]
                return self.reply(200, {"results": [minified(it) for it in items], "total": len(ITEMS)})
            if m := re.fullmatch(r"/api/me/progress/([^/]+)", path):
                p = PROGRESS.get(m.group(1))
                return self.reply(200, p) if p else self.reply(404, b"Not Found", "text/plain")
            m = re.fullmatch(r"/api/items/([^/]+)(/.*)?", path)
            it = ITEMS.get(m.group(1)) if m else None
        if not it:
            return self.reply(404, b"Not Found", "text/plain")
        rest = m.group(2) or ""
        if rest == "":
            return self.reply(200, expanded(it))
        if fm := re.fullmatch(r"/file/(\d+)", rest):
            f = it["files"].get(fm.group(1))
            if not f:
                return self.reply(404, b"Not Found", "text/plain")
            return self.send_file(f, AUDIO_EXT.get(f.suffix.lower(), "application/octet-stream"))
        if rest == "/ebook" and it["ebookFile"]:
            return self.send_file(it["files"]["9999"], "application/epub+zip")
        return self.reply(404, b"Not Found", "text/plain")

    def do_PATCH(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        path = u.path[len(BASE):]
        if not self.authed(q):
            return self.reply(401, b"Unauthorized", "text/plain")
        if m := re.fullmatch(r"/api/me/progress/([^/]+)", path):
            b = self.body()
            with LOCK:
                if m.group(1) not in ITEMS:
                    return self.reply(404, b"Not Found", "text/plain")
                set_progress(m.group(1), float(b.get("currentTime") or 0))
            return self.reply(200, b"OK", "text/plain")
        return self.reply(404, b"Not Found", "text/plain")

    def do_POST(self):
        u = urlparse(self.path)
        b = self.body()
        with LOCK:
            if u.path == "/mock/play":
                item = b.get("item") or next(iter(ITEMS))
                pos = float(b["pos"]) if "pos" in b else (PROGRESS.get(item) or {}).get("currentTime", 0)
                CLOCK["offset"] = float(b.get("clock") or 0)
                PLAYING.clear()
                PLAYING.update({"item": item, "pos": pos, "t0": time.time(), "rate": float(b.get("rate") or 1),
                                "every": float(b.get("every") or 10), "last": time.time()})
                set_progress(item, pos)
                return self.reply(200, {"ok": True, "item": item})
            if u.path == "/mock/pause":
                if PLAYING:
                    set_progress(PLAYING["item"], PLAYING["pos"] + (time.time() - PLAYING["t0"]) * PLAYING["rate"])
                PLAYING.clear()
                return self.reply(200, {"ok": True})
        return self.reply(404, b"Not Found", "text/plain")


def main() -> None:
    ap = argparse.ArgumentParser(description="Fake Audiobookshelf server for testing")
    ap.add_argument("--library", type=Path, required=True, help="folder of book folders (audio files + ebook each)")
    ap.add_argument("--port", type=int, default=8766)
    ap.add_argument("--allow", action="append", help="allowed CORS origin (default http://localhost:8000)")
    args = ap.parse_args()
    ALLOW.extend(args.allow or ["http://localhost:8000"])
    load_library(args.library)
    threading.Thread(target=player_thread, daemon=True).start()
    print(f"mock Audiobookshelf on http://localhost:{args.port}{BASE}  (API key: {TOKEN})")
    ThreadingHTTPServer(("", args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
