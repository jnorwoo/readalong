#!/usr/bin/env python3
"""
book_sync.py - make a read-along file from an audiobook and its ebook.

It transcribes the audio with WhisperX (word-level timestamps), then lines the
transcript up against the ebook's real text, so the output uses the book's own
words, spelling and punctuation instead of the transcription.

Output (next to --out, default: named after the book):
  <name>.lrc        plain LRC, one timed line per display line
  <name>.sync.json  lines + per-word timings + chapters (used by player.html)

Usage:
  python book_sync.py "Book.m4b" --book "Book.epub"
  python book_sync.py part1.mp3 part2.mp3 part3.mp3 --book Book.epub --language en

Works only with DRM-free files you own (EPUB/TXT and MP3/M4A/M4B/etc).
Requires ffmpeg on your PATH and `pip install whisperx`.
"""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import shutil
import subprocess
import sys
import unicodedata
import zipfile
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from html.parser import HTMLParser
from pathlib import Path
from posixpath import dirname as posix_dirname, join as posix_join, normpath as posix_normpath
from xml.etree import ElementTree as ET


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------

def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def need_tool(name: str) -> None:
    if shutil.which(name) is None:
        sys.exit(f"error: '{name}' was not found on your PATH. Install ffmpeg first "
                 f"(macOS: brew install ffmpeg, Windows: winget install ffmpeg, "
                 f"Linux: apt install ffmpeg).")


def audio_duration(path: Path) -> float:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    return float(out)


def fmt_lrc_time(t: float) -> str:
    t = max(0.0, t)
    minutes = int(t // 60)
    seconds = t - minutes * 60
    return f"{minutes:02d}:{seconds:05.2f}"


_norm_strip = re.compile(r"[^0-9a-z]+")


def norm_word(w: str) -> str:
    """Lowercase, strip accents and punctuation, so 'Don't,' == 'dont'."""
    w = unicodedata.normalize("NFKD", w)
    w = "".join(c for c in w if not unicodedata.combining(c))
    w = w.lower().replace("’", "").replace("'", "")
    return _norm_strip.sub("", w)


# --------------------------------------------------------------------------
# Ebook loading
# --------------------------------------------------------------------------

@dataclass
class Token:
    text: str            # word as printed in the book (with punctuation)
    norm: str            # normalised form used for matching ("" = never matched)
    para: int            # paragraph number
    chapter: int         # chapter number
    start: float | None = None
    end: float | None = None
    matched: bool = False


@dataclass
class Book:
    title: str
    tokens: list[Token] = field(default_factory=list)
    chapters: list[str] = field(default_factory=list)


BLOCK_TAGS = {"p", "div", "h1", "h2", "h3", "h4", "h5", "h6", "li", "blockquote",
              "section", "article", "br", "tr", "dd", "dt", "pre", "figcaption"}
HEADING_TAGS = {"h1", "h2", "h3"}
SKIP_TAGS = {"script", "style", "head", "title", "nav", "rt", "rp", "sup"}


class _XHTMLText(HTMLParser):
    """Collects paragraphs and the first heading from one EPUB content file."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.paras: list[str] = []
        self.heading: str | None = None
        self._buf: list[str] = []
        self._skip = 0
        self._in_heading = 0
        self._heading_buf: list[str] = []

    def _flush(self) -> None:
        text = " ".join("".join(self._buf).split())
        if text:
            self.paras.append(text)
        self._buf = []

    def handle_starttag(self, tag, attrs):
        if tag in SKIP_TAGS:
            self._skip += 1
        elif tag in BLOCK_TAGS:
            self._flush()
        if tag in HEADING_TAGS:
            self._in_heading += 1

    def handle_endtag(self, tag):
        if tag in SKIP_TAGS:
            self._skip = max(0, self._skip - 1)
        elif tag in BLOCK_TAGS:
            self._flush()
        if tag in HEADING_TAGS and self._in_heading:
            self._in_heading -= 1
            if self.heading is None:
                h = " ".join("".join(self._heading_buf).split())
                if h:
                    self.heading = h
            self._heading_buf = []

    def handle_data(self, data):
        if self._skip:
            return
        self._buf.append(data)
        if self._in_heading:
            self._heading_buf.append(data)

    def close(self):
        super().close()
        self._flush()


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def load_epub(path: Path) -> tuple[str, list[tuple[str | None, list[str]]]]:
    """Return (title, [(heading, paragraphs) per spine document])."""
    with zipfile.ZipFile(path) as z:
        names = set(z.namelist())
        if "META-INF/encryption.xml" in names:
            enc = z.read("META-INF/encryption.xml").decode("utf-8", "ignore")
            # Font obfuscation is harmless; anything else means DRM.
            if "EncryptedData" in enc and "obfuscation" not in enc.lower():
                sys.exit("error: this EPUB is DRM-protected. Use a DRM-free copy.")
        container = ET.fromstring(z.read("META-INF/container.xml"))
        opf_path = next(el.attrib["full-path"] for el in container.iter()
                        if _local(el.tag) == "rootfile")
        opf = ET.fromstring(z.read(opf_path))
        base = posix_dirname(opf_path)

        title = path.stem
        for el in opf.iter():
            if _local(el.tag) == "title" and (el.text or "").strip():
                title = el.text.strip()
                break

        manifest = {}
        for el in opf.iter():
            if _local(el.tag) == "item":
                manifest[el.attrib.get("id")] = el.attrib.get("href", "")
        spine_ids = [el.attrib.get("idref") for el in opf.iter() if _local(el.tag) == "itemref"
                     and el.attrib.get("linear", "yes") != "no"]

        docs = []
        for sid in spine_ids:
            href = manifest.get(sid)
            if not href:
                continue
            full = posix_normpath(posix_join(base, html.unescape(href).split("#")[0]))
            if full not in names:
                continue
            parser = _XHTMLText()
            parser.feed(z.read(full).decode("utf-8", "ignore"))
            parser.close()
            if parser.paras:
                docs.append((parser.heading, parser.paras))
        return title, docs


CHAPTER_LINE = re.compile(r"^\s*(chapter|part|book|prologue|epilogue|interlude)\b.{0,60}$", re.I)


def load_text(path: Path) -> tuple[str, list[tuple[str | None, list[str]]]]:
    raw = path.read_text(encoding="utf-8", errors="ignore")
    paras = [" ".join(p.split()) for p in re.split(r"\n\s*\n", raw)]
    paras = [p for p in paras if p]
    docs: list[tuple[str | None, list[str]]] = [(None, [])]
    for p in paras:
        if CHAPTER_LINE.match(p):
            docs.append((p, [p]))
        else:
            docs[-1][1].append(p)
    return path.stem, [d for d in docs if d[1]]


PG_START = re.compile(r"\*\*\*\s*START OF (THE|THIS) PROJECT GUTENBERG", re.I)
PG_END = re.compile(r"\*\*\*\s*END OF (THE|THIS) PROJECT GUTENBERG", re.I)


def strip_gutenberg(docs: list[tuple[str | None, list[str]]]) -> list[tuple[str | None, list[str]]]:
    """Drop Project Gutenberg's header and license text, which audiobooks don't narrate."""
    flat = [(d, i, p) for d, (_, paras) in enumerate(docs) for i, p in enumerate(paras)]
    starts = [k for k, (_, _, p) in enumerate(flat) if PG_START.search(p)]
    if not starts:
        return docs
    ends = [k for k, (_, _, p) in enumerate(flat) if PG_END.search(p) and k > starts[0]]
    keep = set(range(starts[0] + 1, ends[0] if ends else len(flat)))
    out = []
    for d, (heading, paras) in enumerate(docs):
        kept = [p for k, (dd, i, p) in enumerate(flat) if dd == d and k in keep]
        if kept:
            out.append((heading if heading and not PG_START.search(heading) else None, kept))
    return out


def load_book(path: Path) -> Book:
    ext = path.suffix.lower()
    if ext == ".epub":
        title, docs = load_epub(path)
    elif ext in {".txt", ".md", ".text"}:
        title, docs = load_text(path)
    else:
        sys.exit(f"error: unsupported book format '{ext}'. Use .epub or .txt "
                 f"(convert other formats with Calibre first).")

    docs = strip_gutenberg(docs)
    book = Book(title=title)
    para_no = 0
    for ch_no, (heading, paras) in enumerate(docs):
        book.chapters.append(heading or f"Section {ch_no + 1}")
        for p in paras:
            for w in p.split():
                book.tokens.append(Token(text=w, norm=norm_word(w), para=para_no, chapter=ch_no))
            para_no += 1
    return book


# --------------------------------------------------------------------------
# Transcription (WhisperX), chunked and cached
# --------------------------------------------------------------------------

@dataclass
class Word:
    norm: str
    start: float | None
    end: float | None


def extract_chunk(src: Path, start: float, length: float, dest: Path) -> None:
    subprocess.run(
        ["ffmpeg", "-nostdin", "-loglevel", "error", "-y",
         "-ss", f"{max(0.0, start):.3f}", "-t", f"{length:.3f}", "-i", str(src),
         "-vn", "-ac", "1", "-ar", "16000", "-f", "wav", str(dest)],
        check=True,
    )


class Transcriber:
    def __init__(self, model_name: str | None, device: str, language: str | None,
                 batch_size: int, compute_type: str | None) -> None:
        try:
            import whisperx  # noqa: F401
        except ImportError:
            sys.exit("error: whisperx is not installed. Run: pip install whisperx")
        import torch
        import whisperx

        if device == "auto":
            device = "cuda" if torch.cuda.is_available() else "cpu"
        self.device = device
        self.model_name = model_name or ("large-v3" if device == "cuda" else "small")
        self.compute_type = compute_type or ("float16" if device == "cuda" else "int8")
        self.batch_size = batch_size
        self.language = language
        self.wx = whisperx
        log(f"loading WhisperX model '{self.model_name}' on {device} ({self.compute_type})...")
        self.model = whisperx.load_model(self.model_name, device,
                                         compute_type=self.compute_type, language=language)
        self.align_model = None
        self.align_meta = None

    def transcribe(self, wav: Path) -> list[dict]:
        audio = self.wx.load_audio(str(wav))
        result = self.model.transcribe(audio, batch_size=self.batch_size,
                                       language=self.language)
        lang = result.get("language") or self.language or "en"
        if self.language is None:
            self.language = lang  # detect once, reuse for every later chunk
            log(f"detected language: {lang}")
        if self.align_model is None:
            self.align_model, self.align_meta = self.wx.load_align_model(
                language_code=lang, device=self.device)
        aligned = self.wx.align(result["segments"], self.align_model, self.align_meta,
                                audio, self.device, return_char_alignments=False)
        return aligned.get("word_segments", [])


def transcribe_all(audio_files: list[Path], offsets: list[float], durations: list[float],
                   cache_dir: Path, args) -> list[Word]:
    cache_dir.mkdir(parents=True, exist_ok=True)
    chunk_len = args.chunk_minutes * 60.0
    overlap = 2.0
    transcriber: Transcriber | None = None
    words: list[Word] = []

    for f_idx, (src, offset, dur) in enumerate(zip(audio_files, offsets, durations)):
        n_chunks = max(1, int((dur + chunk_len - 1e-6) // chunk_len))
        for c in range(n_chunks):
            keep_from = c * chunk_len
            keep_to = min(dur, (c + 1) * chunk_len)
            cut_from = max(0.0, keep_from - overlap)
            cache = cache_dir / f"file{f_idx:02d}_chunk{c:03d}.json"
            label = f"[file {f_idx + 1}/{len(audio_files)} chunk {c + 1}/{n_chunks}]"

            if cache.exists():
                segs = json.loads(cache.read_text())
                log(f"{label} cached")
            else:
                if transcriber is None:
                    transcriber = Transcriber(args.model, args.device, args.language,
                                              args.batch_size, args.compute_type)
                wav = cache_dir / "current_chunk.wav"
                log(f"{label} {fmt_lrc_time(keep_from)}-{fmt_lrc_time(keep_to)} transcribing...")
                extract_chunk(src, cut_from, keep_to - cut_from, wav)
                raw = transcriber.transcribe(wav)
                segs = [{"w": w.get("word", ""),
                         "s": (w["start"] + cut_from) if w.get("start") is not None else None,
                         "e": (w["end"] + cut_from) if w.get("end") is not None else None}
                        for w in raw]
                cache.write_text(json.dumps(segs))
                wav.unlink(missing_ok=True)

            # Keep only words that start inside this chunk's own window, so the
            # 2s overlap never produces duplicates.
            last_start = keep_from
            for w in segs:
                s = w["s"] if w["s"] is not None else last_start
                if s < keep_from or s >= keep_to + (0.01 if c == n_chunks - 1 else 0):
                    continue
                last_start = s
                n = norm_word(w["w"])
                if n:
                    words.append(Word(n,
                                      None if w["s"] is None else w["s"] + offset,
                                      None if w["e"] is None else w["e"] + offset))
    return words


# --------------------------------------------------------------------------
# Aligning transcript words to ebook words
# --------------------------------------------------------------------------

def align(book: Book, words: list[Word], chunk: int = 400) -> int:
    """Mark ebook tokens with times from matching transcript words. Returns match count."""
    e_idx = [i for i, t in enumerate(book.tokens) if t.norm]
    E = [book.tokens[i].norm for i in e_idx]
    T = [w.norm for w in words]
    ptr = 0
    lost = True          # start "lost" so the first chunk searches widely
    matched = 0

    for ts in range(0, len(T), chunk):
        tchunk = T[ts:ts + chunk]
        reach = 25000 if lost else int(len(tchunk) * 2.5) + 300
        wend = min(len(E), ptr + reach)
        if ptr >= wend:
            break
        sm = SequenceMatcher(None, E[ptr:wend], tchunk, autojunk=False)
        min_block = 4 if lost else 2
        hits = 0
        last_e = None
        prev_a, prev_b = (None, None) if lost else (0, 0)
        for b in sm.get_matching_blocks():
            if b.size < min_block:
                continue
            # A block that jumps much further ahead in the book than in the audio
            # is almost always a repeated phrase matching in the wrong place.
            if prev_a is not None and (b.a - prev_a) > 2 * max(0, b.b - prev_b) + 40:
                continue
            prev_a, prev_b = b.a + b.size, b.b + b.size
            for k in range(b.size):
                ei = e_idx[ptr + b.a + k]
                w = words[ts + b.b + k]
                tok = book.tokens[ei]
                if w.start is not None and w.end is not None:
                    tok.start, tok.end = w.start, w.end
                    tok.matched = True
                    hits += 1
            last_e = ptr + b.a + b.size
        if hits >= max(8, int(len(tchunk) * 0.15)) and last_e is not None:
            ptr = last_e
            lost = False
        else:
            lost = True   # e.g. audio credits/intro, or a section missing from the ebook
        matched += hits
    return matched - drop_out_of_order(book)


def drop_out_of_order(book: Book) -> int:
    """Keep the largest set of matches whose times rise with book order; unmatch the rest."""
    from bisect import bisect_right
    idx = [i for i, t in enumerate(book.tokens) if t.matched]
    tails: list[float] = []      # smallest tail start time for each run length
    tail_pos: list[int] = []
    parent = [-1] * len(idx)
    for j, i in enumerate(idx):
        s = book.tokens[i].start
        k = bisect_right(tails, s)
        if k == len(tails):
            tails.append(s); tail_pos.append(j)
        else:
            tails[k] = s; tail_pos[k] = j
        parent[j] = tail_pos[k - 1] if k else -1
    keep = set()
    j = tail_pos[-1] if tail_pos else -1
    while j != -1:
        keep.add(idx[j]); j = parent[j]
    dropped = 0
    for i in idx:
        if i not in keep:
            t = book.tokens[i]
            t.matched, t.start, t.end = False, None, None
            dropped += 1
    return dropped


def interpolate(book: Book, max_gap_words: int = 80) -> None:
    """Give unmatched words in short gaps times spread between their matched neighbours."""
    toks = book.tokens
    anchors = [i for i, t in enumerate(toks) if t.matched]
    for a, b in zip(anchors, anchors[1:]):
        gap = b - a - 1
        if gap <= 0:
            continue
        t0, t1 = toks[a].end, toks[b].start
        if gap > max_gap_words or t1 < t0 or (t1 - t0) > gap * 1.5 + 5:
            continue  # big hole: probably text that isn't narrated, leave untimed
        step = (t1 - t0) / (gap + 1)
        for k in range(1, gap + 1):
            tok = toks[a + k]
            tok.start = t0 + step * (k - 0.5)
            tok.end = t0 + step * (k + 0.5)


# --------------------------------------------------------------------------
# Building display lines
# --------------------------------------------------------------------------

SENT_END = re.compile(r"[.!?…]['\"”’)\]]*$")
SOFT_BREAK = re.compile(r"[,;:—–]['\"”’)\]]*$")


def build_lines(book: Book, max_words: int) -> list[dict]:
    lines: list[dict] = []
    cur: list[Token] = []

    def flush():
        nonlocal cur
        timed = [t for t in cur if t.start is not None]
        if timed:
            lines.append({
                "start": round(timed[0].start, 3),
                "end": round(timed[-1].end, 3),
                "text": " ".join(t.text for t in cur),
                "chapter": cur[0].chapter,
                "words": [{"w": t.text,
                           "s": None if t.start is None else round(t.start, 3),
                           "e": None if t.end is None else round(t.end, 3)} for t in cur],
            })
        cur = []

    toks = book.tokens
    for i, tok in enumerate(toks):
        cur.append(tok)
        nxt = toks[i + 1] if i + 1 < len(toks) else None
        n = len(cur)
        if nxt is None or nxt.para != tok.para:
            flush()
        elif SENT_END.search(tok.text) and n >= 4:
            flush()
        elif n >= max_words:
            flush()
        elif n >= int(max_words * 0.6) and SOFT_BREAK.search(tok.text):
            flush()

    # Keep line starts in order even if alignment jittered.
    for a, b in zip(lines, lines[1:]):
        if b["start"] < a["start"]:
            b["start"] = a["start"]
    return lines


def chapter_marks(book: Book, lines: list[dict]) -> list[dict]:
    seen = set()
    marks = []
    for ln in lines:
        ch = ln["chapter"]
        if ch not in seen:
            seen.add(ch)
            marks.append({"title": book.chapters[ch], "start": ln["start"]})
    return marks


# --------------------------------------------------------------------------
# Joining multi-file audiobooks
# --------------------------------------------------------------------------

def join_audio(files: list[Path], dest: Path) -> None:
    lst = dest.with_suffix(".concat.txt")
    lst.write_text("".join(f"file '{str(p.resolve()).replace(chr(39), chr(39) + chr(92) + chr(39) + chr(39))}'\n"
                           for p in files))
    try:
        same = len({p.suffix.lower() for p in files}) == 1
        cmd = ["ffmpeg", "-nostdin", "-loglevel", "error", "-y", "-f", "concat", "-safe", "0",
               "-i", str(lst), "-vn"]
        cmd += ["-c", "copy"] if same else ["-c:a", "aac", "-b:a", "96k"]
        subprocess.run(cmd + [str(dest)], check=True)
    finally:
        lst.unlink(missing_ok=True)


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description="Sync an ebook's text to its audiobook.")
    ap.add_argument("audio", nargs="+", type=Path, help="audiobook file(s), in reading order")
    ap.add_argument("--book", required=True, type=Path, help="ebook (.epub or .txt)")
    ap.add_argument("--out", type=Path, help="output path prefix (default: next to the book)")
    ap.add_argument("--model", help="Whisper model (default: large-v3 on GPU, small on CPU)")
    ap.add_argument("--language", help="language code, e.g. en (default: auto-detect)")
    ap.add_argument("--device", default="auto", help="cuda, cpu or auto")
    ap.add_argument("--compute-type", help="e.g. float16, int8 (default depends on device)")
    ap.add_argument("--batch-size", type=int, default=16)
    ap.add_argument("--chunk-minutes", type=float, default=20)
    ap.add_argument("--max-words", type=int, default=12,
                    help="max words per display line (smaller = better for glasses)")
    ap.add_argument("--cache-dir", type=Path, help="where transcripts are cached for resuming")
    args = ap.parse_args(argv)

    need_tool("ffmpeg")
    need_tool("ffprobe")
    for p in [*args.audio, args.book]:
        if not p.exists():
            sys.exit(f"error: file not found: {p}")
    for p in args.audio:
        if p.suffix.lower() in {".aax", ".aaxc"}:
            sys.exit(f"error: {p.name} is a DRM-protected Audible file. Use a DRM-free audio file.")

    out = args.out or args.book.with_suffix("")
    out.parent.mkdir(parents=True, exist_ok=True)
    cache_dir = args.cache_dir or out.parent / f".book_sync_cache_{out.name}"

    log(f"reading ebook {args.book.name}...")
    book = load_book(args.book)
    log(f"  '{book.title}': {len(book.tokens):,} words, {len(book.chapters)} sections")

    durations = [audio_duration(p) for p in args.audio]
    offsets = [sum(durations[:i]) for i in range(len(durations))]
    log(f"audio: {len(args.audio)} file(s), {fmt_lrc_time(sum(durations))} total")

    words = transcribe_all(args.audio, offsets, durations, cache_dir, args)
    log(f"transcript: {len(words):,} words")

    matched = align(book, words)
    interpolate(book)
    lines = build_lines(book, args.max_words)

    narrated = sum(1 for t in book.tokens if t.norm)
    timed = sum(1 for t in book.tokens if t.norm and t.start is not None)
    log(f"matched {matched:,} words directly; {timed / max(1, narrated):.0%} of the book's words have times")
    if timed / max(1, narrated) < 0.5:
        log("warning: under half the book was synced. Check the audio and ebook are the same "
            "edition and language (try --language and a larger --model).")

    if len(args.audio) > 1:
        joined = out.parent / f"{out.name}.joined{args.audio[0].suffix.lower()}"
        log(f"joining audio files into {joined.name} so the player has one file...")
        join_audio(args.audio, joined)
        audio_name = joined.name
    else:
        audio_name = args.audio[0].name

    lrc = [f"[ti:{book.title}]", f"[length:{fmt_lrc_time(sum(durations))}]", "[re:book_sync]"]
    lrc += [f"[{fmt_lrc_time(ln['start'])}]{ln['text']}" for ln in lines]
    lrc_path = out.parent / f"{out.name}.lrc"
    lrc_path.write_text("\n".join(lrc) + "\n", encoding="utf-8")

    data = {
        "version": 1,
        "title": book.title,
        "audio": audio_name,
        "duration": round(sum(durations), 3),
        "chapters": chapter_marks(book, lines),
        "lines": [{k: v for k, v in ln.items() if k != "chapter"} for ln in lines],
    }
    json_path = out.parent / f"{out.name}.sync.json"
    json_path.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")),
                         encoding="utf-8")

    log(f"wrote {lrc_path}")
    log(f"wrote {json_path}  ({len(lines):,} lines, {len(data['chapters'])} chapters)")
    log("open player.html, then load the audio and the .sync.json file.")


if __name__ == "__main__":
    main()
