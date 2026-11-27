/* abs.js - optional Audiobookshelf support for Read-along, plus the multi-file player.
 *
 * Audiobookshelf (audiobookshelf.org) is a self-hosted audiobook server. With it connected:
 * - Player mode: books on your server that have a sync file play here, streamed from the
 *   server, and your place is saved back to Audiobookshelf (so its own apps pick up there).
 * - Follow mode: you listen in the Audiobookshelf app; this page asks the server where you
 *   are and shows the text in step, like synced lyrics for a song playing on another device.
 *
 * Sync files for Audiobookshelf books are kept in Google Drive (cloud.js), made by the PC
 * sync helper (tools/sync_worker.py). Settings (server address + API key) are kept in this
 * browser, and optionally in your Drive so the glasses can pick them up without typing.
 *
 * The server must allow this site: Audiobookshelf Settings -> "Allowed CORS Origins".
 * Requests send the API key as ?token=, because browsers won't let an Authorization header
 * through Audiobookshelf's CORS setup (it answers Access-Control-Allow-Headers: *).
 *
 * TrackAudio (also here, used by both pages for every book) plays a list of audio files
 * as one continuous book, like an <audio> element with a single timeline.
 */
(() => {
  const ls = {
    get(k) { try { return JSON.parse(localStorage.getItem(k)); } catch { return null; } },
    set(k, v) { try { localStorage.setItem(k, JSON.stringify(v)); } catch {} },
    del(k) { try { localStorage.removeItem(k); } catch {} },
  };
  const KEY = "readalong:abs";
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

  // ---------------------------------------------------------------- TrackAudio

  const FORWARD = ["play", "pause", "seeked", "seeking", "loadedmetadata", "durationchange", "timeupdate",
                   "error", "ended", "ratechange", "waiting", "playing", "canplay"];

  /**
   * Wraps an <audio> element. With a single source (audio.src = url) it behaves exactly like
   * the element. With setTracks([{url, start, duration}]) it plays the files back to back:
   * currentTime and duration cover the whole book, and seeking crosses files as needed.
   */
  class TrackAudio extends EventTarget {
    constructor(el) {
      super();
      this.el = el;
      this.tracks = null;
      this.i = 0;
      this.switching = null;     // {offset, play} while a different file loads
      for (const type of FORWARD) el.addEventListener(type, () => this._on(type));
    }

    _on(type) {
      if (this.tracks) {
        if (type === "ended" && this.i < this.tracks.length - 1) return this._go(this.i + 1, 0, true);
        if (this.switching) {
          if (type === "loadedmetadata") {
            const s = this.switching;
            this.el.playbackRate = this.el.defaultPlaybackRate = this.rate || 1;
            if (s.offset > 0.05) this.el.currentTime = s.offset;
            if (s.play) this.el.play().catch(() => {});
            if (s.offset <= 0.05) this._switched();
            return;
          }
          if (type === "seeked") return this._switched();
          if (type === "error") this.switching = null;
          else return;   // hide the pause/play/emptied noise of changing files
        }
        // A file ending fires "pause" just before "ended"; the book itself plays on.
        if (type === "pause" && this.el.ended && this.i < this.tracks.length - 1) return;
      }
      this.dispatchEvent(new Event(type));
    }

    _switched() {
      this.switching = null;
      this.dispatchEvent(new Event("seeked"));
    }

    _go(i, offset, play) {
      this.i = i;
      this.switching = { offset, play };
      this.el.src = this.tracks[i].url;
    }

    /** Play these files as one book: [{url, start, duration}] in order (start/duration in seconds). */
    setTracks(tracks) {
      this.tracks = tracks.map((t) => ({ ...t }));
      this.i = 0;
      this.switching = null;
      this.el.src = this.tracks[0].url;
    }

    get src() { return this.el.src; }
    set src(v) { this.tracks = null; this.switching = null; this.el.src = v; }
    removeAttribute(name) { this.tracks = null; this.switching = null; this.el.removeAttribute(name); }
    load() { this.el.load(); }

    get duration() {
      if (!this.tracks) return this.el.duration;
      const last = this.tracks[this.tracks.length - 1];
      return last.start + (last.duration || 0);
    }
    get currentTime() {
      if (!this.tracks) return this.el.currentTime;
      const base = this.tracks[this.i].start;
      return base + (this.switching ? this.switching.offset : this.el.currentTime);
    }
    set currentTime(t) {
      if (!this.tracks) { this.el.currentTime = t; return; }
      t = Math.max(0, Math.min(t, this.duration));
      let j = 0;
      while (j < this.tracks.length - 1 && this.tracks[j + 1].start <= t) j++;
      const offset = t - this.tracks[j].start;
      if (j === this.i && !this.switching) { this.el.currentTime = offset; return; }
      if (j === this.i && this.switching) { this.switching.offset = offset; return; }
      this._go(j, offset, this.switching ? this.switching.play : !this.el.paused);
    }
    get paused() { return this.switching ? !this.switching.play : this.el.paused; }
    get readyState() { return this.switching ? 0 : this.el.readyState; }
    get playbackRate() { return this.el.playbackRate; }
    set playbackRate(v) { this.rate = v; this.el.playbackRate = v; this.el.defaultPlaybackRate = v; }
    get error() { return this.el.error; }
    play() {
      if (this.switching) { this.switching.play = true; return Promise.resolve(); }
      return this.el.play();
    }
    pause() {
      if (this.switching) {
        if (this.switching.play) { this.switching.play = false; this.el.pause(); this.dispatchEvent(new Event("pause")); }
        return;
      }
      this.el.pause();
    }
  }

  // ---------------------------------------------------------------- settings

  /** {url, token, user} or null. url is the server's base, e.g. https://host/audiobookshelf */
  const settings = () => ls.get(KEY);
  const configured = () => !!(settings() || {}).token;

  // People paste the address bar, e.g. https://host/audiobookshelf/library/<id>. Keep the base.
  function baseCandidates(input) {
    let u;
    try { u = new URL(/^https?:\/\//i.test(input) ? input : "https://" + input); }
    catch { throw new Error("That doesn’t look like a web address."); }
    const parts = u.pathname.split("/").filter(Boolean);
    const cut = parts.findIndex((p) => /^(library|item|items|config|author|authors|series|collection|collections|playlist|playlists|login|account|audiobook|share|stats|batch)$/i.test(p));
    const keep = cut >= 0 ? parts.slice(0, cut) : parts;
    const out = [];
    // No "http(s)://" typed: try https, and http too when this page is itself on http (local testing).
    const origins = [u.origin];
    if (!/^https?:\/\//i.test(input) && location.protocol === "http:") origins.push(u.origin.replace(/^https:/, "http:"));
    for (const origin of origins) {
      for (let n = keep.length; n >= 0; n--) out.push(origin + (n ? "/" + keep.slice(0, n).join("/") : ""));
    }
    return out;
  }

  const corsHelp = (base) => new Error(
    `Couldn’t reach Audiobookshelf at ${base}. If the address is right, add ${location.origin} ` +
    `in Audiobookshelf under Settings → Allowed CORS Origins, then try again.`);

  /** Check the address and API key, and remember them. Returns the saved settings. */
  async function connect(address, token) {
    token = String(token || "").trim();
    if (!token) throw new Error("Paste an API key from Audiobookshelf (Settings → API Keys).");
    let base = null, lastErr = null;
    for (const cand of baseCandidates(String(address || "").trim())) {
      try {
        const r = await fetch(cand + "/status", { cache: "no-store" });
        const d = await r.json();
        if (d && d.app === "audiobookshelf") { base = cand; break; }
      } catch (e) { lastErr = e; }
    }
    if (!base) throw lastErr ? corsHelp(address) : new Error("No Audiobookshelf server found at that address.");
    let r;
    try { r = await fetch(`${base}/api/me?token=${encodeURIComponent(token)}`, { cache: "no-store" }); }
    catch { throw corsHelp(base); }
    if (r.status === 401 || r.status === 403) throw new Error("Audiobookshelf didn’t accept that API key.");
    if (!r.ok) throw new Error(`Audiobookshelf answered ${r.status}.`);
    const me = await r.json();
    const s = { url: base, token, user: me.username || "" };
    ls.set(KEY, s);
    return s;
  }

  /** Use settings that came from elsewhere (e.g. the copy in Google Drive). */
  function adopt(s) { if (s && s.url && s.token) ls.set(KEY, { url: s.url, token: s.token, user: s.user || "" }); }
  function disconnect() { ls.del(KEY); }
  const host = () => { try { return new URL(settings().url).host; } catch { return ""; } };

  // ---------------------------------------------------------------- API

  function withToken(url) {
    const s = settings();
    return url + (url.includes("?") ? "&" : "?") + "token=" + encodeURIComponent(s.token);
  }

  async function api(path, opts = {}) {
    const s = settings();
    if (!s) throw Object.assign(new Error("Audiobookshelf isn’t connected."), { notConnected: true });
    let r;
    try { r = await fetch(withToken(s.url + path), Object.assign({ cache: "no-store" }, opts)); }
    catch { throw corsHelp(s.url); }
    if (r.status === 401) throw Object.assign(new Error("Audiobookshelf no longer accepts the API key. Connect again."), { badKey: true });
    if (!r.ok) throw Object.assign(new Error(`Audiobookshelf error ${r.status}`), { status: r.status });
    return r;
  }
  const getJSON = async (path) => (await api(path)).json();

  /** Audiobooks on the server: [{id, title, author, duration, tracks: n, ebook: "epub"|...|null}] */
  async function listBooks() {
    const { libraries } = await getJSON("/api/libraries");
    const out = [];
    for (const lib of libraries || []) {
      if (lib.mediaType && lib.mediaType !== "book") continue;
      for (let page = 0; page < 50; page++) {
        const d = await getJSON(`/api/libraries/${lib.id}/items?minified=1&limit=500&page=${page}&sort=media.metadata.title`);
        for (const it of d.results || []) {
          const m = it.media || {};
          if (!m.numTracks && !m.duration) continue;           // ebook-only items
          out.push({ id: it.id, title: (m.metadata && m.metadata.title) || "Untitled",
                     author: (m.metadata && m.metadata.authorName) || "", duration: m.duration || 0,
                     tracks: m.numTracks || 0, ebook: m.ebookFormat || null });
        }
        if (!d.results || d.results.length < 500) break;
      }
    }
    return out;
  }

  /** One book with its audio files as playable tracks. */
  async function getItem(id) {
    const it = await getJSON(`/api/items/${encodeURIComponent(id)}?expanded=1`);
    const m = it.media || {};
    const s = settings();
    const tracks = (m.tracks || []).map((t) => ({
      url: withToken(s.url + t.contentUrl), start: t.startOffset || 0, duration: t.duration || 0,
    }));
    return { id: it.id, title: (m.metadata && m.metadata.title) || "", author: (m.metadata && m.metadata.authorName) || "",
             duration: m.duration || tracks.reduce((a, t) => a + t.duration, 0), tracks,
             ebook: m.ebookFile ? (m.ebookFile.ebookFormat || "") : null };
  }

  /** Your place in a book: {pos, at (ms), duration, finished} or null. */
  async function getProgress(id) {
    try {
      const p = await getJSON(`/api/me/progress/${encodeURIComponent(id)}`);
      return { pos: p.currentTime || 0, at: p.lastUpdate || 0, duration: p.duration || 0, finished: !!p.isFinished };
    } catch (e) {
      if (e.status === 404) return null;
      throw e;
    }
  }

  // Saves go out at most every 15 s, and right away when urgent (pause, leaving).
  const pending = new Map();
  let timer = 0;
  async function flush() {
    const batch = [...pending.entries()];
    pending.clear();
    for (const [id, p] of batch) {
      try {
        await api(`/api/me/progress/${encodeURIComponent(id)}`, {
          method: "PATCH", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ currentTime: p.pos, duration: p.duration, progress: p.duration ? Math.min(1, p.pos / p.duration) : 0 }),
          keepalive: true,
        });
      } catch (e) {
        if (!pending.has(id)) pending.set(id, p);
        console.warn("Read-along: couldn’t save the position to Audiobookshelf", e);
      }
    }
  }
  function saveProgress(id, pos, duration, urgent) {
    if (!configured()) return;
    pending.set(id, { pos, duration });
    if (urgent) { clearTimeout(timer); timer = 0; flush(); return; }
    if (!timer) timer = setTimeout(() => { timer = 0; flush(); }, 15000);
  }

  // ---------------------------------------------------------------- follow mode

  /**
   * Follow what you're listening to in the Audiobookshelf app.
   *
   * The server only hears from the app every so often (about every 10-15 s while playing),
   * so between reports the position is counted forward at the speed the reports imply.
   * The server's clock and this device's may differ; each report's arrival narrows down
   * the difference. Returns {stop(), now() -> {item, pos, playing, rate, at} | null}.
   * onChange(state) fires when the item or play/pause state changes.
   */
  function follow({ onChange, interval = 2500 } = {}) {
    let stopped = false;
    let item = null, anchor = null, playing = false, rate = 1;
    let lastSeen = null;              // the previous report {pos, at} for this item
    let skewLo = -Infinity, skewHi = Infinity;   // local clock - server clock, in ms
    let prevPoll = 0;
    let gap = 15000;                  // typical time between reports while playing
    let lastSig = "";

    // Clocks are usually set from the internet, so assume they agree unless the reports'
    // arrival times prove otherwise; then use the nearest offset that fits.
    const skew = () => Math.min(Math.max(0, skewLo), skewHi);

    function emit() {
      const sig = `${item}|${playing}`;
      if (sig !== lastSig) { lastSig = sig; if (onChange) onChange(now()); }
    }

    function now() {
      if (!item || !anchor) return null;
      const t = Date.now();
      const stale = t - anchor.local > Math.max(6000, gap * 1.3);
      const live = playing && !stale;
      const pos = live ? anchor.pos + rate * (t - anchor.local) / 1000 : anchor.pos;
      return { item, pos, playing: live, rate, at: anchor.at };
    }

    async function poll() {
      const t0 = Date.now();
      const me = await getJSON("/api/me");
      const t1 = Date.now();
      const list = (me.mediaProgress || []).filter((p) => p.libraryItemId && !p.episodeId && p.lastUpdate);
      list.sort((a, b) => b.lastUpdate - a.lastUpdate);
      const p = list[0];
      if (!p) return;
      const seen = { pos: p.currentTime || 0, at: p.lastUpdate };
      if (p.libraryItemId !== item) {
        item = p.libraryItemId;
        lastSeen = null;
        playing = false;
        anchor = { pos: seen.pos, at: seen.at, local: seen.at + skew() };
        // Not "playing" until a second report shows the position moving.
        prevPoll = t0;
        lastSeen = seen;
        emit();
        return;
      }
      if (lastSeen && seen.at !== lastSeen.at) {
        // This report reached the server after the previous poll began and before this one ended.
        if (prevPoll) skewLo = Math.max(skewLo, prevPoll - seen.at - 300);
        skewHi = Math.min(skewHi, t1 - seen.at);
        if (skewLo > skewHi) { skewLo = -Infinity; skewHi = t1 - seen.at; }   // clock jumped; start over
        const dt = (seen.at - lastSeen.at) / 1000;
        const dp = seen.pos - lastSeen.pos;
        if (Math.abs(dp) < 0.5) playing = false;                       // reported, but not moving
        else if (dt > 1.5 && dp > 0 && dp / dt > 0.4 && dp / dt < 4) {
          playing = true;
          rate = Math.round((dp / dt) / 0.05) * 0.05;
          gap = gap * 0.7 + dt * 1000 * 0.3;
        } else playing = true;                                          // a jump: a seek, carry on
        anchor = { pos: seen.pos, at: seen.at, local: seen.at + skew() };
        lastSeen = seen;
      } else if (!lastSeen) lastSeen = seen;
      prevPoll = t0;
      emit();
    }

    (async () => {
      while (!stopped) {
        try { await poll(); } catch (e) { console.warn("Read-along: follow", e); }
        await sleep(document.hidden ? interval * 4 : interval);
      }
    })();
    const ticker = setInterval(emit, 1000);   // notices when playback goes quiet
    return { stop() { stopped = true; clearInterval(ticker); }, now };
  }

  window.ReadalongAbs = {
    TrackAudio, trackAudio: (el) => new TrackAudio(el),
    settings, configured, connect, adopt, disconnect, host,
    listBooks, getItem, getProgress, saveProgress, follow,
  };
})();
