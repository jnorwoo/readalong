# CLAUDE.md

Context for Claude Code working in this repo. Read this first.

## What this project is

A read-along app for audiobooks: the ebook's text is shown in sync with the audio, word by word, like synced lyrics. The main target is **Meta Ray-Ban Display** glasses (web app on the 600×600 in-lens display), plus a desktop/phone player. The owner also has a separate Spotify lyrics app called Cue; this project may later share its sync format with Cue.

Pipeline:
1. `tools/book_sync.py` transcribes the audiobook with WhisperX (word timestamps) in cached chunks, then aligns the transcript to the ebook's real text and writes `<name>.lrc` and `<name>.sync.json`.
2. `tools/add_to_library.py` copies audio + sync into `web/books/<id>/` and updates `web/library.json` (or the git-ignored `web/books/private/` + `web/library.private.json` with `--private`).
3. `web/index.html` (desktop/phone) and `web/glasses.html` (glasses) read the library and play books. Both are single-file, no build step, no framework.

## Commands

```bash
pip install -r requirements.txt               # whisperx (pulls in torch)
python tools/fetch_example.py [--device cpu]  # Gutenberg example: download, sync, add to library
python tools/serve.py [--lan]                 # http://localhost:8000 and /glasses.html
python -m unittest discover tests -v          # 9 tests, no GPU/WhisperX needed (ffmpeg needed for 1)
```

## Status (as handed off from a claude.ai chat)

Tested and working:
- EPUB/TXT parsing, chapter detection, Project Gutenberg header/license stripping.
- Alignment on synthetic data: >90% of words timed, average error about 0.01s, worst under 1s, with dropped/misheard words, filler, audiobook intro credits, and a section missing from the audio (correctly left untimed). About 1s for a 120k-word novel.
- Chunked transcription plumbing with a stub transcriber: chunk overlap de-duplication, multi-file offsets, caching, joining multi-file audio with ffmpeg.
- Both web apps in headless Chromium: loading, playback, word highlighting, line seeking, chapters, options menu, resume position, library loading, 600×600 layout, light/dark (index.html), phone width.
- `tools/serve.py` range requests (206 responses).

NOT yet tested (do these first):
1. **A real WhisperX run.** The sandbox couldn't download models. `Transcriber` in `book_sync.py` uses the standard whisperx API (`load_model`, `load_audio`, `transcribe`, `load_align_model`, `align`, reading `word_segments`). Run `python tools/fetch_example.py` and fix any API mismatch with the installed whisperx version. Check the reported coverage (expect >85%) and listen to a few spots in the player to confirm timing.
2. **The real Gutenberg files.** URLs are in `tools/fetch_example.py` (text: ebook 7256 EPUB; audio: ebook 22440 MP3). Confirm they download and that the recording matches the text edition. If the audio starts with a spoken intro, that's expected and handled.
3. **The glasses app on Meta's simulator and hardware.** See "Meta Ray-Ban Display" below.

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

1. Real WhisperX run on the example; fix issues; commit `web/books/gift-of-the-magi/` and `web/library.json`.
2. Push to GitHub, enable Pages (Settings → Pages → Source: GitHub Actions). The workflow `.github/workflows/pages.yml` runs tests, then deploys `web/` without private books.
3. Test `glasses.html` in Meta's simulator, then on the glasses via the Pages URL. Adjust font sizes / `MAX_WORDS` (currently 7) for legibility.
4. Add a service worker so a downloaded book plays offline on the glasses.
5. Private books on the glasses without publishing them, e.g. a local HTTPS server or a tunnel, depending on what the glasses accept.
6. Optional: Meta AI voice control (WebMCP-style tools like play, pause, "go back a sentence"), currently off by default on devices and enabled per device in developer mode.
7. Optional: share the sync format with Cue.

## Constraints

- **Copyright:** only public-domain books may be committed or published. Purchased books stay in git-ignored `web/books/private/`. Never add tools for removing DRM (Audible AAX, Kindle).
- Keep the web apps single-file and buildless unless there's a strong reason; GitHub Pages serves `web/` as-is.
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
