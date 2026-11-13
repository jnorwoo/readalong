/* cloud.js - optional Google Drive library and reading-position sync for Read-along.
 *
 * No server of our own: the page talks to Google directly.
 * - Sign-in uses Google's TV-style "device code" flow: the page shows a short code and
 *   you enter it at google.com/device on any phone. That works on the glasses' D-pad.
 * - The only permission is drive.file: the app sees only files it created itself,
 *   not the rest of your Drive.
 * - Books live in a "Read-along" folder in your Drive: the audio file plus its
 *   .sync.json. Reading positions live in progress.json in the same folder.
 * - Opened books are kept in this browser's Cache Storage so they play offline.
 *
 * Setup (Google Cloud project, client ID): docs/google-drive.md.
 * If cloud-config.js has no client ID, everything here reports enabled = false.
 */
(() => {
  const ls = {
    get(k) { try { return JSON.parse(localStorage.getItem(k)); } catch { return null; } },
    set(k, v) { try { localStorage.setItem(k, JSON.stringify(v)); } catch {} },
    del(k) { try { localStorage.removeItem(k); } catch {} },
  };

  const CFG = Object.assign({
    clientId: "",
    clientSecret: "",
    oauthBase: "https://oauth2.googleapis.com",
    apiBase: "https://www.googleapis.com",
  }, window.READALONG_CLOUD || {},
  // Developer override, e.g. pointing at tests/mock_google.py. Set from the console:
  // localStorage["readalong:cloud-config"] = JSON.stringify({clientId: "x", oauthBase: ...})
  ls.get("readalong:cloud-config") || {});

  const SCOPE = "https://www.googleapis.com/auth/drive.file";
  const TOKEN_KEY = "readalong:google";
  const BOOK_CACHE = "readalong-books-v1";
  const FOLDER_TYPE = "application/vnd.google-apps.folder";
  const CHUNK = 8 * 1024 * 1024;          // resumable upload chunk (multiple of 256 KiB)

  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

  // Browsers and phones label audiobook files inconsistently (an .m4b may come through
  // as "", "audio/x-m4b" or "application/octet-stream"). Chrome plays them anyway, but
  // Safari is stricter, so use the standard type for the file's extension.
  const AUDIO_TYPES = { m4b: "audio/mp4", m4a: "audio/mp4", mp4: "audio/mp4", aac: "audio/aac", mp3: "audio/mpeg",
    ogg: "audio/ogg", opus: "audio/ogg", oga: "audio/ogg", flac: "audio/flac", wav: "audio/wav", webm: "audio/webm" };
  function audioType(name, type) {
    const ext = String(name || "").split(".").pop().toLowerCase();
    return AUDIO_TYPES[ext] || (type && type !== "application/octet-stream" ? type : "audio/mpeg");
  }
  const signedOutError = () => Object.assign(new Error("Not connected to Google Drive."), { signedOut: true });

  // ---------------------------------------------------------------- sign-in

  async function oauth(path, body) {
    const r = await fetch(CFG.oauthBase + path, { method: "POST", body: new URLSearchParams(body) });
    const d = await r.json().catch(() => ({}));
    return { ok: r.ok, d };
  }

  function saveToken(d) {
    const prev = ls.get(TOKEN_KEY) || {};
    ls.set(TOKEN_KEY, {
      refresh: d.refresh_token || prev.refresh,
      access: d.access_token,
      exp: Date.now() + (d.expires_in || 3600) * 1000,
    });
  }

  /** Start sign-in. Show `code` and `url` to the user; `done` resolves once they approve. */
  async function startSignIn() {
    const { ok, d } = await oauth("/device/code", { client_id: CFG.clientId, scope: SCOPE });
    if (!ok) throw new Error("Google sign-in isn’t available: " + (d.error_description || d.error || "unknown error"));
    let interval = (d.interval || 5) * 1000;
    let cancelled = false;
    const deadline = Date.now() + (d.expires_in || 1800) * 1000;
    const done = (async () => {
      while (!cancelled) {
        await sleep(interval);
        if (cancelled) break;
        if (Date.now() > deadline) throw new Error("The code expired. Start again for a new one.");
        const t = await oauth("/token", {
          client_id: CFG.clientId, client_secret: CFG.clientSecret, device_code: d.device_code,
          grant_type: "urn:ietf:params:oauth:grant-type:device_code",
        });
        if (t.ok) { saveToken(t.d); return true; }
        const err = t.d.error;
        if (err === "authorization_pending") continue;
        if (err === "slow_down") { interval += 5000; continue; }
        if (err === "access_denied") throw new Error("Sign-in was declined.");
        if (err === "expired_token") throw new Error("The code expired. Start again for a new one.");
        throw new Error("Sign-in failed: " + (t.d.error_description || err || "unknown error"));
      }
      return false;
    })();
    return {
      code: d.user_code,
      url: d.verification_url || d.verification_uri || "https://www.google.com/device",
      done,
      cancel() { cancelled = true; },
    };
  }

  function signOut() {
    const t = ls.get(TOKEN_KEY);
    ls.del(TOKEN_KEY);
    folderId = progressId = null;
    if (t && t.refresh) fetch(CFG.oauthBase + "/revoke", { method: "POST", body: new URLSearchParams({ token: t.refresh }) }).catch(() => {});
  }

  let refreshing = null;
  async function accessToken(force) {
    const t = ls.get(TOKEN_KEY);
    if (!t || !t.refresh) throw signedOutError();
    if (!force && t.access && t.exp - 60000 > Date.now()) return t.access;
    if (!refreshing) {
      refreshing = (async () => {
        try {
          const r = await oauth("/token", {
            client_id: CFG.clientId, client_secret: CFG.clientSecret,
            refresh_token: t.refresh, grant_type: "refresh_token",
          });
          if (!r.ok) {
            if (r.d.error === "invalid_grant") { ls.del(TOKEN_KEY); throw signedOutError(); }
            throw new Error("Couldn’t reach Google: " + (r.d.error_description || r.d.error || "network error"));
          }
          saveToken(r.d);
          return r.d.access_token;
        } finally { refreshing = null; }
      })();
    }
    return refreshing;
  }

  // ---------------------------------------------------------------- Drive API

  async function api(path, opts = {}, retry = true) {
    const token = await accessToken();
    const headers = Object.assign({ Authorization: "Bearer " + token }, opts.headers || {});
    const r = await fetch(CFG.apiBase + path, Object.assign({}, opts, { headers }));
    if (r.status === 401 && retry) { await accessToken(true); return api(path, opts, false); }
    if (!r.ok) throw new Error(`Google Drive error ${r.status}: ${(await r.text()).slice(0, 200)}`);
    return r;
  }

  const enc = encodeURIComponent;
  const tagged = (v) => `appProperties has { key='ra' and value='${v}' }`;

  async function findFiles(query, fields = "id,name,size,mimeType,appProperties") {
    const r = await api(`/drive/v3/files?q=${enc(query + " and trashed=false")}` +
      `&fields=${enc("files(" + fields + ")")}&pageSize=1000&orderBy=${enc("modifiedTime desc")}`);
    return (await r.json()).files || [];
  }

  let folderId = null;
  async function folder() {
    if (folderId) return folderId;
    const found = await findFiles(`${tagged("root")} and mimeType='${FOLDER_TYPE}'`, "id");
    if (found.length) return (folderId = found[0].id);
    const r = await api("/drive/v3/files?fields=id", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name: "Read-along", mimeType: FOLDER_TYPE, appProperties: { ra: "root" } }),
    });
    return (folderId = (await r.json()).id);
  }

  async function createFile(meta, blob) {
    const b = "readalong" + Math.random().toString(36).slice(2);
    const body = new Blob([
      `--${b}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n${JSON.stringify(meta)}\r\n`,
      `--${b}\r\nContent-Type: ${blob.type || "application/octet-stream"}\r\n\r\n`, blob, `\r\n--${b}--\r\n`,
    ]);
    const r = await api("/upload/drive/v3/files?uploadType=multipart&fields=id", {
      method: "POST", headers: { "Content-Type": `multipart/related; boundary=${b}` }, body,
    });
    return (await r.json()).id;
  }

  async function updateFile(id, blob) {
    await api(`/upload/drive/v3/files/${id}?uploadType=media`, {
      method: "PATCH", headers: { "Content-Type": blob.type || "application/octet-stream" }, body: blob,
    });
  }

  // XHR, not fetch: Drive answers unfinished chunks with 308, and XHR reports upload progress.
  function putChunk(url, blob, range, onSent, signal) {
    return new Promise((resolve, reject) => {
      const x = new XMLHttpRequest();
      x.open("PUT", url);
      x.setRequestHeader("Content-Range", range);
      if (onSent) x.upload.onprogress = (e) => onSent(e.loaded);
      x.onload = () => resolve({ status: x.status, body: x.responseText, range: x.getResponseHeader("Range") });
      x.onerror = () => resolve({ status: 0 });
      if (signal) {
        if (signal.aborted) return reject(new DOMException("Cancelled", "AbortError"));
        signal.addEventListener("abort", () => { x.abort(); reject(new DOMException("Cancelled", "AbortError")); }, { once: true });
      }
      x.send(blob);
    });
  }

  async function uploadLarge(meta, file, onProgress, signal) {
    const type = meta.mimeType || file.type || "application/octet-stream";
    const start = await api("/upload/drive/v3/files?uploadType=resumable&fields=id", {
      method: "POST",
      headers: {
        "Content-Type": "application/json; charset=UTF-8",
        "X-Upload-Content-Type": type,
        "X-Upload-Content-Length": String(file.size),
      },
      body: JSON.stringify(meta),
      signal,
    });
    const session = start.headers.get("Location");
    if (!session) throw new Error("Google Drive didn’t return an upload address.");
    let pos = 0, failures = 0;
    for (;;) {
      const end = Math.min(pos + CHUNK, file.size);
      const from = pos;
      let queried = false;
      let res = await putChunk(session, file.slice(from, end), `bytes ${from}-${end - 1}/${file.size}`,
        (sent) => onProgress && onProgress((from + sent) / file.size), signal);
      if (res.status === 0 || res.status >= 500) {
        // Network hiccup: wait, then ask Drive how much it already has.
        if (++failures > 6) throw new Error("Upload kept failing. Check the connection and try again.");
        await sleep(1000 * 2 ** failures);
        res = await putChunk(session, null, `bytes */${file.size}`, null, signal);
        queried = true;
        if (res.status === 0 || res.status >= 500) continue;
      } else failures = 0;
      if (res.status === 200 || res.status === 201) { if (onProgress) onProgress(1); return JSON.parse(res.body).id; }
      if (res.status === 308) {
        // Range ("bytes=0-N") says how much Drive has. Without it, assume the chunk we
        // just sent arrived, or after a status check, resend the same chunk.
        pos = res.range ? parseInt(res.range.split("-")[1], 10) + 1 : (queried ? from : end);
        continue;
      }
      throw new Error(`Upload failed (${res.status}): ${(res.body || "").slice(0, 200)}`);
    }
  }

  // appProperties allow 124 bytes for key + value together.
  function clip(s, bytes) {
    s = String(s || "");
    const te = new TextEncoder();
    while (te.encode(s).length > bytes) s = s.slice(0, -1);
    return s;
  }

  // ---------------------------------------------------------------- books

  /** Upload an audiobook and its .sync.json to the Read-along folder. */
  async function addBook(audioFile, syncFile, { author = "", onProgress, signal } = {}) {
    const syncText = await syncFile.text();
    let data;
    try { data = JSON.parse(syncText); } catch { throw new Error("That sync file isn’t valid JSON. Choose the .sync.json from book_sync.py."); }
    if (!Array.isArray(data.lines)) throw new Error("That JSON has no “lines”. Choose the .sync.json from book_sync.py.");
    const title = data.title || syncFile.name.replace(/\.(sync\.)?json$/i, "");
    const parent = await folder();
    const audioId = await uploadLarge(
      { name: audioFile.name, parents: [parent], mimeType: audioType(audioFile.name, audioFile.type), appProperties: { ra: "audio" } },
      audioFile, onProgress, signal);
    // The book entry is written last, so a cancelled upload never shows up as a book.
    return createFile({
      name: syncFile.name, parents: [parent], mimeType: "application/json",
      appProperties: {
        ra: "book", audio: audioId, title: clip(title, 110), author: clip(author, 110),
        duration: String(Math.round(data.duration || 0)),
      },
    }, new Blob([syncText], { type: "application/json" }));
  }

  /** Books in the Read-along folder. ids look like "drive:<sync file id>". */
  async function listBooks() {
    const files = await findFiles(tagged("book"));
    return files.map((f) => {
      const p = f.appProperties || {};
      return {
        id: "drive:" + f.id, title: p.title || f.name, author: p.author || "",
        duration: +p.duration || 0, drive: { sync: f.id, audio: p.audio },
      };
    });
  }

  const cacheKey = (fileId) => new URL("offline/" + fileId, location.href).href;
  async function cached(fileId) {
    try { const r = await (await caches.open(BOOK_CACHE)).match(cacheKey(fileId)); return r ? r.blob() : null; }
    catch { return null; }
  }

  async function fetchFile(fileId, onProgress, signal, isAudio) {
    const hit = await cached(fileId);
    if (hit) return hit;
    const meta = await (await api(`/drive/v3/files/${fileId}?fields=name,size,mimeType`, { signal })).json();
    const r = await api(`/drive/v3/files/${fileId}?alt=media`, { signal });
    const stored = meta.mimeType || r.headers.get("Content-Type") || "application/octet-stream";
    const type = isAudio ? audioType(meta.name, stored) : stored;
    const total = +meta.size || 0;
    let got = 0;
    const counted = r.body.pipeThrough(new TransformStream({
      transform(chunk, ctl) { got += chunk.length; if (onProgress && total) onProgress(got / total); ctl.enqueue(chunk); },
    }));
    // Stream straight into Cache Storage (on disk) rather than holding a whole book in memory.
    let cache = null;
    try { cache = await caches.open(BOOK_CACHE); } catch {}
    if (!cache) return new Response(counted).blob().then((b) => new Blob([b], { type }));
    await cache.put(cacheKey(fileId), new Response(counted, { headers: { "Content-Type": type } }));
    return (await cache.match(cacheKey(fileId))).blob();
  }

  /** Download (or read from the offline copy) a Drive book: { sync: text, audio: Blob }. */
  async function getBook(book, { onProgress, signal } = {}) {
    const sync = await (await fetchFile(book.drive.sync, null, signal)).text();
    const audio = await fetchFile(book.drive.audio, onProgress, signal, true);
    return { sync, audio };
  }

  async function isDownloaded(book) {
    try {
      const c = await caches.open(BOOK_CACHE);
      return !!(await c.match(cacheKey(book.drive.audio)));
    } catch { return false; }
  }

  async function removeDownload(book) {
    try {
      const c = await caches.open(BOOK_CACHE);
      await c.delete(cacheKey(book.drive.audio));
      await c.delete(cacheKey(book.drive.sync));
    } catch {}
  }

  // ---------------------------------------------------------------- reading positions

  let progressId = null;
  async function progressFile() {
    if (progressId) return progressId;
    const found = await findFiles(tagged("progress"), "id");
    if (found.length) return (progressId = found[0].id);
    progressId = await createFile({
      name: "progress.json", parents: [await folder()], mimeType: "application/json",
      appProperties: { ra: "progress" },
    }, new Blob(['{"books":{}}'], { type: "application/json" }));
    return progressId;
  }

  async function readProgress() {
    const r = await api(`/drive/v3/files/${await progressFile()}?alt=media`, { cache: "no-store" });
    try { const d = await r.json(); d.books = d.books || {}; return d; } catch { return { books: {} }; }
  }

  /** The synced position for a book key ({pos, at, ...}), or null. Never throws. */
  async function getPosition(key, timeoutMs = 4000) {
    if (!signedIn()) return null;
    try {
      return await Promise.race([
        readProgress().then((d) => d.books[key] || null),
        sleep(timeoutMs).then(() => null),
      ]);
    } catch (e) { console.warn("Read-along: couldn’t read synced position", e); return null; }
  }

  const pending = new Map();
  let timer = 0, chain = Promise.resolve();

  async function flush() {
    if (!pending.size) return;
    const batch = new Map(pending);
    pending.clear();
    try {
      const d = await readProgress();
      for (const [k, v] of batch) if (!d.books[k] || (d.books[k].at || 0) <= v.at) d.books[k] = v;
      await updateFile(await progressFile(), new Blob([JSON.stringify(d)], { type: "application/json" }));
    } catch (e) {
      for (const [k, v] of batch) if (!pending.has(k)) pending.set(k, v);
      console.warn("Read-along: couldn’t sync position", e);
    }
  }

  /**
   * Remember a position ({pos, at, speed, offset}) in Drive. Writes at most every
   * 30 seconds; pass urgent (on pause, on leaving) to write now.
   */
  function savePosition(key, rec, urgent) {
    if (!signedIn()) return Promise.resolve();
    pending.set(key, rec);
    if (urgent) {
      clearTimeout(timer); timer = 0;
      return (chain = chain.then(flush));
    }
    if (!timer) timer = setTimeout(() => { timer = 0; chain = chain.then(flush); }, 30000);
    return chain;
  }

  function signedIn() { return !!(ls.get(TOKEN_KEY) || {}).refresh; }

  window.ReadalongCloud = {
    enabled: !!CFG.clientId,
    audioType,
    signedIn, startSignIn, signOut,
    listBooks, addBook, getBook, isDownloaded, removeDownload,
    getPosition, savePosition,
  };
})();
