#!/usr/bin/env python3
"""
ebook2pdf.py — Convert on-screen digital text (e-book readers, web pages,
document viewers) into a PDF using screen capture + OCR (pytesseract).

Workflow:
  1. Pick the window (or screen region) that shows the text.
  2. The tool captures the region, sends a "next page" keystroke,
     waits for the page to render, and repeats.
  3. It stops when the requested number of pages is reached, or
     automatically when two consecutive captures are identical
     (i.e. the last page was reached).
  4. All pages are assembled into a PDF:
       - "searchable" mode (default): page images with an invisible,
         selectable OCR text layer (looks exactly like the source).
       - "text" mode: OCR-extracted plain text re-flowed into a PDF.
       - "image" mode: page images only, no OCR.

Requires the Tesseract OCR engine to be installed on the system
(https://tesseract-ocr.github.io/tessdoc/Installation.html).

Examples:
  # Fully interactive: asks window-picker vs drag-select, then asks
  # for a page count (or Enter to scan to the end of the book)
  python ebook2pdf.py -o book.pdf

  # Capture up to 120 pages from a window whose title contains "Kindle"
  python ebook2pdf.py --window kindle --pages 120 -o book.pdf

  # Scan to the end of the book without any prompts
  python ebook2pdf.py --window kindle --to-end -o book.pdf

  # Drag-select a region of the screen, advance with the space bar
  python ebook2pdf.py --select-region --key space -o book.pdf

  # Plain re-flowed text PDF, also dump the raw text
  python ebook2pdf.py --mode text --save-text book.txt -o book.pdf
"""

import argparse
import hashlib
import io
import json
import os
import re
import sys
import time
from collections import Counter

from PIL import Image


def make_dpi_aware():
    """On Windows, mark the process DPI-aware so window coordinates from
    pygetwindow match the physical pixels captured by mss. Without this,
    display scaling (125%, 150%...) makes captures come out cropped."""
    if sys.platform != "win32":
        return
    import ctypes
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)  # per-monitor DPI aware
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass

# Safety cap for end-of-book mode, which normally stops via
# identical-page detection long before this many captures.
END_OF_BOOK_CAP = 10000

# ---------------------------------------------------------------------------
# Region / window selection
# ---------------------------------------------------------------------------


def list_windows():
    """Return a list of (title, box) for all visible titled windows."""
    try:
        import pygetwindow as gw
    except Exception:
        return None  # pygetwindow unavailable on this platform
    windows = []
    try:
        for w in gw.getAllWindows():
            title = (w.title or "").strip()
            if not title or w.width <= 0 or w.height <= 0:
                continue
            windows.append((title, (w.left, w.top, w.width, w.height), w))
    except Exception:
        return None
    return windows


def pick_window_interactive():
    """Print all windows and let the user choose one by number."""
    windows = list_windows()
    if not windows:
        print("Window enumeration is not available on this platform.")
        print("Falling back to interactive region selection (drag a box).")
        return select_region_with_mouse(), None
    print("\nVisible windows:")
    for i, (title, box, _w) in enumerate(windows, 1):
        print(f"  [{i:2d}] {title[:70]}  ({box[2]}x{box[3]} at {box[0]},{box[1]})")
    while True:
        choice = input("\nSelect the window containing the text (number): ").strip()
        try:
            idx = int(choice) - 1
            if 0 <= idx < len(windows):
                title, box, w = windows[idx]
                print(f"Selected: {title}")
                return box, w
        except ValueError:
            pass
        print("Invalid choice, try again.")


def choose_source_interactive():
    """Ask the user whether to pick a window or drag-select a region."""
    print("\nHow do you want to choose the capture area?")
    print("  [1] Pick an open window from a list")
    print("  [2] Drag-select a region of the screen")
    while True:
        choice = input("Choice [1]: ").strip() or "1"
        if choice == "1":
            return pick_window_interactive()
        if choice == "2":
            return select_region_with_mouse(), None
        print("Enter 1 or 2.")


def choose_page_count_interactive():
    """Ask for a page count; blank means scan to the end of the book."""
    while True:
        raw = input(
            "\nHow many pages should be captured? "
            "(enter a number, or press Enter to scan to the end of the book): "
        ).strip()
        if not raw:
            return None
        try:
            n = int(raw)
            if n > 0:
                return n
        except ValueError:
            pass
        print("Enter a positive number, or leave blank for end-of-book.")


def find_window_by_title(fragment):
    """Find the first window whose title contains `fragment` (case-insensitive)."""
    windows = list_windows()
    if not windows:
        sys.exit(
            "Window lookup by title is not available on this platform. "
            "Use --select-region or --region instead."
        )
    fragment = fragment.lower()
    for title, box, w in windows:
        if fragment in title.lower():
            print(f"Matched window: {title}")
            return box, w
    sys.exit(f'No window title contains "{fragment}". Run without --window to list them.')


def select_region_with_mouse():
    """Full-screen transparent overlay: drag a rectangle to choose the region."""
    import tkinter as tk

    result = {}
    root = tk.Tk()
    root.attributes("-fullscreen", True)
    root.attributes("-alpha", 0.25)
    root.configure(bg="black", cursor="crosshair")
    canvas = tk.Canvas(root, bg="black", highlightthickness=0)
    canvas.pack(fill="both", expand=True)
    canvas.create_text(
        root.winfo_screenwidth() // 2,
        40,
        text="Drag a rectangle around the text area (Esc to cancel)",
        fill="white",
        font=("TkDefaultFont", 16),
    )
    rect = {"id": None, "x0": 0, "y0": 0}

    def on_press(e):
        rect["x0"], rect["y0"] = e.x_root, e.y_root
        rect["id"] = canvas.create_rectangle(e.x, e.y, e.x, e.y, outline="red", width=2)

    def on_drag(e):
        if rect["id"] is not None:
            x0 = rect["x0"] - root.winfo_rootx()
            y0 = rect["y0"] - root.winfo_rooty()
            canvas.coords(rect["id"], x0, y0, e.x, e.y)

    def on_release(e):
        x0, y0 = rect["x0"], rect["y0"]
        x1, y1 = e.x_root, e.y_root
        result["box"] = (min(x0, x1), min(y0, y1), abs(x1 - x0), abs(y1 - y0))
        root.destroy()

    def on_escape(_e):
        root.destroy()

    canvas.bind("<ButtonPress-1>", on_press)
    canvas.bind("<B1-Motion>", on_drag)
    canvas.bind("<ButtonRelease-1>", on_release)
    root.bind("<Escape>", on_escape)
    root.mainloop()

    box = result.get("box")
    if not box or box[2] < 10 or box[3] < 10:
        sys.exit("Region selection cancelled or too small.")
    print(f"Selected region: {box[2]}x{box[3]} at {box[0]},{box[1]}")
    return box


# ---------------------------------------------------------------------------
# Capture / page turning
# ---------------------------------------------------------------------------


def grab(sct, box):
    """Capture (left, top, width, height) and return a PIL image."""
    left, top, width, height = box
    raw = sct.grab({"left": left, "top": top, "width": width, "height": height})
    return Image.frombytes("RGB", raw.size, raw.rgb)


def image_fingerprint(img):
    """Hash of a downscaled grayscale image — tolerant of cursor blinks."""
    small = img.convert("L").resize((256, 256))
    return hashlib.md5(small.tobytes()).hexdigest()


def focus_window(win):
    """Try to bring the chosen window to the foreground."""
    if win is None:
        return False
    try:
        win.activate()
        time.sleep(0.5)
        return True
    except Exception:
        return False


def capture_pages(box, win, args):
    """Capture pages, turning them automatically. Returns list of PIL images."""
    import mss
    import pyautogui

    pyautogui.FAILSAFE = True  # slam mouse into a screen corner to abort

    if not focus_window(win):
        print(
            f"\nStarting in {args.start_delay} seconds — click on the reader window "
            "now so it receives the page-turn keystrokes..."
        )
    for remaining in range(args.start_delay, 0, -1):
        print(f"  {remaining}...", end="\r", flush=True)
        time.sleep(1)
    print()

    images = []
    with mss.mss() as sct:
        img = grab(sct, box)
        images.append(img)
        seen = {image_fingerprint(img)}
        print("Captured page 1", end="\r", flush=True)

        while len(images) < args.pages:
            # Turn the page; if the screen doesn't change (slow render,
            # dropped keystroke) retry with a longer wait each time. Only
            # after every attempt fails is it the end of the book. The
            # fingerprint set guarantees no page is captured twice.
            new_img = None
            for attempt in range(1, args.turn_retries + 1):
                pyautogui.press(args.key)
                time.sleep(args.delay * attempt)
                candidate = grab(sct, box)
                fp = image_fingerprint(candidate)
                if fp not in seen:
                    new_img = candidate
                    seen.add(fp)
                    break
                if attempt < args.turn_retries:
                    print(f"\nPage unchanged after turn (attempt {attempt}/"
                          f"{args.turn_retries}) — retrying with a longer "
                          "wait...")
            if new_img is None:
                print(f"\nPage still unchanged after {args.turn_retries} "
                      "attempts — reached the end of the book.")
                break
            images.append(new_img)
            print(f"Captured page {len(images)}", end="\r", flush=True)

    print(f"\nCaptured {len(images)} unique pages.")
    if not images:
        sys.exit("No pages captured — nothing to do.")
    return images


# ---------------------------------------------------------------------------
# PDF assembly
# ---------------------------------------------------------------------------


def build_searchable_pdf(images, output, lang):
    """Each page = original image + invisible OCR text layer (Tesseract PDF)."""
    import pytesseract
    from pypdf import PdfReader, PdfWriter

    writer = PdfWriter()
    for i, img in enumerate(images, 1):
        print(f"OCR page {i}/{len(images)}...", end="\r", flush=True)
        page_pdf = pytesseract.image_to_pdf_or_hocr(img, extension="pdf", lang=lang)
        reader = PdfReader(io.BytesIO(page_pdf))
        for p in reader.pages:
            writer.add_page(p)
    print()
    with open(output, "wb") as f:
        writer.write(f)


def extract_text(images, lang):
    """OCR every page image; returns a list of page-text strings."""
    import pytesseract

    texts = []
    for i, img in enumerate(images, 1):
        print(f"OCR page {i}/{len(images)}...", end="\r", flush=True)
        texts.append(pytesseract.image_to_string(img, lang=lang).strip())
    print()
    return texts


# Reader chrome that is not part of the book text: page counters,
# progress percentages, locations ("Página 161 de 413 • 38%", "Loc 1024").
CHROME_PATTERNS = [
    re.compile(r"^[\s•·|]*(página|pagina|page|pág\.?|p\.)\s*\d+.*$", re.I),
    re.compile(r"^[\s•·|]*loc(ation)?\.?\s*\d+.*$", re.I),
]
# Bare numbers/percents/separators — only stripped at the BOTTOM of a page,
# where reader page counters live; at the top they may be chapter numbers.
BARE_NUMBER_PATTERN = re.compile(r"^[\s•·|.\-–—\d%:]+$")

# Chapter-opening labels ("CHAPTER FOURTEEN", "PROLOGUE") — they force a
# page break and keep their alignment from the source page.
CHAPTER_LABEL_RE = re.compile(
    r"^(chapter|capitulo|capítulo|chapitre|kapitel|part|book)\s+[\w\s-]{1,20}$"
    r"|^(prologue|epilogue|preface|introduction|interlude|foreword|afterword)$",
    re.I)

# Shear angles (tangents) treated as italic candidates. Typical italics
# lean 10-15 degrees (tangent 0.18-0.27).
ITALIC_SHEARS = [0.15, 0.20, 0.25, 0.30]

# Slant can only be measured on letters with vertical stems. A word made
# entirely of round/diagonal letterforms ("way", "eyes", "see") gives a
# noise answer, so it stays undecided and inherits from its neighbors.
_VERTICAL_STEM_CHARS = set("bdfhijklmnpqrtu" "BDEFHIJKLMNPRTU" "14")


def _norm_line(line):
    return re.sub(r"\s+", " ", line.strip().lower())


def _italic_fraction(markup):
    """Fraction of a paragraph's text that sits inside <i> runs."""
    total = len(re.sub(r"<[^>]+>", "", markup))
    if not total:
        return 0.0
    italic = sum(len(re.sub(r"<[^>]+>", "", m))
                 for m in re.findall(r"<i>(.*?)</i>", markup))
    return italic / total


def _esc(text):
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _shear_energy(ink, cy, t):
    """Sharpness of the vertical projection after shearing ink by t."""
    cols = {}
    for x, y in ink:
        c = int(x + t * (y - cy))
        cols[c] = cols.get(c, 0) + 1
    return sum(n * n for n in cols.values())


def _word_italic(img):
    """Classify a word image as italic (True), upright (False), or
    ambiguous (None — the caller lets it inherit from its neighbors).

    Compares the sharpness of the vertical stroke profile upright vs
    sheared back by typical italic angles: genuinely italic words get
    much sharper when counter-sheared, genuinely upright words get worse,
    and words made of round letterforms with no strong verticals ("see",
    "eyes") land in between and stay undecided."""
    g = img.convert("L")
    w, h = g.size
    if w < 4 or h < 8:
        return None
    data = g.tobytes()
    ink = [(i % w, i // w) for i, v in enumerate(data) if v < 160]
    if len(ink) < 30:
        return None

    cy = h / 2
    upright = _shear_energy(ink, cy, 0.0)
    if upright <= 0:
        return None
    italic = max(_shear_energy(ink, cy, t) for t in ITALIC_SHEARS)
    ratio = italic / upright
    # Calibrated on rendered serif/sans/mono text: upright words score
    # 0.80-1.00, italic words 1.01-1.18, round-letterform words ~1.00.
    # Only confident calls decide; the wide ambiguous band in between
    # inherits from neighboring words, which lets weak italics join a
    # surrounding italic run without letting them fire on their own.
    if ratio > 1.04:
        return True
    if ratio < 0.96:
        return False
    return None


def _line_markup(words):
    """words: list of (escaped_text, is_italic) -> line with <i> runs."""
    segments = []
    for text, italic in words:
        if segments and segments[-1][1] == italic:
            segments[-1][0].append(text)
        else:
            segments.append(([text], italic))
    parts = []
    for texts, italic in segments:
        joined = " ".join(texts)
        parts.append(f"<i>{joined}</i>" if italic else joined)
    return " ".join(parts)


def _join_lines(lines):
    """Join a paragraph's lines, mending words hyphenated at line ends."""
    out = ""
    for line in lines:
        if not out:
            out = line
        elif out.endswith("-</i>"):
            out = out[:-5] + "</i>" + line
        elif out.endswith("-"):
            out = out[:-1] + line
        else:
            out += " " + line
    return out


# OCR confidence gates: individual words below MIN_WORD_CONF are dropped
# outright; paragraphs whose mean confidence falls below the --min-confidence
# threshold (default 55) are discarded as gibberish from artwork or
# decorative pages.
MIN_WORD_CONF = 20


HEADING_DEFAULTS = {"h1_ratio": 1.8, "h2_ratio": 1.35}


def extract_structured(images, lang, min_conf=55, h1_ratio=1.8,
                       h2_ratio=1.35):
    """OCR every page with word geometry and return, per page, a list of
    paragraph dicts: {"kind": "h1"|"h2"|"body", "plain": ..., "markup": ...}.

    Headings are detected by comparing each paragraph's median word height
    to the document-wide median (body text). Italics are detected from the
    glyph slant of each word's image."""
    import statistics

    import pytesseract
    from pytesseract import Output

    datas = []
    for i, img in enumerate(images, 1):
        print(f"OCR page {i}/{len(images)}...", end="\r", flush=True)
        datas.append(pytesseract.image_to_data(img, lang=lang,
                                               output_type=Output.DICT))
    print()

    def conf(d, j):
        try:
            return float(d["conf"][j])
        except (TypeError, ValueError):
            return -1.0

    heights = [
        d["height"][j]
        for d in datas
        for j in range(len(d["text"]))
        if len(d["text"][j].strip()) >= 2 and conf(d, j) > 30
    ]
    body_height = statistics.median(heights) if heights else 20

    pages = []
    for img, d in zip(images, datas):
        paras = {}  # (block, par) -> {line_num: [word indices]}
        for j in range(len(d["text"])):
            if not d["text"][j].strip() or conf(d, j) < MIN_WORD_CONF:
                continue
            key = (d["block_num"][j], d["par_num"][j])
            paras.setdefault(key, {}).setdefault(d["line_num"][j], []).append(j)

        raw_paras = []
        for key, lines in paras.items():
            # inline drop cap: OCR sometimes glues the oversized chapter-
            # opening letter into the first text line as a junk token
            # ("W.:", "Ww"). A huge 1-3 char first word followed by a
            # normal-sized word is that letter — reduce it to the letter,
            # folding it into a short lowercase remainder ("W"+"e" -> "We").
            first_idxs = lines[min(lines)]
            if len(first_idxs) >= 2:
                j0, j1 = first_idxs[0], first_idxs[1]
                t0 = d["text"][j0].strip()
                t1 = d["text"][j1].strip()
                alpha = [c for c in t0 if c.isalpha()]
                if (len(t0) <= 3 and alpha
                        and d["height"][j0] >= 1.8 * body_height
                        and d["height"][j1] < 1.4 * body_height):
                    letter = alpha[0].upper()
                    if t1 and t1[0].islower() and len(t1) <= 2:
                        d["text"][j1] = letter + t1
                        d["text"][j0] = ""
                    else:
                        d["text"][j0] = letter

            line_recs = []
            for ln in sorted(lines):
                texts, flags, heights, confs = [], [], [], []
                left = right = top = bottom = None
                for j in lines[ln]:
                    text = d["text"][j].strip()
                    if not text:
                        continue
                    box = (d["left"][j], d["top"][j],
                           d["left"][j] + d["width"][j],
                           d["top"][j] + d["height"][j])
                    left = box[0] if left is None else min(left, box[0])
                    right = box[2] if right is None else max(right, box[2])
                    top = box[1] if top is None else min(top, box[1])
                    bottom = box[3] if bottom is None else max(bottom, box[3])
                    if conf(d, j) >= 0:
                        confs.append(conf(d, j))
                    # short words and words without vertical stems are
                    # unreliable for slant detection (None); they inherit
                    # from their neighbors below
                    italic = (None if len(text) < 3
                              or not set(text) & _VERTICAL_STEM_CHARS
                              else _word_italic(img.crop(box)))
                    texts.append(_esc(text))
                    flags.append(italic)
                    if len(text) >= 2:
                        heights.append(d["height"][j])
                for k, f in enumerate(flags):
                    if f is None:
                        prev = next((flags[m] for m in range(k - 1, -1, -1)
                                     if flags[m] is not None), None)
                        nxt = next((flags[m] for m in range(k + 1, len(flags))
                                    if flags[m] is not None), None)
                        # lean italic: a short word joins an italic run when
                        # either determined neighbor is italic
                        flags[k] = bool(prev) or bool(nxt)
                if not texts:
                    continue
                line_recs.append({
                    "plain": " ".join(texts),
                    "markup": _line_markup(list(zip(texts, flags))),
                    "left": left, "right": right, "top": top,
                    "bottom": bottom, "heights": heights, "confs": confs,
                })
            if line_recs:
                raw_paras.append(line_recs)

        # The right margin of the text column: justified wrapped lines
        # reach it, so a line ending well short of it is an intentional
        # break (heading lines, place/date blocks, verse) — split there.
        # Centered lines (poetry, epigraphs) are always their own line,
        # even on pages with no justified text to establish the margin.
        page_paras = []
        if raw_paras:
            col_right = max(r["right"] for lr in raw_paras for r in lr)
            col_left = min(r["left"] for lr in raw_paras for r in lr)
            col_width = max(col_right - col_left, 1)
            short_cut = col_right - 0.15 * col_width

        def line_is_centered(rec):
            center = (rec["left"] + rec["right"]) / 2
            width = rec["right"] - rec["left"]
            return (abs(center - img.width / 2) < 0.05 * img.width
                    and width < 0.7 * img.width
                    and rec["left"] > col_left + 0.08 * col_width)

        for line_recs in raw_paras:
            segments, current = [], []
            for i, rec in enumerate(line_recs):
                current.append(rec)
                is_last = i == len(line_recs) - 1
                if not is_last and (rec["right"] < short_cut
                                    or line_is_centered(rec)
                                    or line_is_centered(line_recs[i + 1])):
                    segments.append(current)
                    current = []
            if current:
                segments.append(current)

            for seg in segments:
                plain = _join_lines([r["plain"] for r in seg])
                markup = _join_lines([r["markup"] for r in seg])
                if not plain.strip():
                    continue
                confs = [c for r in seg for c in r["confs"]]
                if (confs and len(plain) > 2
                        and sum(confs) / len(confs) < min_conf):
                    continue  # OCR gibberish (decorative pages, artwork)
                heights = [h for r in seg for h in r["heights"]]
                ratio = ((statistics.median(heights) / body_height)
                         if heights else 1.0)
                page_paras.append({
                    "kind": "body", "plain": plain, "markup": markup,
                    # a paragraph whose final line stops short is complete —
                    # never merge it with the next page
                    "ends_short": seg[-1]["right"] < short_cut,
                    "_ratio": ratio,
                    "_left": min(r["left"] for r in seg),
                    "_right": max(r["right"] for r in seg),
                    "_top": min(r["top"] for r in seg),
                    "_bottom": max(r["bottom"] for r in seg),
                })

        page_paras.sort(key=lambda p: p["_top"])

        # Headings must be larger than body text AND centered on the page —
        # a lone tall-glyphed word at the paragraph indent ("Tidy?") is
        # body text, not a heading.
        page_center = img.width / 2
        for p in page_paras:
            center = (p["_left"] + p["_right"]) / 2
            width = p["_right"] - p["_left"]
            p["_centered"] = (abs(center - page_center) < 0.05 * img.width
                              and width < 0.7 * img.width)

        def in_centered_run(idx):
            """True when a neighboring paragraph is normal-sized centered
            text — i.e. this line sits inside a poem/epigraph block, so a
            slightly tall measurement must not make it a heading."""
            for n in (idx - 1, idx + 1):
                if 0 <= n < len(page_paras):
                    q = page_paras[n]
                    if (q["_centered"]
                            and q.get("_ratio", 1.0) < h2_ratio):
                        return True
            return False

        for idx, p in enumerate(page_paras):
            plain = p["plain"].strip()
            if CHAPTER_LABEL_RE.match(plain) and len(plain) < 36:
                p["kind"] = "label"
                gap_left = p["_left"]
                gap_right = img.width - p["_right"]
                if p["_centered"]:
                    p["align"] = "center"
                elif gap_right < gap_left * 0.5:
                    p["align"] = "right"
                else:
                    p["align"] = "left"
            elif p["_centered"] and len(plain) < 80:
                if p["_ratio"] >= h1_ratio:
                    p["kind"] = "h1"
                elif p["_ratio"] >= h2_ratio:
                    # A short line directly under a chapter title/label is a
                    # subtitle — heading-group position outranks the checks
                    # below. Otherwise: italic glyph boxes run tall (slant +
                    # ascenders), inflating the size ratio, so mostly-italic
                    # centered lines (thoughts, epigraphs) stay body, as do
                    # lines sitting inside a centered block (poems).
                    prev_kind = (page_paras[idx - 1]["kind"]
                                 if idx > 0 else None)
                    if prev_kind in ("h1", "label") and len(plain) < 30:
                        p["kind"] = "h2"
                    elif (not in_centered_run(idx)
                            and _italic_fraction(p["markup"]) <= 0.6):
                        p["kind"] = "h2"
            # centered text that isn't a heading (poems, epigraphs,
            # dedications) keeps its centering in the output
            if p["kind"] == "body" and p["_centered"]:
                p["align"] = "center"

        # the heading right after a chapter label is the chapter title —
        # give it full title styling even if it measured on the small side
        for i in range(len(page_paras) - 1):
            if (page_paras[i]["kind"] == "label"
                    and page_paras[i + 1]["kind"] == "h2"):
                page_paras[i + 1]["kind"] = "h1"

        # a centered bare number right before a heading is the chapter
        # number, even when it measured too small for h1 on its own — it
        # must open the chapter (page break + contents entry), not trail
        # the previous page
        for i in range(len(page_paras) - 1):
            p, q = page_paras[i], page_paras[i + 1]
            if (p["kind"] in ("body", "h2") and p.get("_centered")
                    and re.fullmatch(r"\d{1,4}|[IVXLCDM]{1,8}",
                                     p["plain"].strip(), re.I)
                    and q["kind"] in ("h1", "h2", "label")):
                p["kind"] = "h1"
                p.pop("align", None)

        # drop caps: a huge one-to-three-letter "paragraph" is the oversized
        # first letter of the adjacent paragraph — put it back
        for i, p in enumerate(page_paras):
            if (p["kind"] == "body" and p["plain"]
                    and len(p["plain"]) <= 3 and p["plain"].isalpha()
                    and p.get("_ratio", 1.0) >= 1.8):
                letter = p["plain"][0].upper()
                target = next(
                    (q for q in page_paras
                     if q is not p and q["kind"] == "body" and q["plain"]
                     and q["plain"][0].islower()),
                    None)
                if target is not None:
                    target["plain"] = letter + target["plain"]
                    target["markup"] = _esc(letter) + target["markup"]
                    p["plain"] = p["markup"] = ""  # dropped below

        page_paras = [p for p in page_paras if p["plain"]]
        for p in page_paras:
            for key in ("_top", "_bottom", "_ratio", "_left", "_right",
                        "_centered"):
                p.pop(key, None)
        pages.append(page_paras)
    return pages


def strip_headers_footers(pages):
    """Remove reader chrome and running headers/footers from structured
    pages. A paragraph is a running header/footer when its normalized text
    appears at the edge of at least 30% of the pages."""
    EDGE = 2  # paragraphs from each edge of a page to consider
    edge_counts = Counter()
    for paras in pages:
        # a set, so a paragraph counts at most once per page even when the
        # page is short and its top/bottom edge windows overlap
        for text in {_norm_line(p["plain"]) for p in paras[:EDGE] + paras[-EDGE:]}:
            edge_counts[text] += 1

    threshold = max(2, int(0.3 * len(pages)))
    running = {t for t, n in edge_counts.items() if n >= threshold and len(t) > 3}

    cleaned = []
    for paras in pages:
        keep = []
        for i, p in enumerate(paras):
            plain = p["plain"].strip()
            near_edge = i < EDGE or i >= len(paras) - EDGE
            near_bottom = i >= len(paras) - EDGE
            if near_edge and _norm_line(plain) in running:
                continue
            if any(pat.match(plain) for pat in CHROME_PATTERNS):
                continue
            if near_bottom and BARE_NUMBER_PATTERN.match(plain):
                continue
            keep.append(p)
        cleaned.append(keep)
    return cleaned


_TERMINAL = tuple('.!?:"\'”’)»…')


def _merge_paragraphs(prev, nxt):
    """Append paragraph `nxt` to `prev` in place, mending hyphenation."""
    for field in ("plain", "markup"):
        a, b = prev[field], nxt[field]
        if a.endswith("-</i>"):
            prev[field] = a[:-5] + "</i>" + b
        elif a.endswith("-"):
            prev[field] = a[:-1] + b
        else:
            prev[field] = a + " " + b


def reflow_paragraphs(pages, keep_page_breaks=False):
    """Flatten structured pages into one list of paragraph dicts, joining
    body paragraphs that continue across a page break. With
    keep_page_breaks, a "\\f" sentinel separates pages instead."""
    out = []
    for page_no, paras in enumerate(pages):
        if keep_page_breaks and page_no > 0:
            out.append("\f")
        for i, p in enumerate(paras):
            prev = out[-1] if out and isinstance(out[-1], dict) else None
            continues_previous = (
                not keep_page_breaks
                and i == 0
                and prev is not None
                and prev["kind"] == "body"
                and p["kind"] == "body"
                # centered lines (poems, epigraphs) never continue a
                # justified paragraph from the previous page
                and prev.get("align") != "center"
                and p.get("align") != "center"
                # a short final line means the paragraph ended on that page
                and not prev.get("ends_short")
                and (
                    prev["plain"].endswith("-")
                    or not prev["plain"].endswith(_TERMINAL)
                    or p["plain"][0].islower()
                )
            )
            if continues_previous:
                _merge_paragraphs(prev, p)
                prev["ends_short"] = p.get("ends_short", False)
            else:
                out.append(dict(p))
    return out


FORMAT_DEFAULTS = {
    "page_size": "a4",
    "orientation": "portrait",
    "font_size": 11.0,
    "line_spacing": 1.4,
    "margin": 2.5,
}


def _ask_choice(prompt, choices, default):
    while True:
        raw = input(f"  {prompt} ({'/'.join(choices)}) [{default}]: ").strip().lower()
        if not raw:
            return default
        if raw in choices:
            return raw
        print(f"    Please enter one of: {', '.join(choices)}")


def _ask_number(prompt, default):
    while True:
        raw = input(f"  {prompt} [{default}]: ").strip()
        if not raw:
            return default
        try:
            value = float(raw)
            if value > 0:
                return value
        except ValueError:
            pass
        print("    Please enter a positive number.")


def _clean_path(raw):
    """Trim whitespace and the quotes Windows' 'Copy as path' adds."""
    return raw.strip().strip('"').strip("'").strip()


# User defaults, kept next to the script: default destination folder, the
# folder whose newest image becomes the suggested cover, and remembered
# author/series names offered as numbered choices at the prompts.
CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "ebook2pdf_config.json")


def load_config():
    try:
        with open(CONFIG_FILE, encoding="utf-8") as f:
            cfg = json.load(f)
    except (OSError, ValueError):
        cfg = {}
    cfg.setdefault("dest", "")
    cfg.setdefault("cover_dir", "")
    cfg.setdefault("authors", [])
    cfg.setdefault("series", [])
    cfg.setdefault("format", "both")
    return cfg


def save_config(cfg):
    try:
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2)
    except OSError as exc:
        print(f"Could not save {CONFIG_FILE}: {exc}")


def newest_image(folder):
    """Most recently modified image file in `folder`, or ''."""
    exts = (".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif")
    folder = os.path.expanduser(folder)
    try:
        files = [os.path.join(folder, name) for name in os.listdir(folder)
                 if name.lower().endswith(exts)]
    except OSError:
        return ""
    return max(files, key=os.path.getmtime, default="")


def _pick_known(label, plural, known):
    """Offer remembered names as a numbered list; typing a new name (or a
    number from the list) selects it, Enter skips."""
    if known:
        print(f"  Known {plural}:")
        for i, name in enumerate(known, 1):
            print(f"    [{i}] {name}")
        raw = input(f"  {label} (number, new name, or Enter to skip): ").strip()
    else:
        raw = input(f"  {label} (Enter to skip): ").strip()
    if raw.isdigit() and known and 1 <= int(raw) <= len(known):
        return known[int(raw) - 1]
    return raw


def resolve_book_info(args):
    """Return {'title','author','series','cover','dest'} for the book.
    Metadata flags on the command line (or --defaults) skip the prompts;
    config defaults (destination folder, newest cover image, remembered
    authors and series) fill anything not answered."""
    cfg = load_config()
    fields = {k: getattr(args, k)
              for k in ("title", "author", "series", "cover", "dest")}
    if args.defaults or any(v is not None for v in fields.values()):
        fields = {k: (v or "") for k, v in fields.items()}
        fields["dest"] = fields["dest"] or cfg["dest"]
        return fields

    print("\nBook details (used for the title page, contents, and file name):")
    fields["title"] = input("  Title (Enter to skip): ").strip()
    fields["author"] = _pick_known("Author", "authors", cfg["authors"])
    series_name = _pick_known("Series", "series", cfg["series"])
    fields["series"] = series_name
    if series_name:
        number = input("  Book number in the series (Enter to skip): ").strip()
        if number:
            fields["series"] = f"{series_name}, Book {number}"

    newest = newest_image(cfg["cover_dir"]) if cfg["cover_dir"] else ""
    while True:
        if newest:
            raw = input(f"  Cover image (Enter = none, 1 = newest: "
                        f"{os.path.basename(newest)}, or a path): ")
        else:
            raw = input("  Cover image (Enter = none, or a path): ")
        raw = raw.strip()
        if not raw:
            cover = ""
            break
        if raw == "1" and newest:
            cover = newest
            break
        cover = _clean_path(raw)
        if os.path.isfile(os.path.expanduser(cover)):
            break
        print(f"    File not found: {cover} — try again, or press Enter "
              "for no cover.")
    fields["cover"] = cover

    dest_hint = cfg["dest"] or "current folder"
    fields["dest"] = (_clean_path(input(f"  Destination folder "
                                        f"[{dest_hint}]: "))
                      or cfg["dest"])

    # remember new names for next time
    changed = False
    if fields["author"] and fields["author"] not in cfg["authors"]:
        cfg["authors"].append(fields["author"])
        changed = True
    if series_name and series_name not in cfg["series"]:
        cfg["series"].append(series_name)
        changed = True
    if changed:
        save_config(cfg)
    return fields


def resolve_output_path(args, book, ext="pdf"):
    """Output path: -o wins (its extension swapped to `ext` if needed);
    otherwise <dest>/<sanitized title>.<ext>."""
    if args.output:
        if args.output.lower().endswith(f".{ext}"):
            return args.output
        return os.path.splitext(args.output)[0] + f".{ext}"
    title = (book or {}).get("title", "")
    name = re.sub(r'[<>:"/\\|?*]', "", title).strip().rstrip(".") or "output"
    name = re.sub(r"\s+", " ", name)
    dest = os.path.expanduser((book or {}).get("dest", "") or "")
    if dest:
        os.makedirs(dest, exist_ok=True)
    return os.path.join(dest, f"{name}.{ext}")


def resolve_settings(args):
    """All conversion settings in one place: output format, PDF layout,
    EPUB margins, OCR confidence, and heading detection. Setting flags on
    the command line (or --defaults) skip the prompt; otherwise a single
    summary is shown — Enter proceeds with defaults, 1 changes settings."""
    cfg_format = load_config().get("format", "both")
    flag_values = [args.format, args.page_size, args.orientation,
                   args.font_size, args.line_spacing, args.margin,
                   args.epub_vmargin, args.epub_hmargin,
                   args.min_confidence, args.h1_ratio, args.h2_ratio]

    def from_flags():
        return {
            "format": args.format or cfg_format,
            "pdf": {k: getattr(args, k) if getattr(args, k) is not None
                    else FORMAT_DEFAULTS[k] for k in FORMAT_DEFAULTS},
            "epub": {
                "vmargin": (args.epub_vmargin
                            if args.epub_vmargin is not None
                            else EPUB_MARGIN_DEFAULTS["vmargin"]),
                "hmargin": (args.epub_hmargin
                            if args.epub_hmargin is not None
                            else EPUB_MARGIN_DEFAULTS["hmargin"]),
            },
            "min_conf": (args.min_confidence
                         if args.min_confidence is not None else 55),
            "h1_ratio": (args.h1_ratio if args.h1_ratio is not None
                         else HEADING_DEFAULTS["h1_ratio"]),
            "h2_ratio": (args.h2_ratio if args.h2_ratio is not None
                         else HEADING_DEFAULTS["h2_ratio"]),
        }

    if args.defaults or any(v is not None for v in flag_values):
        return from_flags()

    s = from_flags()  # all defaults at this point
    fmt = s["pdf"]
    print(f"""
Settings:
  Output format : {s['format'].upper()}
  PDF layout    : {fmt['page_size'].upper()} {fmt['orientation']}, \
{fmt['font_size']:g} pt, {fmt['line_spacing']:g} line spacing, \
{fmt['margin']:g} cm margins
  EPUB margins  : {s['epub']['vmargin']:g} em top/bottom, \
{s['epub']['hmargin']:g} em sides
  OCR filter    : keep paragraphs with mean confidence >= {s['min_conf']:g}
  Headings      : chapter title >= {s['h1_ratio']:g}x body text, \
subheading >= {s['h2_ratio']:g}x""")
    if input("Press Enter to proceed with defaults, "
             "or 1 to change settings: ").strip() != "1":
        return s

    s["format"] = _ask_choice("Output format", ["pdf", "epub", "both"],
                              cfg_format)
    if s["format"] in ("pdf", "both"):
        fmt["page_size"] = _ask_choice("Page size", ["a4", "letter"], "a4")
        fmt["orientation"] = _ask_choice("Orientation",
                                         ["portrait", "landscape"], "portrait")
        fmt["font_size"] = _ask_number("Font size (pt)", 11.0)
        fmt["line_spacing"] = _ask_number("Line spacing (x font size)", 1.4)
        fmt["margin"] = _ask_number("Margins (cm)", 2.5)
    if s["format"] in ("epub", "both"):
        s["epub"]["vmargin"] = _ask_number(
            "EPUB top/bottom margin (em)", EPUB_MARGIN_DEFAULTS["vmargin"])
        s["epub"]["hmargin"] = _ask_number(
            "EPUB side margin (em)", EPUB_MARGIN_DEFAULTS["hmargin"])
    s["min_conf"] = _ask_number("Minimum OCR confidence (0-100)", 55)
    s["h1_ratio"] = _ask_number("Chapter title size (x body text)",
                                HEADING_DEFAULTS["h1_ratio"])
    s["h2_ratio"] = _ask_number("Subheading size (x body text)",
                                HEADING_DEFAULTS["h2_ratio"])
    return s


# Tesseract language -> EPUB/ISO language code (first tag wins for "eng+fra")
EPUB_LANG = {"eng": "en", "deu": "de", "fra": "fr", "spa": "es", "ita": "it",
             "por": "pt", "nld": "nl", "pol": "pl", "swe": "sv", "dan": "da",
             "nor": "no", "fin": "fi", "rus": "ru", "ces": "cs", "cym": "cy"}

EPUB_MARGIN_DEFAULTS = {"vmargin": 1.4, "hmargin": 0.4}
_MARGIN_START = "/* ebook2pdf margins */"
_MARGIN_END = "/* end ebook2pdf margins */"
_MARGIN_BLOCK_RE = re.compile(
    re.escape(_MARGIN_START) + r".*?" + re.escape(_MARGIN_END) + r"\s*",
    re.S)


def _epub_margin_css(vmargin=1.4, hmargin=0.4):
    """Page-edge breathing room, declared several ways because readers
    vary in what they honor: @page margins (ADE, Kobo), body padding
    (most webviews), or neither (older Kindle)."""
    return (f"{_MARGIN_START}\n"
            f"@page {{ margin: {vmargin:g}em {hmargin:g}em; }}\n"
            f"body {{ margin: 0; padding: {vmargin:g}em {hmargin:g}em; }}\n"
            f"div.chapter {{ margin-top: {vmargin * 1.6:g}em; }}\n"
            f"{_MARGIN_END}\n")


EPUB_CSS = """\
body { font-family: serif; }
p { text-align: justify; margin: 0 0 0.5em 0; }
p.center { text-align: center; }
p.label { font-weight: bold; margin: 2em 0 1em 0; }
p.label.right { text-align: right; }
p.label.center { text-align: center; }
h1 { text-align: center; margin: 1.5em 0 1em 0; }
h2 { text-align: center; margin: 1em 0 0.8em 0; }
div.pic { text-align: center; margin: 1em 0; }
div.pic img { max-width: 100%; }
div.titlepage { text-align: center; margin-top: 18%; }
div.titlepage p.series { letter-spacing: 0.12em; color: #444444; }
div.titlepage h1 { font-size: 2em; margin: 1em 0; }
div.titlepage p.author { font-style: italic; font-size: 1.2em;
                         margin-top: 2.5em; text-align: center; }
"""


def build_epub(paragraphs, output, book=None, lang="eng", margins=None):
    """Assemble the structured paragraphs into an EPUB: metadata, cover,
    title page, one XHTML file per chapter, embedded pictures, and a
    navigation table of contents."""
    import uuid

    from ebooklib import epub

    book = book or {}
    title = book.get("title") or "Untitled"
    bk = epub.EpubBook()
    bk.set_identifier(str(uuid.uuid4()))
    bk.set_title(title)
    bk.set_language(EPUB_LANG.get(lang.split("+")[0].lower(), "en"))
    if book.get("author"):
        bk.add_author(book["author"])
    series = book.get("series") or ""
    if series:
        name = re.sub(r",\s*Book\s+\S+$", "", series)
        bk.add_metadata(None, "meta", "",
                        {"name": "calibre:series", "content": name})
        m = re.search(r"Book\s+(\d+)", series)
        if m:
            bk.add_metadata(None, "meta", "",
                            {"name": "calibre:series_index",
                             "content": m.group(1)})

    cover = book.get("cover")
    if cover:
        cover = os.path.expanduser(_clean_path(cover))
        if os.path.isfile(cover):
            ext = os.path.splitext(cover)[1].lower() or ".png"
            with open(cover, "rb") as f:
                bk.set_cover(f"cover{ext}", f.read())
        else:
            print(f"Cover image not found, skipping: {cover}")

    css_text = (_epub_margin_css(**{**EPUB_MARGIN_DEFAULTS, **(margins or {})})
                + EPUB_CSS)
    css = epub.EpubItem(uid="style", file_name="style/main.css",
                        media_type="text/css", content=css_text)
    bk.add_item(css)

    # split the flow into chapters at the detected chapter openings
    starts = _chapter_starts(paragraphs)
    idxs = [i for i, _ in starts]
    names = [t for _, t in starts]
    sections = []
    if not idxs or idxs[0] > 0:
        head = paragraphs[:idxs[0]] if idxs else paragraphs
        if any(isinstance(p, dict) for p in head):
            sections.append(("Front matter", head))
    for k, i in enumerate(idxs):
        end = idxs[k + 1] if k + 1 < len(idxs) else len(paragraphs)
        sections.append((names[k], paragraphs[i:end]))

    chapters = []
    if book.get("title"):
        parts = ['<div class="titlepage">']
        if series:
            parts.append(f'<p class="series">{_esc(series).upper()}</p>')
        parts.append(f"<h1>{_esc(title)}</h1>")
        if book.get("author"):
            parts.append(f'<p class="author">{_esc(book["author"])}</p>')
        parts.append("</div>")
        tp = epub.EpubHtml(uid="titlepage", title="Title Page",
                           file_name="titlepage.xhtml")
        tp.content = "".join(parts)
        tp.add_item(css)
        bk.add_item(tp)
        chapters.append(tp)

    for k, (name, paras) in enumerate(sections, 1):
        html = ['<div class="chapter">']
        for para in paras:
            if isinstance(para, str):  # "\f" page-break sentinel
                continue
            kind = para["kind"]
            if kind == "h1":
                html.append(f"<h1>{para['markup']}</h1>")
            elif kind == "h2":
                html.append(f"<h2>{para['markup']}</h2>")
            elif kind == "label":
                align = para.get("align", "left")
                html.append(f'<p class="label {align}">{para["markup"]}</p>')
            elif para.get("align") == "center":
                html.append(f'<p class="center">{para["markup"]}</p>')
            else:
                html.append(f"<p>{para['markup']}</p>")
        html.append("</div>")
        ch = epub.EpubHtml(uid=f"chap{k}", title=name,
                           file_name=f"chap_{k}.xhtml")
        ch.content = "\n".join(html)
        ch.add_item(css)
        bk.add_item(ch)
        chapters.append(ch)

    bk.toc = chapters
    bk.add_item(epub.EpubNcx())
    bk.add_item(epub.EpubNav())
    bk.spine = (["cover"] if cover and os.path.isfile(cover) else []) \
        + ["nav"] + chapters
    epub.write_epub(output, bk, {})


# ---------------------------------------------------------------------------
# Editing existing books
# ---------------------------------------------------------------------------


def _body_inner(content):
    """Inner HTML of an XHTML document's <body> (or the whole fragment)."""
    s = content.decode("utf-8") if isinstance(content, bytes) else content
    m = re.search(r"<body[^>]*>(.*)</body>", s, re.S | re.I)
    return m.group(1) if m else s


_LEADING_HEADING_RE = re.compile(
    r'^\s*(<h[12][^>]*>.*?</h[12]>|<p[^>]*class="[^"]*label[^"]*"[^>]*>.*?</p>'
    r'|<p[^>]*class="running"[^>]*>.*?</p>)\s*', re.S)
_RUNNING_RE = re.compile(r'<p[^>]*class="running"[^>]*>.*?</p>\s*', re.S)


def _unwrap_chapter_div(html):
    """Peel off a section's <div class="chapter"> wrapper if present."""
    m = re.match(r'\s*<div[^>]*class="chapter"[^>]*>(.*)</div>\s*$',
                 html, re.S)
    return (m.group(1), True) if m else (html, False)


def _strip_leading_headings(html):
    html, _ = _unwrap_chapter_div(html)
    while True:
        m = _LEADING_HEADING_RE.match(html)
        if not m:
            return html
        html = html[m.end():]


_BLOCK_SPLIT_RE = re.compile(r"(?<=</p>)\s*|(?<=</h1>)\s*|(?<=</h2>)\s*"
                             r"|(?<=</h3>)\s*|(?<=</div>)\s*")


def _split_blocks(html):
    """A section's HTML as a list of block elements."""
    return [part for part in _BLOCK_SPLIT_RE.split(html) if part.strip()]


def _first_heading_text(html):
    m = re.search(r"<h[12][^>]*>(.*?)</h[12]>", html, re.S)
    if m:
        return re.sub(r"<[^>]+>", "", m.group(1)).strip()
    return ""


def _flatten_toc_links(entries):
    links = []
    for t in entries or []:
        if isinstance(t, (list, tuple)):
            head, children = (t[0], t[1]) if len(t) == 2 else (None, t)
            if head is not None and getattr(head, "href", ""):
                links.append(head)
            links.extend(_flatten_toc_links(children))
        else:
            links.append(t)
    return links


def _insert_toc_after(entries, href, link):
    """Insert `link` right after the entry pointing at `href` (recursive)."""
    for i, t in enumerate(entries):
        head = t[0] if isinstance(t, (list, tuple)) and len(t) == 2 else t
        if getattr(head, "href", "").split("#")[0] == href:
            entries.insert(i + 1, link)
            return True
        if isinstance(t, (list, tuple)) and len(t) == 2:
            children = t[1] if isinstance(t[1], list) else list(t[1])
            if _insert_toc_after(children, href, link):
                return True
    return False


def _remove_from_toc(entries, href):
    out = []
    for t in entries or []:
        if isinstance(t, (list, tuple)) and len(t) == 2:
            head, children = t
            children = _remove_from_toc(children, href)
            if getattr(head, "href", "").split("#")[0] == href and not children:
                continue
            out.append((head, children))
        elif getattr(t, "href", "").split("#")[0] == href:
            continue
        else:
            out.append(t)
    return out


_IMAGE_MEDIA_TYPES = {".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                      ".png": "image/png", ".webp": "image/webp",
                      ".gif": "image/gif", ".bmp": "image/bmp"}


def _ask_cover_path():
    """Prompt for a cover image, offering the newest file in the
    configured covers folder as the default. Returns '' if skipped."""
    default = newest_image(load_config().get("cover_dir", ""))
    while True:
        if default:
            raw = input(f"New cover image [{default}]: ")
        else:
            raw = input("New cover image file: ")
        path = _clean_path(raw) or default
        if not path:
            return ""
        path = os.path.expanduser(path)
        if os.path.isfile(path):
            return path
        print(f"    File not found: {path} — try again, or press Enter "
              "to cancel.")
        default = ""


def _find_cover_image(bk):
    """The book's cover image item, however the EPUB declares it."""
    import ebooklib
    for item in bk.get_items():
        if item.get_type() == ebooklib.ITEM_COVER:
            return item
    cover_id = None
    for ns in ("OPF", None):
        try:
            for _val, attrs in bk.get_metadata(ns, "meta") or []:
                if attrs and attrs.get("name") == "cover":
                    cover_id = attrs.get("content")
        except KeyError:
            pass
    if cover_id:
        item = bk.get_item_with_id(cover_id)
        if item is not None:
            return item
    for item in bk.get_items():
        if (item.get_type() == ebooklib.ITEM_IMAGE
                and "cover" in item.file_name.lower()):
            return item
    return None


def _render_cover_page(image_path, width, height):
    """A single-page in-memory PDF with the image centered and fit."""
    from reportlab.lib.utils import ImageReader
    from reportlab.pdfgen import canvas as rl_canvas

    buf = io.BytesIO()
    c = rl_canvas.Canvas(buf, pagesize=(width, height))
    img = ImageReader(image_path)
    iw, ih = img.getSize()
    margin = 18
    zoom = min((width - 2 * margin) / iw, (height - 2 * margin) / ih)
    c.drawImage(img, (width - iw * zoom) / 2, (height - ih * zoom) / 2,
                iw * zoom, ih * zoom)
    c.showPage()
    c.save()
    buf.seek(0)
    return buf


def _ask_save_path(path):
    stem, ext = os.path.splitext(path)
    default = f"{stem}-edited{ext}"
    raw = _clean_path(input(f"Save as [{default}]: "))
    return raw or default


def edit_epub(path):
    import ebooklib
    from ebooklib import epub

    bk = epub.read_epub(path)
    spine_ids = [s[0] if isinstance(s, (list, tuple)) else s for s in bk.spine]
    by_id = {item.id: item for item in bk.get_items()}
    titles = {}
    for link in _flatten_toc_links(bk.toc):
        titles.setdefault(getattr(link, "href", "").split("#")[0],
                          getattr(link, "title", ""))

    def sections():
        out = []
        for sid in spine_ids:
            item = by_id.get(sid)
            if item is None or item.get_type() != ebooklib.ITEM_DOCUMENT:
                continue
            if os.path.basename(item.file_name) in ("nav.xhtml", "toc.xhtml",
                                                    "cover.xhtml"):
                continue
            name = (titles.get(item.file_name)
                    or _first_heading_text(_body_inner(item.get_content()))
                    or item.file_name)
            out.append((item, name))
        return out

    def drop(item):
        nonlocal spine_ids
        bk.items.remove(item)
        spine_ids = [sid for sid in spine_ids if sid != item.id]
        bk.spine = [s for s in bk.spine
                    if (s[0] if isinstance(s, (list, tuple)) else s) != item.id]
        bk.toc = _remove_from_toc(bk.toc, item.file_name)

    changed = False
    while True:
        secs = sections()
        print(f"\n{os.path.basename(path)} — {len(secs)} sections:")
        for i, (_, name) in enumerate(secs, 1):
            print(f"  [{i:2d}] {name[:70]}")
        print("Options: 1 = remove a chapter's heading (its text merges into"
              " the previous section)\n         2 = delete a section entirely"
              "\n         3 = fix page margins"
              "\n         4 = change the cover image"
              "\n         5 = add a chapter heading (splits a section)"
              "\n         s = save and exit, q = quit without saving")
        choice = input("Choice: ").strip().lower()
        if choice == "q":
            print("No changes saved.")
            return
        if choice == "s":
            break
        if choice == "5":
            raw = input("Section holding the text where the new chapter "
                        "starts: ").strip()
            if not raw.isdigit() or not 1 <= int(raw) <= len(secs):
                print("Invalid section number.")
                continue
            item, _name = secs[int(raw) - 1]
            inner, wrapped = _unwrap_chapter_div(
                _body_inner(item.get_content()))
            blocks = _split_blocks(inner)
            if len(blocks) < 2:
                print("That section has too little content to split.")
                continue
            print("Paragraphs:")
            for i, blk in enumerate(blocks, 1):
                preview = re.sub(r"<[^>]+>", "", blk).strip()[:65]
                print(f"  [{i:3d}] {preview}")
            raw = input("Paragraph where the new chapter starts: ").strip()
            if not raw.isdigit() or not 2 <= int(raw) <= len(blocks):
                print("Invalid paragraph number (the first paragraph "
                      "cannot start a new split).")
                continue
            heading = input("Heading text: ").strip()
            if not heading:
                print("No heading given.")
                continue
            cut = int(raw) - 1
            head_html = "\n".join(blocks[:cut])
            tail_html = f"<h1>{_esc(heading)}</h1>\n" + "\n".join(blocks[cut:])
            if wrapped:
                head_html = f'<div class="chapter">{head_html}</div>'
                tail_html = f'<div class="chapter">{tail_html}</div>'
            item.content = head_html
            new_name = f"added_{len(by_id)}.xhtml"
            new_item = epub.EpubHtml(uid=f"added{len(by_id)}", title=heading,
                                     file_name=new_name)
            new_item.content = tail_html
            style = next((s for s in bk.get_items()
                          if s.get_type() == ebooklib.ITEM_STYLE), None)
            if style is not None:
                new_item.add_item(style)
            bk.add_item(new_item)
            by_id[new_item.id] = new_item
            pos = spine_ids.index(item.id) + 1
            spine_ids.insert(pos, new_item.id)
            bpos = next((i for i, s in enumerate(bk.spine)
                         if (s[0] if isinstance(s, (list, tuple)) else s)
                         == item.id), len(bk.spine) - 1) + 1
            bk.spine.insert(bpos, (new_item.id, "yes"))
            titles[new_name] = heading
            link = epub.Link(new_name, heading, f"added{len(by_id)}")
            bk.toc = list(bk.toc)
            if not _insert_toc_after(bk.toc, item.file_name, link):
                bk.toc.append(link)
            changed = True
            print(f"Added chapter: {heading}")
            continue
        if choice == "4":
            path_new = _ask_cover_path()
            if not path_new:
                continue
            with open(path_new, "rb") as f:
                data = f.read()
            ext = os.path.splitext(path_new)[1].lower()
            media = _IMAGE_MEDIA_TYPES.get(ext, "image/png")
            existing = _find_cover_image(bk)
            if existing is not None:
                existing.content = data
                existing.media_type = media
            else:
                # register the image plus the standard cover metadata that
                # readers use for the library thumbnail (set_cover's page
                # object does not survive a read/write round trip)
                item = epub.EpubItem(uid="cover-img",
                                     file_name=f"images/cover{ext or '.png'}",
                                     media_type=media, content=data)
                bk.add_item(item)
                bk.add_metadata(None, "meta", "",
                                {"name": "cover", "content": "cover-img"})
            changed = True
            print(f"Cover set from {os.path.basename(path_new)}.")
            continue
        if choice == "3":
            v = _ask_number("Top/bottom margin (em)",
                            EPUB_MARGIN_DEFAULTS["vmargin"])
            h = _ask_number("Side margin (em)",
                            EPUB_MARGIN_DEFAULTS["hmargin"])
            block = _epub_margin_css(v, h)
            css_items = [item for item in bk.get_items()
                         if item.get_type() == ebooklib.ITEM_STYLE]
            if css_items:
                # margin declarations appended last win the cascade; an
                # earlier ebook2pdf margin block is replaced, not stacked
                for item in css_items:
                    text = item.get_content()
                    text = text.decode("utf-8", "ignore") \
                        if isinstance(text, bytes) else text
                    text = _MARGIN_BLOCK_RE.sub("", text)
                    item.content = text.rstrip() + "\n\n" + block
            else:
                sheet = epub.EpubItem(
                    uid="ebook2pdf_style", file_name="style/ebook2pdf.css",
                    media_type="text/css", content=block)
                bk.add_item(sheet)
                for item, _ in sections():
                    item.add_link(href="style/ebook2pdf.css",
                                  rel="stylesheet", type="text/css")
            changed = True
            print(f"Margins set to {v:g} em top/bottom, {h:g} em sides.")
            continue
        if choice not in ("1", "2"):
            continue
        raw = input("Section number: ").strip()
        if not raw.isdigit() or not 1 <= int(raw) <= len(secs):
            print("Invalid section number.")
            continue
        idx = int(raw) - 1
        item, name = secs[idx]
        if choice == "2":
            drop(item)
            changed = True
            print(f"Deleted: {name}")
            continue
        # remove heading: strip it and fold the text into the previous section
        rest = _strip_leading_headings(_body_inner(item.get_content()))
        rest = _RUNNING_RE.sub("", rest)
        if idx > 0:
            prev = secs[idx - 1][0]
            prev.content = _body_inner(prev.get_content()) + rest
            drop(item)
        else:
            item.content = rest
            bk.toc = _remove_from_toc(bk.toc, item.file_name)
        changed = True
        print(f"Removed heading: {name}")

    if not changed:
        print("Nothing changed.")
        return
    # links read from an existing EPUB carry no uid, which breaks
    # ebooklib's NCX writer — assign them before saving
    for i, link in enumerate(_flatten_toc_links(bk.toc)):
        if not getattr(link, "uid", None):
            try:
                link.uid = f"navpoint{i}"
            except AttributeError:
                pass
    out = _ask_save_path(path)
    epub.write_epub(out, bk, {})
    print(f"Saved: {out}")


def _parse_page_ranges(raw, total):
    """'3,5-7' -> zero-based page indices; 1-based input, inclusive ends."""
    pages = set()
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            a, _, b = part.partition("-")
            if a.strip().isdigit() and b.strip().isdigit():
                pages.update(range(int(a) - 1, int(b)))
        elif part.isdigit():
            pages.add(int(part) - 1)
    return {p for p in pages if 0 <= p < total}


def edit_pdf(path):
    from pypdf import PdfReader, PdfWriter

    reader = PdfReader(path)
    total = len(reader.pages)
    chapters = []  # (title, zero-based start page)

    def walk(entries):
        for entry in entries:
            if isinstance(entry, list):
                walk(entry)
            else:
                try:
                    chapters.append(
                        (entry.title,
                         reader.get_destination_page_number(entry)))
                except Exception:
                    pass
    try:
        walk(reader.outline)
    except Exception:
        pass
    chapters.sort(key=lambda c: c[1])

    removed = set()
    new_cover = None  # (mode, image path); mode: "replace" | "insert"
    added_marks = []  # (title, zero-based page)
    while True:
        print(f"\n{os.path.basename(path)} — {total} pages"
              + (f", {len(removed)} marked for removal" if removed else "")
              + (f", new cover: {os.path.basename(new_cover[1])}"
                 if new_cover else "")
              + (f", {len(added_marks)} new bookmark(s)"
                 if added_marks else ""))
        if chapters:
            print("Chapters (from bookmarks):")
            for i, (name, page0) in enumerate(chapters, 1):
                mark = " [removed]" if page0 in removed else ""
                print(f"  [{i:2d}] p.{page0 + 1:<4d} {name[:60]}{mark}")
        print("Options: 1 = remove pages (e.g. 3,5-7)\n"
              "         2 = remove a chapter (its pages, up to the next "
              "chapter)\n"
              "         3 = change the cover page\n"
              "         4 = add a chapter bookmark (title + page)\n"
              "         u = undo all changes\n"
              "         s = save and exit, q = quit without saving")
        choice = input("Choice: ").strip().lower()
        if choice == "q":
            print("No changes saved.")
            return
        if choice == "s":
            break
        if choice == "u":
            removed.clear()
            new_cover = None
            added_marks.clear()
            continue
        if choice == "4":
            title = input("Chapter title: ").strip()
            raw = input("Page number it starts on: ").strip()
            if title and raw.isdigit() and 1 <= int(raw) <= total:
                added_marks.append((title, int(raw) - 1))
                print(f"Bookmark '{title}' at page {raw}.")
            else:
                print("Need a title and a valid page number.")
            continue
        if choice == "3":
            cover_path = _ask_cover_path()
            if not cover_path:
                continue
            mode = input("[r]eplace the current first page, or [i]nsert a "
                         "new page before it? [r]: ").strip().lower()
            new_cover = ("insert" if mode.startswith("i") else "replace",
                         cover_path)
            continue
        if choice == "1":
            raw = input("Pages to remove: ").strip()
            picked = _parse_page_ranges(raw, total)
            if picked:
                removed |= picked
                print(f"Marked {len(picked)} page(s).")
            else:
                print("No valid pages in that input.")
        elif choice == "2" and chapters:
            raw = input("Chapter number: ").strip()
            if raw.isdigit() and 1 <= int(raw) <= len(chapters):
                k = int(raw) - 1
                start = chapters[k][1]
                end = (chapters[k + 1][1] if k + 1 < len(chapters) else total)
                removed |= set(range(start, end))
                print(f"Marked pages {start + 1}-{end} "
                      f"({chapters[k][0][:40]}).")
            else:
                print("Invalid chapter number.")

    if not removed and not new_cover and not added_marks:
        print("Nothing changed.")
        return
    keep = [i for i in range(total) if i not in removed]
    if new_cover and new_cover[0] == "replace" and keep and keep[0] == 0:
        keep = keep[1:]
    if not keep and not new_cover:
        sys.exit("Refusing to remove every page.")
    writer = PdfWriter()
    if new_cover:
        box = reader.pages[0].mediabox
        cover_buf = _render_cover_page(new_cover[1],
                                       float(box.width), float(box.height))
        writer.append(PdfReader(cover_buf))
    try:
        writer.append(reader, pages=keep)
    except Exception:
        for i in keep:
            writer.add_page(reader.pages[i])
    offset = 1 if new_cover else 0
    for title, page0 in added_marks:
        if page0 in keep:
            writer.add_outline_item(title, keep.index(page0) + offset)
        else:
            print(f"Skipping bookmark '{title}': its page was removed.")
    out = _ask_save_path(path)
    with open(out, "wb") as f:
        writer.write(f)
    print(f"Saved: {out} ({len(writer.pages)} pages)")


def edit_book(path):
    path = os.path.expanduser(_clean_path(path))
    if not os.path.isfile(path):
        sys.exit(f"File not found: {path}")
    if path.lower().endswith(".epub"):
        edit_epub(path)
    elif path.lower().endswith(".pdf"):
        edit_pdf(path)
    else:
        sys.exit("Only .epub and .pdf files can be edited.")


def _chapter_starts(paragraphs):
    """Find chapter openings: (index, combined heading text) for every
    label/h1 paragraph that follows body content (or starts the book),
    joining the consecutive run of label/h1 lines into one entry."""
    chapters = []
    prev = None
    for i, para in enumerate(paragraphs):
        if isinstance(para, str):  # "\f" page-break sentinel
            prev = None
            continue
        kind = para["kind"]
        if kind in ("h1", "label") and prev in (None, "body"):
            parts = [para["plain"]]
            j = i + 1
            while (j < len(paragraphs) and isinstance(paragraphs[j], dict)
                   and paragraphs[j]["kind"] in ("label", "h1")):
                parts.append(paragraphs[j]["plain"])
                j += 1
            chapters.append((i, " — ".join(parts)))
        prev = kind
    return chapters


def build_text_pdf(paragraphs, output, fmt=None, book=None):
    """Lay the cleaned paragraphs out on standard pages, styling headings
    and italics to match the source. With `book` info, prepend a cover
    page, a stylized title page, and a linked table of contents."""
    from reportlab.lib.enums import TA_CENTER, TA_JUSTIFY, TA_LEFT, TA_RIGHT
    from reportlab.lib.pagesizes import A4, landscape, letter
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import cm
    from reportlab.platypus import (HRFlowable, Image as RLImage, PageBreak,
                                    Paragraph, SimpleDocTemplate, Spacer)
    from reportlab.platypus.tableofcontents import TableOfContents

    class BookDocTemplate(SimpleDocTemplate):
        def afterFlowable(self, flowable):
            entry = getattr(flowable, "_toc_entry", None)
            if entry is not None:
                text, key = entry
                self.canv.bookmarkPage(key)
                self.notify("TOCEntry", (0, _esc(text), self.page, key))
                self.canv.addOutlineEntry(text, key, level=0)

    fmt = {**FORMAT_DEFAULTS, **(fmt or {})}
    page = letter if fmt["page_size"] == "letter" else A4
    if fmt["orientation"] == "landscape":
        page = landscape(page)
    fs = fmt["font_size"]
    leading = fs * fmt["line_spacing"]
    margin = fmt["margin"] * cm

    styles = {
        "body": ParagraphStyle(
            "Body", fontName="Times-Roman", fontSize=fs, leading=leading,
            alignment=TA_JUSTIFY, spaceAfter=fs * 0.5),
        "h1": ParagraphStyle(
            "H1", fontName="Times-Bold", fontSize=fs * 1.8,
            leading=fs * 1.8 * 1.2, alignment=TA_CENTER,
            spaceBefore=fs * 1.6, spaceAfter=fs * 0.9),
        "h2": ParagraphStyle(
            "H2", fontName="Times-Bold", fontSize=fs * 1.35,
            leading=fs * 1.35 * 1.2, alignment=TA_CENTER,
            spaceBefore=fs * 1.1, spaceAfter=fs * 0.7),
    }
    label_align = {"left": TA_LEFT, "center": TA_CENTER, "right": TA_RIGHT}
    for align, ta in label_align.items():
        styles[f"label-{align}"] = ParagraphStyle(
            f"Label-{align}", fontName="Times-Bold", fontSize=fs * 1.05,
            leading=fs * 1.05 * 1.2, alignment=ta,
            spaceBefore=fs * 1.2, spaceAfter=fs * 2.2)
    styles["body-center"] = ParagraphStyle(
        "BodyCenter", parent=styles["body"], alignment=TA_CENTER)

    book = book or {}
    doc = BookDocTemplate(
        output, pagesize=page,
        leftMargin=margin, rightMargin=margin,
        topMargin=margin, bottomMargin=margin,
        title=book.get("title") or None,
        author=book.get("author") or None,
    )
    # the usable frame is smaller than page-minus-margins: reportlab frames
    # carry 6pt of internal padding on every side
    frame_w = page[0] - 2 * margin - 12
    frame_h = page[1] - 2 * margin - 12
    story = []

    # --- cover page ---------------------------------------------------------
    cover = book.get("cover")
    if cover:
        cover = os.path.expanduser(_clean_path(cover))
        if os.path.isfile(cover):
            pil = Image.open(cover)
            zoom = min(frame_w / pil.width, frame_h / pil.height)
            flow = RLImage(cover, width=pil.width * zoom,
                           height=pil.height * zoom)
            flow.hAlign = "CENTER"
            story.append(flow)
            story.append(PageBreak())
        else:
            print(f"Cover image not found, skipping: {cover}")

    # --- title page ----------------------------------------------------------
    if book.get("title"):
        rule = dict(width="35%", thickness=0.8, color="black",
                    spaceBefore=fs, spaceAfter=fs)
        story.append(Spacer(1, frame_h * 0.22))
        if book.get("series"):
            story.append(Paragraph(
                _esc(book["series"]).upper(),
                ParagraphStyle("Series", fontName="Times-Roman",
                               fontSize=fs * 1.05, leading=fs * 1.5,
                               alignment=TA_CENTER, textColor="#444444")))
            story.append(Spacer(1, fs * 1.5))
        story.append(HRFlowable(**rule))
        story.append(Paragraph(
            _esc(book["title"]),
            ParagraphStyle("TitlePage", fontName="Times-Bold",
                           fontSize=fs * 2.6, leading=fs * 2.6 * 1.15,
                           alignment=TA_CENTER,
                           spaceBefore=fs, spaceAfter=fs)))
        story.append(HRFlowable(**rule))
        if book.get("author"):
            story.append(Spacer(1, fs * 2.5))
            story.append(Paragraph(
                f"<i>{_esc(book['author'])}</i>",
                ParagraphStyle("Author", fontName="Times-Roman",
                               fontSize=fs * 1.4, leading=fs * 1.4 * 1.3,
                               alignment=TA_CENTER)))
        story.append(PageBreak())

    # --- table of contents ----------------------------------------------------
    chapters = _chapter_starts(paragraphs)
    toc_keys = {i: f"chap{n}" for n, (i, _) in enumerate(chapters)}
    toc_texts = dict(chapters)
    if chapters:
        story.append(Paragraph(
            "Contents",
            ParagraphStyle("TOCTitle", fontName="Times-Bold",
                           fontSize=fs * 1.5, leading=fs * 1.5 * 1.2,
                           alignment=TA_CENTER, spaceAfter=fs * 1.5)))
        toc = TableOfContents()
        toc.dotsMinLevel = 0
        toc.levelStyles = [ParagraphStyle(
            "TOCEntry", fontName="Times-Roman", fontSize=fs,
            leading=fs * 1.9, leftIndent=fs, rightIndent=fs,
            firstLineIndent=-fs * 0.5)]
        story.append(toc)
        story.append(PageBreak())

    prev_kind = None
    for i, para in enumerate(paragraphs):
        if para == "\f":
            story.append(PageBreak())
            prev_kind = None
            continue
        if isinstance(para, str):
            para = {"kind": "body", "markup": _esc(para)}
        # chapters start on a fresh page: break before a chapter label or
        # an h1 that follows page content (but not between the label and
        # its title, and not at the very start of the document)
        if para["kind"] in ("h1", "label") and prev_kind == "body":
            story.append(PageBreak())
        style_key = para["kind"]
        if style_key == "label":
            style_key = f"label-{para.get('align', 'left')}"
        elif style_key == "body" and para.get("align") == "center":
            style_key = "body-center"
        flow = Paragraph(para["markup"], styles[style_key])
        if i in toc_keys:
            flow._toc_entry = (toc_texts[i], toc_keys[i])
        story.append(flow)
        prev_kind = para["kind"]

    if chapters:
        doc.multiBuild(story)  # extra passes settle the TOC page numbers
    else:
        doc.build(story)


def build_image_pdf(images, output):
    """Images only, no OCR."""
    first, rest = images[0], images[1:]
    first.save(output, "PDF", save_all=True, append_images=rest, resolution=150)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def parse_args():
    p = argparse.ArgumentParser(
        description="Capture a window page-by-page, OCR it, and build a PDF.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__.split("Examples:")[1] if "Examples:" in __doc__ else None,
    )
    src = p.add_mutually_exclusive_group()
    src.add_argument("--window", metavar="TITLE",
                     help="capture the first window whose title contains TITLE")
    src.add_argument("--select-region", action="store_true",
                     help="drag-select a screen region instead of picking a window")
    src.add_argument("--region", metavar="X,Y,W,H",
                     help="capture an explicit region, e.g. 100,80,900,1200")

    p.add_argument("-o", "--output", default=None,
                   help="output PDF path (default: the book title, or "
                        "output.pdf, in --dest or the current folder)")
    p.add_argument("--title", default=None,
                   help="book title for the title page and the file name")
    p.add_argument("--author", default=None, help="author for the title page")
    p.add_argument("--series", default=None,
                   help="series line for the title page (optional)")
    p.add_argument("--cover", default=None, metavar="IMAGE",
                   help="image file (e.g. PNG) used as a full cover page "
                        "before the title page")
    p.add_argument("--dest", default=None, metavar="DIR",
                   help="destination folder for the PDF")
    p.add_argument("--edit", metavar="BOOK",
                   help="edit an existing .epub or .pdf instead of scanning: "
                        "list chapters, remove chapter headings (EPUB), "
                        "delete sections/pages")
    p.add_argument("--format", choices=["pdf", "epub", "both"], default=None,
                   help="text mode output: pdf (default), epub, or both")
    p.add_argument("--mode", choices=["text", "searchable", "image"],
                   default="text",
                   help="text: OCR text re-flowed onto clean portrait pages "
                        "(default); searchable: page images + invisible OCR "
                        "text layer; image: page images only")
    p.add_argument("--page-size", choices=["a4", "letter"], default=None,
                   help="page size for text mode (default a4)")
    p.add_argument("--orientation", choices=["portrait", "landscape"],
                   default=None,
                   help="page orientation for text mode (default portrait)")
    p.add_argument("--font-size", type=float, default=None,
                   help="body font size in points for text mode (default 11)")
    p.add_argument("--line-spacing", type=float, default=None,
                   help="line spacing as a multiple of the font size for "
                        "text mode (default 1.4)")
    p.add_argument("--margin", type=float, default=None,
                   help="page margin in cm for text mode (default 2.5)")
    p.add_argument("--epub-vmargin", type=float, default=None, metavar="EM",
                   help="EPUB top/bottom page margin in em (default 1.4)")
    p.add_argument("--epub-hmargin", type=float, default=None, metavar="EM",
                   help="EPUB side page margin in em (default 0.4)")
    p.add_argument("--defaults", action="store_true",
                   help="use default formatting without prompting")
    p.add_argument("--keep-page-breaks", action="store_true",
                   help="text mode: keep original page boundaries instead of "
                        "re-flowing paragraphs across them")
    count = p.add_mutually_exclusive_group()
    count.add_argument("--pages", type=int, default=None,
                       help="capture at most N pages (still auto-stops early "
                            "if the page no longer changes)")
    count.add_argument("--to-end", action="store_true",
                       help="scan until the end of the book (stops when the "
                            "page no longer changes); skips the interactive "
                            "page-count prompt")
    p.add_argument("--key", default="right",
                   help="key sent to turn the page: right, pagedown, space, "
                        "down, enter... (default: right)")
    p.add_argument("--delay", type=float, default=1.5,
                   help="seconds to wait after each page turn (default 1.5)")
    p.add_argument("--start-delay", type=int, default=5,
                   help="countdown before capture starts so you can focus "
                        "the reader window (default 5)")
    p.add_argument("--turn-retries", type=int, default=3,
                   help="attempts per page turn before concluding the book "
                        "ended; the wait grows with each attempt (default 3)")
    p.add_argument("--lang", default="eng",
                   help="Tesseract language code(s), e.g. eng, deu, eng+fra")
    p.add_argument("--min-confidence", type=float, default=None, metavar="N",
                   help="drop paragraphs whose mean OCR confidence is below "
                        "N (default 55; lower keeps more marginal text)")
    p.add_argument("--h1-ratio", type=float, default=None, metavar="X",
                   help="centered text at least X times the body size "
                        "becomes a chapter title (default 1.8)")
    p.add_argument("--h2-ratio", type=float, default=None, metavar="X",
                   help="centered text at least X times the body size "
                        "becomes a subheading (default 1.35)")
    p.add_argument("--save-text", metavar="FILE",
                   help="also write the OCR text to FILE (text/searchable modes)")
    p.add_argument("--save-images", metavar="DIR",
                   help="also save each captured page as PNG into DIR")
    p.add_argument("--tesseract-cmd", metavar="PATH",
                   help="path to the tesseract executable if not on PATH")
    return p.parse_args()


# Common install locations checked when tesseract is not on PATH.
TESSERACT_DEFAULT_PATHS = [
    r"C:\Program Files\Tesseract-OCR\tesseract.exe",
    r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
    os.path.expandvars(r"%LOCALAPPDATA%\Programs\Tesseract-OCR\tesseract.exe"),
    "/usr/local/bin/tesseract",
    "/opt/homebrew/bin/tesseract",
]


def configure_tesseract(explicit_cmd=None):
    """Point pytesseract at a tesseract binary: --tesseract-cmd flag,
    TESSERACT_CMD env var, PATH, then common install locations."""
    import pytesseract

    candidates = [explicit_cmd, os.environ.get("TESSERACT_CMD"), None]
    candidates += TESSERACT_DEFAULT_PATHS
    for cmd in candidates:
        if cmd is not None and not os.path.isfile(cmd):
            continue
        if cmd is not None:
            pytesseract.pytesseract.tesseract_cmd = cmd
        try:
            pytesseract.get_tesseract_version()
            return
        except Exception:
            continue
    sys.exit(
        "Tesseract OCR engine not found. Install it "
        "(https://tesseract-ocr.github.io/tessdoc/Installation.html) "
        "or pass --tesseract-cmd /path/to/tesseract."
    )


def main():
    make_dpi_aware()
    args = parse_args()

    if args.edit:
        edit_book(args.edit)
        return

    if args.mode in ("searchable", "text"):
        configure_tesseract(args.tesseract_cmd)

    # --- choose the capture region -----------------------------------------
    win = None
    if args.region:
        try:
            box = tuple(int(v) for v in args.region.split(","))
            assert len(box) == 4 and box[2] > 0 and box[3] > 0
        except (ValueError, AssertionError):
            sys.exit("--region must be X,Y,W,H with positive width/height.")
    elif args.select_region:
        box = select_region_with_mouse()
    elif args.window:
        box, win = find_window_by_title(args.window)
    else:
        box, win = choose_source_interactive()

    # --- decide how many pages ----------------------------------------------
    if args.pages is None and not args.to_end:
        args.pages = choose_page_count_interactive()
    if args.pages is None:  # end-of-book: rely on identical-page detection
        args.pages = END_OF_BOOK_CAP
        print("Scanning until the end of the book "
              "(stops when the page no longer changes).")
    else:
        print(f"Capturing up to {args.pages} pages.")

    # --- book details and settings (text mode only) --------------------------
    book = resolve_book_info(args) if args.mode == "text" else None
    if args.mode == "text":
        settings = resolve_settings(args)
    else:
        settings = {"format": "pdf", "pdf": dict(FORMAT_DEFAULTS),
                    "epub": dict(EPUB_MARGIN_DEFAULTS), "min_conf": 55,
                    **HEADING_DEFAULTS}
    out_format = settings["format"]
    want_pdf = out_format in ("pdf", "both")
    want_epub = args.mode == "text" and out_format in ("epub", "both")
    fmt = settings["pdf"] if want_pdf else None
    epub_margins = settings["epub"] if want_epub else None
    args.output = resolve_output_path(args, book)
    epub_output = resolve_output_path(args, book, "epub") if want_epub else None
    for path, wanted in ((args.output, want_pdf), (epub_output, want_epub)):
        if wanted and path:
            print(f"Output: {os.path.abspath(path)}")

    # --- capture ------------------------------------------------------------
    images = capture_pages(box, win, args)

    if args.save_images:
        os.makedirs(args.save_images, exist_ok=True)
        for i, img in enumerate(images, 1):
            img.save(os.path.join(args.save_images, f"page_{i:04d}.png"))
        print(f"Saved page images to {args.save_images}/")

    # --- build PDF ----------------------------------------------------------
    print(f"Building {args.mode} PDF...")
    if args.mode == "searchable":
        build_searchable_pdf(images, args.output, args.lang)
        if args.save_text:
            texts = extract_text(images, args.lang)
            with open(args.save_text, "w", encoding="utf-8") as f:
                f.write("\n\n\f\n\n".join(texts))
            print(f"Text written to {args.save_text}")
    elif args.mode == "text":
        pages = extract_structured(images, args.lang, settings["min_conf"],
                                   settings["h1_ratio"], settings["h2_ratio"])
        pages = strip_headers_footers(pages)
        paragraphs = reflow_paragraphs(pages, args.keep_page_breaks)
        if want_pdf:
            build_text_pdf(paragraphs, args.output, fmt, book)
        if want_epub:
            build_epub(paragraphs, epub_output, book, args.lang, epub_margins)
            size_kb = os.path.getsize(epub_output) / 1024
            print(f"Done: {epub_output} ({size_kb:.0f} KB)")
        if not want_pdf:
            args.output = None  # skip the PDF size line below
        if args.save_text:
            with open(args.save_text, "w", encoding="utf-8") as f:
                f.write("\n\n".join(p["plain"] for p in paragraphs
                                    if isinstance(p, dict) and p["plain"]))
            print(f"Text written to {args.save_text}")
    else:
        build_image_pdf(images, args.output)

    if args.output:
        size_kb = os.path.getsize(args.output) / 1024
        print(f"Done: {args.output} ({len(images)} pages, {size_kb:.0f} KB)")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nInterrupted.")
        sys.exit(130)
