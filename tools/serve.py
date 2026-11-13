#!/usr/bin/env python3
"""
serve.py - serve web/ locally, with the range requests audio seeking needs.

  python tools/serve.py            # http://localhost:8000
  python tools/serve.py --lan      # also reachable from other devices on your Wi-Fi

Desktop/phone player: /index.html     Glasses app: /glasses.html
"""

from __future__ import annotations

import argparse
import os
import re
import socket
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

WEB = Path(__file__).resolve().parent.parent / "web"


class RangeHandler(SimpleHTTPRequestHandler):
    extensions_map = {**SimpleHTTPRequestHandler.extensions_map,
                      ".m4b": "audio/mp4", ".m4a": "audio/mp4", ".opus": "audio/ogg",
                      ".json": "application/json", ".webmanifest": "application/manifest+json"}

    def end_headers(self):
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Cache-Control", "no-cache")
        super().end_headers()

    def send_head(self):
        rng = self.headers.get("Range")
        path = self.translate_path(self.path)
        if not rng or not os.path.isfile(path):
            return super().send_head()
        m = re.fullmatch(r"bytes=(\d*)-(\d*)", rng.strip())
        size = os.path.getsize(path)
        if not m or (m.group(1) == "" and m.group(2) == ""):
            self.send_error(416)
            return None
        if m.group(1) == "":
            start, end = max(0, size - int(m.group(2))), size - 1
        else:
            start = int(m.group(1))
            end = min(int(m.group(2)) if m.group(2) else size - 1, size - 1)
        if start > end or start >= size:
            self.send_response(416)
            self.send_header("Content-Range", f"bytes */{size}")
            self.end_headers()
            return None
        f = open(path, "rb")
        f.seek(start)
        self.send_response(206)
        self.send_header("Content-Type", self.guess_type(path))
        self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.send_header("Content-Length", str(end - start + 1))
        self.end_headers()
        self._range_left = end - start + 1
        return f

    def copyfile(self, source, outputfile):
        left = getattr(self, "_range_left", None)
        if left is None:
            return super().copyfile(source, outputfile)
        while left > 0:
            chunk = source.read(min(1 << 16, left))
            if not chunk:
                break
            outputfile.write(chunk)
            left -= len(chunk)
        self._range_left = None


def lan_ip() -> str:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--lan", action="store_true", help="listen on all interfaces")
    args = ap.parse_args()
    host = "0.0.0.0" if args.lan else "127.0.0.1"
    httpd = ThreadingHTTPServer((host, args.port), partial(RangeHandler, directory=str(WEB)))
    print(f"player:  http://localhost:{args.port}/")
    print(f"glasses: http://localhost:{args.port}/glasses.html")
    if args.lan:
        print(f"on your network: http://{lan_ip()}:{args.port}/glasses.html")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
