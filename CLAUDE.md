# CLAUDE.md

Context for Claude Code working in this repo. Read this first.

## What this project is

A read-along app for audiobooks: the ebook's text is shown in sync with the audio, word by word, like synced lyrics. The main target is **Meta Ray-Ban Display** glasses (web app on the 600×600 in-lens display), plus a desktop/phone player. The owner also has a separate Spotify lyrics app called Cue; this project may later share its sync format with Cue.

Pipeline:
1. `tools/book_sync.py` transcribes the audiobook with WhisperX (word timestamps) in cached chunks, then aligns the transcript to the ebook's real text and writes `<name>.lrc` and `<name>.sync.json`.
2. `tools/add_to_library.py` copies audio + sync into `web/books/<id>/` and updates `web/library.json` (or the git-ignored `web/books/private/` + `web/library.private.json` with `--private`).
3. `web/index.html` (desktop/phone) and `web/glasses.html` (glasses) read the library and play books. No build step, no framework.
4. Optional Google Drive (`web/cloud.js`, shared by both pages; setup in `docs/google-drive.md`): users add their own books (audio + sync.json) to a "Read-along" Drive folder from the web app, and reading positions sync across devices via `progress.json` there. Sign-in is Google's device-code flow (code entered at google.com/device), which works on the glasses' D-pad. Scope is `drive.file` only. Opened Drive books are cached in Cache Storage for offline play; `web/sw.js` caches the app shell. `web/cloud-config.js` stays empty in git; the Pages workflow fills it from the `GOOGLE_CLIENT_ID` variable and `GOOGLE_CLIENT_SECRET` secret.

## Commands

```bash
pip install -r requirements.txt               # whisperx (pulls in torch)
python tools/fetch_example.py [--device cpu]  # Gutenberg example: download, sync, add to library
python tools/serve.py [--lan]                 # http://localhost:8000 and /glasses.html
python -m unittest discover tests -v          # 10 tests, no GPU/WhisperX needed (ffmpeg needed for 1)
python tests/mock_google.py                   # fake Google sign-in + Drive on :8765 for testing cloud.js (see docs/google-drive.md)
```

## Status

Tested and working:
- EPUB/TXT parsing, chapter detection, Project Gutenberg header/license stripping.
- Alignment on synthetic data: >90% of words timed, average error about 0.01s, worst under 1s, with dropped/misheard words, filler, audiobook intro credits, and a section missing from the audio (correctly left untimed). About 1s for a 120k-word novel.
- Chunked transcription plumbing with a stub transcriber: chunk overlap de-duplication, multi-file offsets, caching, joining multi-file audio with ffmpeg.
- Both web apps in headless Chromium: loading, playback, word highlighting, line seeking, chapters, options menu, resume position, library loading, 600×600 layout, light/dark (index.html), phone width.
- `tools/serve.py` range requests (206 responses).

- **Real WhisperX run** (Nov 2026, Windows 11, RTX 3070 8 GB): whisperx 3.8.6 with torch 2.8.0+cu128 and large-v3 worked with no API changes. Gift of the Magi: 2,012 of 2,065 words matched directly, all timed. Spot check (re-transcribing line clips with a different model) confirmed line timings. The Gutenberg URLs in `fetch_example.py` work and the recording matches the text edition.
- Windows setup notes: `pip install whisperx` installs CPU torch; reinstall `torch==2.8.0 torchaudio==2.8.0 torchvision==0.23.0` from `https://download.pytorch.org/whl/cu128` (`--no-deps --force-reinstall`). ffmpeg via `winget install Gyan.FFmpeg` (open a new shell for PATH). The torchcodec "Could not load libtorchcodec" warning from pyannote is harmless (audio is decoded by ffmpeg). Import torch before using faster-whisper directly, or ctranslate2 can't find `cublas64_12.dll`.
- Gutenberg EPUBs put the story in the same file as the license; chapter titles now come only from headings that survive boilerplate stripping, and untitled files continue the previous chapter.

- Google Drive features (Nov 2026) against `tests/mock_google.py` in Chromium: device-code sign-in on both pages, resumable chunked upload, listing, download into Cache Storage, playback from the cached blob, position sync both ways (newer wins), token refresh, glasses "Remove download".

NOT yet tested:
1. **The glasses app on Meta's simulator and hardware.** See "Meta Ray-Ban Display" below.
2. **Google Drive against real Google.** Needs the user's Cloud project (docs/google-drive.md). Unverified assumptions: Google's upload server exposes the `Location`/`Range` headers to browsers (cloud.js falls back sensibly if `Range` is hidden); Drive answers `alt=media` downloads with CORS; device-flow client works from a browser (CORS preflight to oauth2.googleapis.com was checked and passes).

## Meta Ray-Ban Display facts (verified Nov 2026 from developers.meta.com)

- Web apps run on the glasses via standard web APIs and are opened by URL; users need developer mode. Developer preview, no public store yet (an on-glasses "Explore" app for discovery has been announced).
- Display is 600×600 and **additive**: black is see-through, bright high-contrast elements show. Keep backgrounds pure black, never semi-transparent overlays.
- Input: arrow keys + Enter from Neural Band gestures (D-pad). Pinch-and-drag is opt-in.
- Audio playback through the glasses speaker is supported; also volume/mute control.
- Offline: service workers and the Cache API are supported.
- Docs: https://wearables.developer.meta.com/docs/develop/webapps/ . Meta offers a **Wearables MCP server** for current docs (https://wearables.developer.meta.com/docs/develop/webapps/ai-assisted-mcp); add it to Claude Code if possible and check docs there before guessing. Meta's toolkit repo is `facebook/meta-wearables-webapp`.
- Simulator: Chrome extension "Meta Ray-Ban Display Web App Simulator".

Open questions to check in Meta's docs:
- Does audio keep playing if the web app is backgrounded or the display sleeps? If not, document it and consider keeping the app foregrounded.
- Does a web app need a manifest or any required meta tags / registration step?
- Can the glasses open `http://` LAN URLs (for private books served by `tools/serve.py --lan`), or only HTTPS? If HTTPS only, private books need another route (see next steps).
- Is there a hardware back gesture, and what key event does it send? `glasses.html` treats Escape/Backspace as back and also offers "Library" in the options menu.

## Next steps (suggested order)

1. ~~Real WhisperX run on the example~~ (done; repo is github.com/jnorwoo/readalong).
2. Enable Pages (Settings → Pages → Source: GitHub Actions). The workflow `.github/workflows/pages.yml` runs tests, then deploys `web/` without private books.
3. Test `glasses.html` in Meta's simulator, then on the glasses via the Pages URL. Adjust font sizes / `MAX_WORDS` (currently 7) for legibility.
4. ~~Service worker / offline~~ and ~~private books on the glasses~~: done via Google Drive (`web/cloud.js`). Test with real Google and on the glasses.
5. Check on the glasses: Cache Storage quota for large books (Meta's docs give none), and whether audio keeps playing when the display sleeps.
6. Optional: Meta AI voice control (WebMCP-style tools like play, pause, "go back a sentence"), currently off by default on devices and enabled per device in developer mode.
7. Optional: share the sync format with Cue.

## Constraints

- **Copyright:** only public-domain books may be committed or published. Purchased books stay in git-ignored `web/books/private/`. Never add tools for removing DRM (Audible AAX, Kindle).
- Keep the web apps buildless; GitHub Pages serves `web/` as-is. Each page is one file, except the shared optional Google Drive code in `web/cloud.js` (+ `cloud-config.js`, `sw.js`).
- Pin exact versions for any CDN script added.
- Keep `book_sync.py` importable without whisperx installed (the import is lazy) so tests run without a GPU.

## Sync file format (`*.sync.json`)

```json
{
  "version": 1,
  "title": "The Gift of the Magi",
  "audio": "audio.mp3",
  "duration": 802.4,
  "chapters": [{ "title": "Chapter 1", "start": 31.2 }],
  "lines": [
    { "start": 31.2, "end": 34.9, "text": "One dollar and eighty-seven cents.",
      "words": [{ "w": "One", "s": 31.2, "e": 31.4 }] }
  ]
}
```

Times are seconds from the start of the audio. A word's `s`/`e` may be `null` (untimed); treat it as spoken with the previous word. Lines are sorted by `start`. `glasses.html` re-chunks lines to at most 7 words using word timings.

## How alignment works (book_sync.py)

The transcript is matched to the ebook in 400-word chunks with `difflib.SequenceMatcher`, searching a window ahead of the current position (25,000 words when "lost", e.g. at the start or after unnarrated text). Matching blocks that jump much further in the book than in the audio are rejected (repeated phrases), and a final longest-increasing-subsequence pass drops any match whose time goes backwards. Unmatched words in short gaps get interpolated times; long gaps stay untimed and are left out of the output.
