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

    def test_gutenberg_license_heading_not_used_as_chapter(self):
        # Real PG EPUBs (e.g. #7256): a title page, then one file holding the story AND
        # the license, whose first heading is the license title.
        def doc(body):
            return f'<html xmlns="http://www.w3.org/1999/xhtml"><body>{body}</body></html>'
        story = "".join(f"<p>Story paragraph {i} with some words in it.</p>" for i in range(5))
        docs = [doc("<h1>The Project Gutenberg eBook of Test</h1><p>Intro.</p>"
                    "<p>*** START OF THE PROJECT GUTENBERG EBOOK TEST ***</p>"
                    "<h1>The Test Story</h1><p>by Someone</p>"),
                doc(f"{story}<p>*** END OF THE PROJECT GUTENBERG EBOOK TEST ***</p>"
                    "<h2>THE FULL PROJECT GUTENBERG LICENSE</h2><p>Terms.</p>")]
        p = self.tmp / "pg.epub"
        with zipfile.ZipFile(p, "w") as z:
            z.writestr("META-INF/container.xml",
                       '<container><rootfiles><rootfile full-path="c.opf"/></rootfiles></container>')
            z.writestr("c.opf", '<package><metadata><title>T</title></metadata><manifest>'
                       '<item id="a" href="a.xhtml"/><item id="b" href="b.xhtml"/></manifest>'
                       '<spine><itemref idref="a"/><itemref idref="b"/></spine></package>')
            z.writestr("a.xhtml", docs[0])
            z.writestr("b.xhtml", docs[1])
        book = bs.load_book(p)
        self.assertEqual(book.chapters, ["The Test Story"])
        self.assertNotIn("LICENSE", " ".join(t.text for t in book.tokens))

    def test_txt_chapters(self):
        p = self.tmp / "b.txt"
        p.write_text("Chapter 1\n\nIt was night.\n\nShe walked.\n\nChapter 2\n\nMorning came.\n")
        book = bs.load_book(p)
        self.assertEqual(book.chapters, ["Chapter 1", "Chapter 2"])

    def test_norm_word(self):
        self.assertEqual(bs.norm_word("Don’t,"), "dont")
        self.assertEqual(bs.norm_word("“Café”"), "cafe")
        self.assertEqual(bs.norm_word("—"), "")


try:
    import pypdf  # noqa: F401
    HAVE_PYPDF = True
except ImportError:
    HAVE_PYPDF = False


def make_pdf(path: Path, pages: list, title: str = "The Test Book", outline=None) -> None:
    """Write a simple text PDF (Helvetica, one text line per entry) without extra libraries."""
    def esc(s):
        return s.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
    objs = {1: "<< /Type /Catalog /Pages 2 0 R >>",
            3: "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"}
    kids = []
    for n, lines in enumerate(pages):
        pid, cid = 4 + 2 * n, 5 + 2 * n
        kids.append(f"{pid} 0 R")
        stream = "\n".join(["BT /F1 11 Tf 14 TL 72 760 Td"] + [f"({esc(l)}) Tj T*" for l in lines] + ["ET"])
        objs[pid] = ("<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
                     f"/Resources << /Font << /F1 3 0 R >> >> /Contents {cid} 0 R >>")
        objs[cid] = f"<< /Length {len(stream)} >>\nstream\n{stream}\nendstream"
    objs[2] = f"<< /Type /Pages /Kids [{' '.join(kids)}] /Count {len(pages)} >>"
    info = max(objs) + 1
    objs[info] = f"<< /Title ({esc(title)}) >>"
    out, offs = bytearray(b"%PDF-1.4\n"), {}
    for i in sorted(objs):
        offs[i] = len(out)
        out += f"{i} 0 obj\n{objs[i]}\nendobj\n".encode("latin-1")
    xref = len(out)
    out += f"xref\n0 {info + 1}\n0000000000 65535 f \n".encode()
    out += b"".join(f"{offs[i]:010d} 00000 n \n".encode() for i in range(1, info + 1))
    out += f"trailer\n<< /Size {info + 1} /Root 1 0 R /Info {info} 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    path.write_bytes(bytes(out))
    if outline:
        w = pypdf.PdfWriter(clone_from=str(path))
        for name, page in outline:
            w.add_outline_item(name, page)
        w.write(str(path))


def pdf_pages(chapters, header=lambda ch: "THE TEST BOOK", per_page=40, chapter_lines=True, hyphen=True):
    """Lay out chapters (lists of paragraphs) as printed pages: header, wrapped text, page number."""
    import textwrap
    pages, page_no = [], 0
    for c, paras in enumerate(chapters):
        body = [f"Chapter {c + 1}", ""] if chapter_lines else []
        for p in paras:
            body += textwrap.wrap(p, 70)
        if hyphen and c == 0:                     # split a word across two lines with a hyphen
            body[3] = body[3] + " won-"
            body[4] = "derful " + body[4]
        for i in range(0, len(body), per_page):   # each chapter starts on a new page
            page_no += 1
            pages.append([header(c + 1), ""] + body[i:i + per_page] + ["", str(page_no)])
    return pages


def ascii_paras(n, seed):
    rnd = random.Random(seed)
    out = []
    for _ in range(n):
        ws = [rnd.choice(VOCAB) for _ in range(rnd.randint(25, 70))]
        ws[0] = ws[0].capitalize()
        out.append(" ".join(ws) + ".")
    return out


@unittest.skipUnless(HAVE_PYPDF, "pypdf not installed")
class TestPDF(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def words(self, book):
        return [t.text for t in book.tokens]

    def test_headers_page_numbers_hyphens_and_chapter_lines(self):
        p = self.tmp / "b.pdf"
        make_pdf(p, pdf_pages([ascii_paras(30, 1), ascii_paras(30, 2)]))
        book = bs.load_book(p)
        words = self.words(book)
        self.assertEqual(book.title, "The Test Book")
        self.assertEqual(book.chapters, ["Chapter 1", "Chapter 2"])
        self.assertNotIn("TEST", words, "running header removed")
        self.assertEqual([w for w in words if w.isdigit()], ["1", "2"], "page numbers removed (chapter numbers stay)")
        self.assertIn("wonderful", words, "hyphenated word rejoined")
        self.assertNotIn("won-", words)
        expected = sum(len(p.split()) for p in ascii_paras(30, 1) + ascii_paras(30, 2))
        self.assertEqual(len(words), expected + 4 + 1, "every body word kept (+4 for 'Chapter N' x2, +1 joined word)")

    def test_chapters_from_bookmarks(self):
        p = self.tmp / "b.pdf"
        pages = pdf_pages([ascii_paras(25, 3), ascii_paras(25, 4)], chapter_lines=False, hyphen=False)
        second = next(i for i, pg in enumerate(pages) if i > 0 and pg[2].startswith(ascii_paras(25, 4)[0][:20]))
        make_pdf(p, pages, outline=[("Part One", 0), ("Part Two", second)])
        self.assertEqual(bs.load_book(p).chapters, ["Part One", "Part Two"])

    def test_chapters_from_running_headers(self):
        # No chapter lines in the text or bookmarks; "Chapter N" only atop each page.
        p = self.tmp / "b.pdf"
        make_pdf(p, pdf_pages([ascii_paras(40, 5), ascii_paras(40, 6)], header=lambda ch: f"Chapter {ch}",
                              chapter_lines=False, hyphen=False))
        book = bs.load_book(p)
        self.assertEqual(book.chapters, ["Chapter 1", "Chapter 2"])
        self.assertNotIn("Chapter", self.words(book))

    def test_scanned_and_protected_pdfs_explain_themselves(self):
        scanned = self.tmp / "scan.pdf"
        make_pdf(scanned, [[] for _ in range(5)])            # pages with no text, like page images
        with self.assertRaises(SystemExit) as cm:
            bs.load_book(scanned)
        self.assertIn("scanned", str(cm.exception.code))

        locked = self.tmp / "locked.pdf"
        make_pdf(locked, pdf_pages([ascii_paras(5, 1)]))
        w = pypdf.PdfWriter(clone_from=str(locked))
        w.encrypt(user_password="secret", algorithm="RC4-128")
        w.write(str(locked))
        with self.assertRaises(SystemExit) as cm:
            bs.load_book(locked)
        self.assertIn("protected", str(cm.exception.code))

    def test_alignment_on_pdf(self):
        p = self.tmp / "b.pdf"
        make_pdf(p, pdf_pages([ascii_paras(60, s) for s in range(7, 11)]))
        book = bs.load_book(p)
        words, truth = fake_narration(book)
        bs.align(book, words)
        bs.interpolate(book)
        errs = [abs(book.tokens[i].start - truth[i]) for i in truth if book.tokens[i].start is not None]
        self.assertGreater(len(errs) / len(truth), 0.9)
        self.assertLess(sum(errs) / len(errs), 0.1)


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
