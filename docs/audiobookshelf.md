# Read along with Audiobookshelf

[Audiobookshelf](https://www.audiobookshelf.org/) is a self-hosted audiobook server. If your books are already there, Read-along can use them directly. The audio stays on your server and isn't copied anywhere.

There are two ways to use it:

- **Play in Read-along** (player mode). Open an Audiobookshelf book in Read-along on your phone, computer or glasses. The audio streams from your server, the words light up as they're spoken, and your place is saved back to Audiobookshelf. Stop on the glasses and the Audiobookshelf app picks up there, and the other way round.
- **Follow the Audiobookshelf app** (follow mode). Keep listening in the Audiobookshelf app on your phone, and choose **Follow Audiobookshelf** on the glasses (or **Follow what's playing** on the website). The glasses show the text for whatever is playing, like synced lyrics for a song playing on another device.

  Follow mode has a limit: the Audiobookshelf app tells your server where you are only every 10 to 15 seconds. Read-along counts forward between those reports, so while you're listening it keeps up well. After a pause, a skip or a speed change, though, the text can be off for up to about 15 seconds, until the next report arrives. For exact word-by-word timing, play the book in Read-along instead.

Both modes need a **sync file** for the book. Your PC makes it, the same way as for other books (see [google-drive.md](google-drive.md)), and it's kept in your Google Drive. So you need Google Drive connected as well.

## Setup

1. **Let Read-along talk to your server.** In Audiobookshelf, open **Settings**. Under **Allowed CORS Origins**, add the address of the Read-along site without a path, for example `https://jnorwoo.github.io`, and save. Without this, browsers block the connection.
2. **Make an API key.** In Audiobookshelf, go to **Settings → API Keys** and create one for your user. Give it no expiry, or a long one, and copy it.
3. **Connect on the website.** On your phone or computer, open the Read-along website and connect Google Drive if you haven't yet. Under **Audiobookshelf**, enter your server's address and the API key, then click **Connect Audiobookshelf**.
   - You can paste the address straight from your browser, such as `https://abs.example.com/audiobookshelf/library/...`. Read-along finds the server's base address itself.
   - Leave **Use it on my other devices too** ticked. This keeps the address and key in your Google Drive (the `audiobookshelf.json` file in the Read-along folder), so the glasses and your PC's sync helper can use them without typing. Untick it to keep the key on this device only.
4. **Make sync files.** Under **Audiobookshelf**, find a book and click **Make sync file**. Your PC's sync helper (`python tools/sync_worker.py`, running on the PC with the GPU) does the rest:
   - It downloads the audio and ebook from your server to the PC.
   - It runs WhisperX and saves the finished sync file to Drive.
   - Progress shows under **Making sync files**.
   - When the job is done, the book appears in your library on every device.

   The book needs its ebook (EPUB, PDF or TXT) attached in Audiobookshelf. Books without one say so.

   Already synced the same book into Google Drive (with `sync_worker.py --add` or the website upload)? The book then shows **Use your existing sync file** instead. Clicking it reuses that sync file, so nothing is transcribed again. This works when the server has the same recording. If the two recordings' lengths differ by more than a few seconds, the website warns you that the text may drift; use **Make sync file** in that case.

## Using it

**On the glasses**, Audiobookshelf books appear in the library tagged "Audiobookshelf", and **Follow Audiobookshelf** appears at the bottom of the list.
- Opening a book plays it, with the usual controls.
- In follow mode, playback is controlled from the Audiobookshelf app. On the glasses, Up opens the options menu, where you can change the text timing or go back to the library. Back stops following.

**On the website**, Audiobookshelf books appear in the library too, and **Follow what's playing** is in the Audiobookshelf section.

**Removing** an Audiobookshelf book from Read-along deletes only its sync file. The book stays on your server.

**Offline:** Audiobookshelf books stream, so they need a connection to your server. To listen with no connection, add the book to Drive instead, using `sync_worker.py --add` or the website upload.

## How it works

- Read-along calls the Audiobookshelf API from your browser with the API key. The key goes in the address (`?token=`) rather than a header, because browsers won't send an Authorization header through Audiobookshelf's CORS settings. Audiobookshelf accepts the key either way. One side effect: the key can show up in your server's or reverse proxy's access logs.
- Books split into many files (e.g. 55 MP3 parts) play back to back as one book (`TrackAudio` in `web/abs.js`).
- The sync helper times the text against Audiobookshelf's own track lengths (`book_sync.py --durations ... --no-join`), so the times line up exactly with its player.
- Positions are saved to Audiobookshelf as `currentTime`, at most every 15 seconds while playing, and right away when you pause or leave. When a book opens, the newest position wins, whether it came from this device, Google Drive, or Audiobookshelf.
- Follow mode asks the server every 2.5 seconds for your most recently updated book and its position. From the reports it works out whether you're playing and at what speed. It also works out how far the server's clock is from the device's.

## Testing without a server

`tests/mock_audiobookshelf.py` imitates the parts of the Audiobookshelf API that Read-along uses. That includes its CORS behaviour and a "phone app" that reports progress every 10 seconds:

```bash
python tests/mock_audiobookshelf.py --library examples/abs-library
```

Each folder in `--library` becomes a book (audio files + ebook). The server address is `http://localhost:8766/audiobookshelf` and the API key is `test-key`. To imitate the phone app playing a book:

```bash
curl -X POST localhost:8766/mock/play -d '{"item": "<id>", "pos": 60, "rate": 1.25}'
```

```bash
curl -X POST localhost:8766/mock/pause
```

Use it with `tests/mock_google.py` (see [google-drive.md](google-drive.md)).
