# Your books in Google Drive

With Google Drive connected, Read-along can:

- **Keep your own audiobooks in your Drive.** Add them from your phone or computer, then play them on the glasses or any other device. They're never published; they sit in a "Read-along" folder in your own Drive.
- **Remember your place across devices.** Pause on the glasses and your phone picks up at the same spot, and the other way round.
- **Play offline.** A book you've opened once stays on that device, so it plays without a connection.

The app asks for one permission, `drive.file`: it can see and change only the files it added itself, not anything else in your Drive.

There's no server of our own. The web page talks to Google directly. You do need to set up a free Google Cloud project once (about 10 minutes), because Google requires every app that uses Drive to have one.

## One-time setup

### 1. Create a Google Cloud project

1. Go to https://console.cloud.google.com/ and sign in with the Google account whose Drive you want to use.
2. Click the project picker at the top, then **New project**. Name it "Read-along" and create it. Make sure it's selected.
3. Go to **APIs & Services → Library**, search for **Google Drive API**, open it and click **Enable**.

### 2. Set up the sign-in screen

1. Go to **Google Auth Platform** (or **APIs & Services → OAuth consent screen**) and click **Get started**.
2. App name: "Read-along". Support email: your email. Audience: **External**. Contact email: your email. Agree and create.
3. Open **Data access → Add or remove scopes**, find `.../auth/drive.file` ("See, edit, create, and delete only the specific Google Drive files you use with this app"), tick it, and save.
4. Open **Audience** and click **Publish app** to move it to **In production**. This matters: while an app is in "Testing", Google signs you out every 7 days. `drive.file` is a non-sensitive permission, so Google doesn't need to review the app for this.

   If you'd rather stay in Testing, add your Google account under **Test users** and expect to reconnect weekly.

### 3. Create the sign-in client

1. Open **Clients → Create client**.
2. Application type: **TVs and Limited Input devices**. Name: "Read-along". Create.
3. Copy the **Client ID** and **Client secret**.

This client type is the "enter a code at google.com/device" sign-in. It's the only kind the glasses can use without a keyboard, and the same client also works on phones and computers.

### 4. Give the IDs to the website

The GitHub Pages workflow writes them into `web/cloud-config.js` when it deploys, so they never have to be committed. In a terminal in this repo:

```bash
gh variable set GOOGLE_CLIENT_ID --body "<your client ID>"
```

```bash
gh secret set GOOGLE_CLIENT_SECRET
```

(`gh secret set` asks you to paste the secret.) Then redeploy:

```bash
gh workflow run pages.yml
```

About the "secret": for TV-style sign-in, Google doesn't treat it as confidential, and it ends up readable in the published page. That's expected for this kind of client. Someone who copied it could show "Read-along" on their own sign-in screen, but they could never get into your Drive without you approving it.

## Using it

**On your phone or computer** (https://&lt;you&gt;.github.io/readalong/):

1. Under "Your books in Google Drive", tap **Connect Google Drive**. A code appears.
2. Open the google.com/device link (it can be on the same phone), sign in, and enter the code.
3. Tap **Add a book to Drive** and pick the audiobook and its `.sync.json` together. Make the `.sync.json` on your computer with `tools/book_sync.py` first.
4. The book shows in the library on every connected device.

**On the glasses** (https://&lt;you&gt;.github.io/readalong/glasses.html):

1. Choose **Connect Google Drive** at the bottom of the library. The glasses show a code.
2. On your phone, go to google.com/device and enter it.
3. Choose a Drive book. The first time, it downloads (Back cancels). After that it plays offline.
4. To free space, open the options (Up) and choose **Remove download**. The book stays in Drive.

## How it works

- Books: each book is two files in the "Read-along" Drive folder, the audio and the `.sync.json`. The app finds them by private tags it adds (`appProperties`), so renaming or moving them in Drive is fine. Deleting them in Drive removes the book.
- Positions: `progress.json` in the same folder holds one entry per book: position, speed, text timing, and when it was saved. Each device saves locally every 5 seconds, and to Drive at most every 30 seconds while playing, right away on pause, and when the app is hidden. When you open a book, the newer of this device's position and Drive's position wins.
- Offline: opened Drive books are kept in the browser's Cache Storage, and a small service worker (`web/sw.js`) keeps the app itself available offline.
- Code: `web/cloud.js` (shared by both pages), settings in `web/cloud-config.js`.

## Testing without Google

`tests/mock_google.py` imitates the parts of Google's sign-in and Drive APIs the app uses, in memory:

```bash
python tests/mock_google.py
```

```bash
python tools/serve.py
```

Then, in the browser console on http://localhost:8000:

```js
localStorage["readalong:cloud-config"] = JSON.stringify({clientId: "test", clientSecret: "test",
  oauthBase: "http://localhost:8765", apiBase: "http://localhost:8765"})
```

Reload. Sign-in approves itself after a couple of seconds. Remove that localStorage key to go back to the real settings.
