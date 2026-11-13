#!/usr/bin/env python3
"""
mock_google.py - a small stand-in for Google's sign-in and Drive APIs, for testing
web/cloud.js in a browser without a Google Cloud project.

    python tests/mock_google.py            # listens on http://localhost:8765

Then, in the browser console on http://localhost:8000:

    localStorage["readalong:cloud-config"] = JSON.stringify({clientId: "test",
        clientSecret: "test", oauthBase: "http://localhost:8765",
        apiBase: "http://localhost:8765"})

The device code is approved automatically after two polls. Files live in memory.
Only the parts of the APIs that cloud.js uses are implemented.
"""

from __future__ import annotations

import itertools
import json
import re
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8765
FILES: dict[str, dict] = {}          # id -> metadata (+ "_data": bytes)
SESSIONS: dict[str, dict] = {}       # resumable upload sessions
POLLS = {"n": 0}
ids = itertools.count(1)
tokens = itertools.count(1)
VALID: set[str] = set()


def new_id(prefix="f") -> str:
    return f"{prefix}{next(ids)}"


def public(meta: dict) -> dict:
    return {k: v for k, v in meta.items() if not k.startswith("_")}


def make(meta: dict, data: bytes = b"") -> dict:
    fid = new_id()
    f = {"id": fid, "name": meta.get("name", "untitled"), "mimeType": meta.get("mimeType", "application/octet-stream"),
         "appProperties": meta.get("appProperties", {}), "parents": meta.get("parents", []),
         "size": str(len(data)), "createdTime": time.time(), "modifiedTime": time.time(), "_data": data}
    FILES[fid] = f
    return f


class H(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        sys.stderr.write("mock-google: " + fmt % args + "\n")

    # -- helpers --
    def cors(self):
        self.send_header("Access-Control-Allow-Origin", self.headers.get("Origin") or "*")
        self.send_header("Access-Control-Allow-Methods", "GET,POST,PUT,PATCH,DELETE,OPTIONS")
        self.send_header("Access-Control-Allow-Headers",
                         "Authorization,Content-Type,Content-Range,X-Upload-Content-Type,X-Upload-Content-Length")
        self.send_header("Access-Control-Expose-Headers", "Location,Range")

    def reply(self, code, body=b"", ctype="application/json", headers=None):
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode()
        self.send_response(code)
        self.cors()
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def body(self) -> bytes:
        n = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(n) if n else b""

    def authed(self) -> bool:
        tok = (self.headers.get("Authorization") or "").removeprefix("Bearer ")
        if tok not in VALID:
            self.reply(401, {"error": {"code": 401, "message": "Invalid Credentials"}})
            return False
        return True

    # -- verbs --
    def do_OPTIONS(self):
        self.reply(204, b"")

    def do_POST(self):
        u = urlparse(self.path)
        qs = parse_qs(u.query)
        if u.path == "/device/code":
            POLLS["n"] = 0
            return self.reply(200, {"device_code": "dev-code", "user_code": "WXYZ-1234",
                                    "verification_url": "https://www.google.com/device",
                                    "expires_in": 1800, "interval": 1})
        if u.path == "/token":
            form = {k: v[0] for k, v in parse_qs(self.body().decode()).items()}
            if form.get("grant_type", "").endswith("device_code"):
                POLLS["n"] += 1
                if POLLS["n"] < 3:
                    return self.reply(428, {"error": "authorization_pending"})
            elif form.get("grant_type") == "refresh_token":
                if form.get("refresh_token") != "refresh-1":
                    return self.reply(400, {"error": "invalid_grant"})
            else:
                return self.reply(400, {"error": "unsupported_grant_type"})
            at = f"access-{next(tokens)}"
            VALID.add(at)
            out = {"access_token": at, "expires_in": 3600, "token_type": "Bearer"}
            if form.get("grant_type", "").endswith("device_code"):
                out["refresh_token"] = "refresh-1"
            return self.reply(200, out)
        if u.path == "/revoke":
            return self.reply(200, {})
        if not self.authed():
            return
        if u.path == "/drive/v3/files":
            f = make(json.loads(self.body() or b"{}"))
            return self.reply(200, {"id": f["id"]})
        if u.path == "/upload/drive/v3/files":
            kind = qs.get("uploadType", [""])[0]
            if kind == "multipart":
                m = re.search(r'boundary=("?)([^";]+)\1', self.headers.get("Content-Type", ""))
                parts = self.body().split(b"--" + m.group(2).encode())
                meta_part, data_part = parts[1], parts[2]
                meta = json.loads(meta_part.split(b"\r\n\r\n", 1)[1].strip())
                data = data_part.split(b"\r\n\r\n", 1)[1]
                data = data[:-2] if data.endswith(b"\r\n") else data
                return self.reply(200, {"id": make(meta, data)["id"]})
            if kind == "resumable":
                sid = new_id("s")
                SESSIONS[sid] = {"meta": json.loads(self.body() or b"{}"), "data": bytearray(),
                                 "total": int(self.headers.get("X-Upload-Content-Length") or 0)}
                return self.reply(200, b"", headers={"Location": f"http://localhost:{PORT}/upload/session/{sid}"})
        self.reply(404, {"error": "not found: " + u.path})

    def do_PUT(self):
        u = urlparse(self.path)
        m = re.match(r"/upload/session/(\w+)$", u.path)
        if not m or m.group(1) not in SESSIONS:
            return self.reply(404, {"error": "no session"})
        s = SESSIONS[m.group(1)]
        rng = self.headers.get("Content-Range", "")
        data = self.body()
        mm = re.match(r"bytes (\d+)-(\d+)/(\d+)", rng)
        if mm:
            start = int(mm.group(1))
            if start != len(s["data"]):
                return self.reply(400, {"error": f"expected offset {len(s['data'])}, got {start}"})
            s["data"] += data
        if len(s["data"]) >= s["total"]:
            f = make(s["meta"], bytes(s["data"]))
            del SESSIONS[m.group(1)]
            return self.reply(200, {"id": f["id"]})
        hdr = {"Range": f"bytes=0-{len(s['data']) - 1}"} if s["data"] else {}
        return self.reply(308, b"", headers=hdr)

    def do_DELETE(self):
        u = urlparse(self.path)
        if not self.authed():
            return
        m = re.match(r"/drive/v3/files/(\w+)$", u.path)
        if not m or FILES.pop(m.group(1), None) is None:
            return self.reply(404, {"error": "not found"})
        return self.reply(204, b"")

    def do_PATCH(self):
        u = urlparse(self.path)
        if not self.authed():
            return
        m = re.match(r"/drive/v3/files/(\w+)$", u.path)
        if m:                                        # metadata update; null appProperties are removed
            f = FILES.get(m.group(1))
            if not f:
                return self.reply(404, {"error": "not found"})
            meta = json.loads(self.body() or b"{}")
            for k, v in (meta.pop("appProperties", None) or {}).items():
                if v is None:
                    f["appProperties"].pop(k, None)
                else:
                    f["appProperties"][k] = str(v)
            f.update({k: v for k, v in meta.items() if k in ("name", "trashed")})
            f["modifiedTime"] = time.time()
            return self.reply(200, public(f))
        m = re.match(r"/upload/drive/v3/files/(\w+)$", u.path)
        if not m or m.group(1) not in FILES:
            return self.reply(404, {"error": "not found"})
        f = FILES[m.group(1)]
        f["_data"] = self.body()
        f["size"] = str(len(f["_data"]))
        f["modifiedTime"] = time.time()
        return self.reply(200, {"id": f["id"]})

    def do_GET(self):
        u = urlparse(self.path)
        qs = parse_qs(u.query)
        if not self.authed():
            return
        if u.path == "/drive/v3/files":
            q = qs.get("q", [""])[0]
            out = [f for f in FILES.values() if not ("trashed=false" in q and f.get("trashed"))]
            for key, val in re.findall(r"key='(\w+)' and value='(\w+)'", q):
                out = [f for f in out if f["appProperties"].get(key) == val]
            mt = re.search(r"mimeType='([^']+)'", q)
            if mt:
                out = [f for f in out if f["mimeType"] == mt.group(1)]
            out.sort(key=lambda f: -f["modifiedTime"])
            return self.reply(200, {"files": [public(f) for f in out]})
        m = re.match(r"/drive/v3/files/(\w+)$", u.path)
        if m and m.group(1) in FILES:
            f = FILES[m.group(1)]
            if qs.get("alt", [""])[0] == "media":
                return self.reply(200, f["_data"], ctype=f["mimeType"])
            return self.reply(200, public(f))
        self.reply(404, {"error": "not found: " + u.path})


if __name__ == "__main__":
    print(f"mock Google APIs on http://localhost:{PORT}")
    ThreadingHTTPServer(("127.0.0.1", PORT), H).serve_forever()
