#!/usr/bin/env python3
"""
add_to_library.py - put a synced book into web/books/ so the web apps can open it.

  python tools/add_to_library.py gift-of-the-magi "My Book.mp3" "My Book.sync.json" \
      --title "The Gift of the Magi" --author "O. Henry"

Copies the audio and sync file to web/books/<id>/ and adds or updates the entry
in web/library.json. Use --private for books you bought: they go to
web/books/private/<id>/, which .gitignore keeps out of the repository.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WEB = ROOT / "web"


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description="Add a synced book to the web library.")
    ap.add_argument("id", help="short id, letters/digits/hyphens, e.g. gift-of-the-magi")
    ap.add_argument("audio", type=Path)
    ap.add_argument("sync", type=Path, help="the .sync.json made by book_sync.py")
    ap.add_argument("--title")
    ap.add_argument("--author", default="")
    ap.add_argument("--private", action="store_true",
                    help="store under web/books/private/ (never committed)")
    args = ap.parse_args(argv)

    if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", args.id):
        sys.exit("error: id must be lowercase letters, digits and hyphens")
    for p in (args.audio, args.sync):
        if not p.exists():
            sys.exit(f"error: file not found: {p}")

    sync = json.loads(args.sync.read_text(encoding="utf-8"))
    title = args.title or sync.get("title") or args.id

    rel_dir = Path("books") / ("private" if args.private else "") / args.id
    dest = WEB / rel_dir
    dest.mkdir(parents=True, exist_ok=True)
    audio_name = "audio" + args.audio.suffix.lower()
    shutil.copyfile(args.audio, dest / audio_name)
    sync["audio"] = audio_name
    (dest / "sync.json").write_text(json.dumps(sync, ensure_ascii=False, separators=(",", ":")),
                                    encoding="utf-8")

    lib_name = "library.private.json" if args.private else "library.json"
    lib_path = WEB / lib_name
    lib = json.loads(lib_path.read_text(encoding="utf-8")) if lib_path.exists() else {"books": []}
    entry = {
        "id": args.id,
        "title": title,
        "author": args.author,
        "audio": (rel_dir / audio_name).as_posix(),
        "sync": (rel_dir / "sync.json").as_posix(),
        "duration": sync.get("duration"),
    }
    lib["books"] = [b for b in lib["books"] if b["id"] != args.id] + [entry]
    lib_path.write_text(json.dumps(lib, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"added '{title}' to web/{lib_name} (files in web/{rel_dir.as_posix()})")


if __name__ == "__main__":
    main()
