#!/usr/bin/env python3
"""
fetch_example.py - download a public-domain example from Project Gutenberg,
sync it, and add it to the web library.

  python tools/fetch_example.py

Text:  "The Gift of the Magi" by O. Henry, Project Gutenberg eBook #7256
Audio: the same story, read aloud, Project Gutenberg eBook #22440 (about 13 minutes)

Both are public domain in the USA. Downloads go to examples/ (ignored by git);
the synced result is added to web/books/gift-of-the-magi/, which is committed so
the hosted web app has a book to show.
"""

from __future__ import annotations

import subprocess
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DL = ROOT / "examples" / "gift-of-the-magi"

TEXT_URL = "https://www.gutenberg.org/ebooks/7256.epub.images"
AUDIO_URL = "https://www.gutenberg.org/files/22440/mp3/22440-01.mp3"
USER_AGENT = "readalong-example-fetcher/1.0 (personal, one-time download)"


def download(url: str, dest: Path) -> None:
    if dest.exists() and dest.stat().st_size > 0:
        print(f"already have {dest.name}")
        return
    print(f"downloading {url}")
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=60) as r, open(dest, "wb") as f:
        while chunk := r.read(1 << 16):
            f.write(chunk)


def main() -> None:
    DL.mkdir(parents=True, exist_ok=True)
    epub = DL / "gift-of-the-magi.epub"
    mp3 = DL / "gift-of-the-magi.mp3"
    download(TEXT_URL, epub)
    download(AUDIO_URL, mp3)

    extra = sys.argv[1:]   # pass-through, e.g. --model medium --device cpu
    subprocess.run([sys.executable, str(ROOT / "tools" / "book_sync.py"), str(mp3),
                    "--book", str(epub), "--language", "en", *extra], check=True)
    subprocess.run([sys.executable, str(ROOT / "tools" / "add_to_library.py"),
                    "gift-of-the-magi", str(mp3), str(DL / "gift-of-the-magi.sync.json"),
                    "--title", "The Gift of the Magi", "--author", "O. Henry"], check=True)
    print("\ndone. Start the web app with:  python tools/serve.py")


if __name__ == "__main__":
    main()
