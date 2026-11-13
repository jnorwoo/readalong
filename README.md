# Read-along

Listen to an audiobook and read the book's text along with it, word by word: on your computer, your phone, or in the display of Meta Ray-Ban Display glasses.

It works like synced lyrics, but for books. A script listens to the audiobook with [WhisperX](https://github.com/m-bain/whisperX), lines up what it hears with the ebook's real text, and saves the timing. The web apps then play the audio and light up each word as it's spoken.

Comes with a public-domain example from Project Gutenberg: *The Gift of the Magi* by O. Henry, text ([eBook #7256](https://www.gutenberg.org/ebooks/7256)) and a human-read recording ([eBook #22440](https://www.gutenberg.org/ebooks/22440)).

## What's in here

| Path | What it does |
|---|---|
| `tools/book_sync.py` | Makes the timing file from an audiobook + ebook (EPUB or TXT) |
| `tools/fetch_example.py` | Downloads the Gutenberg example and syncs it, in one step |
| `tools/add_to_library.py` | Adds a synced book to the web apps' library |
| `tools/serve.py` | Runs the web apps locally (with the audio seeking support browsers need) |
| `web/index.html` | Read-along player for computer and phone |
| `web/glasses.html` | Read-along app for Meta Ray-Ban Display (600×600, controlled with Neural Band) |
| `web/cloud.js` | Optional Google Drive library + reading-position sync, shared by both apps |
| `docs/google-drive.md` | How to set up Google Drive |
| `tests/` | Tests for the syncing logic (no GPU or WhisperX needed) |

## Quick start

You need Python 3.10+ and ffmpeg (`brew install ffmpeg`, `winget install ffmpeg`, or `sudo apt install ffmpeg`).

```bash
pip install -r requirements.txt
python tools/fetch_example.py        # downloads + syncs the example (about 13 min of audio)
python tools/serve.py
```

Then open http://localhost:8000 for the player, or http://localhost:8000/glasses.html for the glasses app. In the glasses app, use the arrow keys and Enter to try the controls.

On a computer without an NVIDIA GPU, add `--device cpu` to `fetch_example.py` (it passes options through to `book_sync.py`). The 13-minute example takes a few minutes on CPU.

## Your own books

Only DRM-free files work: EPUB or TXT ebooks, and MP3, M4A, M4B and similar audio. Audible `.aax` files and DRM-protected Kindle books can't be used.

```bash
python tools/book_sync.py "My Book.m4b" --book "My Book.epub" --language en
python tools/add_to_library.py my-book "My Book.m4b" "My Book.sync.json" --private
```

`--private` keeps the book in `web/books/private/`, which is never committed to git or published. Private books show up when you run the app locally with `tools/serve.py`.

To play your own books on the glasses or your phone, keep them in **Google Drive** instead: add the audio and its `.sync.json` from the web app, and every device you connect can play them offline and pick up where you left off. One-time setup: [docs/google-drive.md](docs/google-drive.md).

## On Meta Ray-Ban Display

Meta Ray-Ban Display glasses run web apps opened from a URL (developer mode required). This repo publishes `web/` to GitHub Pages automatically, so after you push:

1. In your repo on GitHub, go to **Settings → Pages** and set **Source** to **GitHub Actions**.
2. Push to `main`. The site appears at `https://<your-username>.github.io/<repo-name>/`.
3. Turn on developer mode for your glasses in the Meta AI app, then open `https://<your-username>.github.io/<repo-name>/glasses.html`.

Without the hardware, try it in Meta's [Ray-Ban Display Web App Simulator](https://chromewebstore.google.com/detail/meta-ray-ban-display-simu/jpjlmmodokemlepklkdbimceggpbjcll) for Chrome.

**Glasses controls:** Select (Enter) plays and pauses. Left and right move back or forward a line. Up opens options: speed, text timing, back 30 seconds, the library, and (for Drive books) removing the download. Back (Escape) returns to the library.

Note: the regular Ray-Ban Meta glasses (without a display) can't show text, so this needs the Display model.

## Development

```bash
python -m unittest discover tests -v
```

The sync file format is documented in `CLAUDE.md`.

## License

Code: MIT. The example book and recording are public domain in the USA, from Project Gutenberg.
