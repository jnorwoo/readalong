"""
Tests for tools/book_sync.py and tools/add_to_library.py.

Run:  python -m unittest discover tests -v

They don't need WhisperX or a GPU: transcription is replaced with fake word
lists so the parsing, matching and timing logic can be checked exactly.
"""

import json
import random
import shutil
import subprocess
import sys
import tempfile
import types
import unittest
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
import book_sync as bs  # noqa: E402
import add_to_library  # noqa: E402

VOCAB = ("the a of and to river light house night she he said walked slowly over under "
         "quiet morning door window old young road field winter letters father mother").split()


def make_epub(path: Path, chapters: int = 4, paras: int = 30, gutenberg: bool = False, seed: int = 1) -> None:
    rnd = random.Random(seed)

    def para():
        ws = [rnd.choice(VOCAB) for _ in range(rnd.randint(15, 50))]
        ws[0] = ws[0].capitalize()
        return " ".join(w + ("," if i % 9 == 8 else "") for i, w in enumerate(ws)) + ". “Don’t go,” she said."

    docs = []
    if gutenberg:
        docs.append("<p>The Project Gutenberg eBook of Test. This eBook is for the use of anyone.</p>"
                    "<p>*** START OF THE PROJECT GUTENBERG EBOOK TEST ***</p>")
    for c in range(chapters):
        body = "".join(f"<p>{para()}</p>" for _ in range(paras))
        docs.append(f"<h1>Chapter {c + 1}</h1>{body}")
    if gutenberg:
        docs.append("<p>*** END OF THE PROJECT GUTENBERG EBOOK TEST ***</p>"
                    "<p>Updated editions will replace the previous one. Section 1. General Terms of Use.</p>")
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("mimetype", "application/epub+zip")
        z.writestr("META-INF/container.xml",
                   '<?xml version="1.0"?><container xmlns="urn:oasis:names:tc:opendocument:xmlns:container" '
                   'version="1.0"><rootfiles><rootfile full-path="OEBPS/content.opf" '
                   'media-type="application/oebps-package+xml"/></rootfiles></container>')
        items = "".join(f'<item id="d{i}" href="text/d{i}.xhtml" media-type="application/xhtml+xml"/>'
                        for i in range(len(docs)))
        spine = "".join(f'<itemref idref="d{i}"/>' for i in range(len(docs)))
        z.writestr("OEBPS/content.opf",
                   '<?xml version="1.0"?><package xmlns="http://www.idpf.org/2007/opf" version="3.0">'
                   '<metadata xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:title>The Test Book</dc:title>'
                   f'</metadata><manifest>{items}</manifest><spine>{spine}</spine></package>')
        for i, d in enumerate(docs):
            z.writestr(f"OEBPS/text/d{i}.xhtml",
                       f'<html xmlns="http://www.w3.org/1999/xhtml"><head><title>x</title></head><body>{d}</body></html>')


def fake_narration(book, skip=range(0), seed=2, drop=0.05, mishear=0.05):
    """Narrate the book as transcript words, with credits, errors and a skipped section."""
    rnd = random.Random(seed)
    words, truth, t = [], {}, 0.0
    for w in "this is a librivox recording narrated by jane doe".split():
        words.append(bs.Word(w, t, t + 0.3)); t += 0.35
    for i, tok in enumerate(book.tokens):
        if i in skip or not tok.norm:
            continue
        truth[i] = t
        r = rnd.random()
        if r < drop:
            t += 0.35
            continue
        words.append(bs.Word(tok.norm if r > drop + mishear else "zz" + tok.norm, t, t + 0.3))
        t += 0.35
        if r > 0.97:
            words.append(bs.Word("um", t, t + 0.2)); t += 0.25
    return words, truth


class TestEbook(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def test_epub_chapters_and_title(self):
        p = self.tmp / "b.epub"
        make_epub(p, chapters=3)
        book = bs.load_book(p)
        self.assertEqual(book.title, "The Test Book")
        self.assertEqual(book.chapters, ["Chapter 1", "Chapter 2", "Chapter 3"])
        self.assertGreater(len(book.tokens), 1000)

    def test_gutenberg_boilerplate_removed(self):
        p = self.tmp / "g.epub"
        make_epub(p, chapters=2, gutenberg=True)
        text = " ".join(t.text for t in bs.load_book(p).tokens)
        self.assertNotIn("Gutenberg", text)
        self.assertNotIn("General Terms", text)
        self.assertTrue(text.startswith("Chapter 1"))

    def test_txt_chapters(self):
        p = self.tmp / "b.txt"
        p.write_text("Chapter 1\n\nIt was night.\n\nShe walked.\n\nChapter 2\n\nMorning came.\n")
        book = bs.load_book(p)
        self.assertEqual(book.chapters, ["Chapter 1", "Chapter 2"])

    def test_norm_word(self):
        self.assertEqual(bs.norm_word("Don’t,"), "dont")
        self.assertEqual(bs.norm_word("“Café”"), "cafe")
        self.assertEqual(bs.norm_word("—"), "")


class TestAlignment(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp())
        make_epub(cls.tmp / "b.epub", chapters=6, paras=40)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp)

    def test_accuracy_with_errors_and_skipped_section(self):
        book = bs.load_book(self.tmp / "b.epub")
        skip = range(3000, 3400)
        words, truth = fake_narration(book, skip=skip)
        bs.align(book, words)
        bs.interpolate(book)
        errs = [abs(book.tokens[i].start - truth[i]) for i in truth if book.tokens[i].start is not None]
        self.assertGreater(len(errs) / len(truth), 0.9, "at least 90% of narrated words get times")
        self.assertLess(max(errs), 2.0, "no word is more than 2s off")
        self.assertLess(sum(errs) / len(errs), 0.1)
        self.assertEqual(sum(1 for i in skip if book.tokens[i].start is not None), 0,
                         "text missing from the audio stays untimed")

    def test_times_never_go_backwards(self):
        book = bs.load_book(self.tmp / "b.epub")
        words, _ = fake_narration(book, seed=5, drop=0.1, mishear=0.1)
        bs.align(book, words)
        times = [t.start for t in book.tokens if t.matched]
        self.assertEqual(times, sorted(times))

    def test_lines_and_chapters(self):
        book = bs.load_book(self.tmp / "b.epub")
        words, _ = fake_narration(book)
        bs.align(book, words)
        bs.interpolate(book)
        lines = bs.build_lines(book, 12)
        self.assertTrue(all(len(l["words"]) <= 12 for l in lines))
        starts = [l["start"] for l in lines]
        self.assertEqual(starts, sorted(starts))
        marks = bs.chapter_marks(book, lines)
        self.assertEqual([m["title"] for m in marks], [f"Chapter {i}" for i in range(1, 7)])


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "needs ffmpeg")
class TestChunking(unittest.TestCase):
    def test_chunks_offsets_and_overlap(self):
        tmp = Path(tempfile.mkdtemp())
        try:
            files = []
            for name, secs in (("a.mp3", 150), ("b.mp3", 70)):
                subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
                                f"sine=f=440:d={secs}", str(tmp / name)], check=True)
                files.append(tmp / name)

            class OneWordPerSecond:
                def __init__(self, *a): pass
                def transcribe(self, wav):
                    d = bs.audio_duration(wav)
                    return [{"word": "w", "start": i + 0.1, "end": i + 0.6} for i in range(int(d))]

            real = bs.Transcriber
            bs.Transcriber = OneWordPerSecond
            try:
                args = types.SimpleNamespace(chunk_minutes=1, model=None, device="cpu", language="en",
                                             batch_size=1, compute_type=None)
                d = [bs.audio_duration(f) for f in files]
                words = bs.transcribe_all(files, [0, d[0]], d, tmp / "cache", args)
            finally:
                bs.Transcriber = real
            starts = [w.start for w in words]
            self.assertEqual(len(words), 220, "no duplicates or gaps across chunks and files")
            self.assertEqual(starts, sorted(starts))
            self.assertAlmostEqual(starts[150], 150.1, places=1)
        finally:
            shutil.rmtree(tmp)


class TestLibrary(unittest.TestCase):
    def test_add_and_replace(self):
        tmp = Path(tempfile.mkdtemp())
        web = tmp / "web"
        web.mkdir()
        old = add_to_library.WEB
        add_to_library.WEB = web
        try:
            (tmp / "x.mp3").write_bytes(b"ID3")
            (tmp / "x.sync.json").write_text(json.dumps({"title": "X", "audio": "x.mp3", "duration": 3, "lines": []}))
            for _ in range(2):  # adding twice replaces, not duplicates
                add_to_library.main(["demo", str(tmp / "x.mp3"), str(tmp / "x.sync.json"), "--author", "A"])
            lib = json.loads((web / "library.json").read_text())
            self.assertEqual(len(lib["books"]), 1)
            self.assertEqual(lib["books"][0]["audio"], "books/demo/audio.mp3")
            self.assertEqual(json.loads((web / "books/demo/sync.json").read_text())["audio"], "audio.mp3")
            add_to_library.main(["mine", str(tmp / "x.mp3"), str(tmp / "x.sync.json"), "--private"])
            self.assertTrue((web / "library.private.json").exists())
            self.assertTrue((web / "books/private/mine/audio.mp3").exists())
        finally:
            add_to_library.WEB = old
            shutil.rmtree(tmp)


if __name__ == "__main__":
    unittest.main()
