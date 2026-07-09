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
    prev_fp = None
    repeats = 0

    with mss.mss() as sct:
        for page in range(1, args.pages + 1):
            img = grab(sct, box)
            fp = image_fingerprint(img)

            if fp == prev_fp:
                repeats += 1
                print(f"Page {page}: identical to previous capture "
                      f"({repeats}/{args.stop_after_repeats}).")
                if repeats >= args.stop_after_repeats:
                    print("Reached the last page — stopping.")
                    break
            else:
                repeats = 0
                images.append(img)
                print(f"Captured page {len(images)}", end="\r", flush=True)
            prev_fp = fp

            if page < args.pages:
                pyautogui.press(args.key)
                time.sleep(args.delay)

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

# Shear angles (tangents) treated as italic candidates. Typical italics
# lean 10-15 degrees (tangent 0.18-0.27).
ITALIC_SHEARS = [0.15, 0.20, 0.25, 0.30]

# Slant can only be measured on letters with vertical stems. A word made
# entirely of round/diagonal letterforms ("way", "eyes", "see") gives a
# noise answer, so it stays undecided and inherits from its neighbors.
_VERTICAL_STEM_CHARS = set("bdfhijklmnpqrtu" "BDEFHIJKLMNPRTU" "14")


def _norm_line(line):
    return re.sub(r"\s+", " ", line.strip().lower())


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


def extract_structured(images, lang):
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
            if not d["text"][j].strip() or conf(d, j) < 0:
                continue
            key = (d["block_num"][j], d["par_num"][j])
            paras.setdefault(key, {}).setdefault(d["line_num"][j], []).append(j)

        raw_paras = []
        for key, lines in paras.items():
            line_recs = []
            for ln in sorted(lines):
                texts, flags, heights = [], [], []
                left = right = None
                for j in lines[ln]:
                    text = d["text"][j].strip()
                    box = (d["left"][j], d["top"][j],
                           d["left"][j] + d["width"][j],
                           d["top"][j] + d["height"][j])
                    left = box[0] if left is None else min(left, box[0])
                    right = box[2] if right is None else max(right, box[2])
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
                    "left": left, "right": right, "heights": heights,
                })
            if line_recs:
                raw_paras.append(line_recs)

        # The right margin of the text column: justified wrapped lines
        # reach it, so a line ending well short of it is an intentional
        # break (heading lines, place/date blocks, verse) — split there.
        page_paras = []
        if raw_paras:
            col_right = max(r["right"] for lr in raw_paras for r in lr)
            col_left = min(r["left"] for lr in raw_paras for r in lr)
            short_cut = col_right - 0.15 * max(col_right - col_left, 1)

        for line_recs in raw_paras:
            segments, current = [], []
            for i, rec in enumerate(line_recs):
                current.append(rec)
                is_last = i == len(line_recs) - 1
                if not is_last and rec["right"] < short_cut:
                    segments.append(current)
                    current = []
            if current:
                segments.append(current)

            for seg in segments:
                plain = _join_lines([r["plain"] for r in seg])
                markup = _join_lines([r["markup"] for r in seg])
                if not plain.strip():
                    continue
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
                })

        # Headings must be larger than body text AND centered on the page —
        # a lone tall-glyphed word at the paragraph indent ("Tidy?") is
        # body text, not a heading.
        page_center = img.width / 2
        for p in page_paras:
            center = (p["_left"] + p["_right"]) / 2
            width = p["_right"] - p["_left"]
            centered = (abs(center - page_center) < 0.05 * img.width
                        and width < 0.7 * img.width)
            if centered and len(p["plain"]) < 80:
                if p["_ratio"] >= 1.7:
                    p["kind"] = "h1"
                elif p["_ratio"] >= 1.25:
                    p["kind"] = "h2"
            del p["_ratio"], p["_left"], p["_right"]
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


def resolve_formatting(args):
    """Return the formatting dict for text mode. Formatting flags on the
    command line (or --defaults) skip the prompt; otherwise offer:
    Enter = defaults, 1 = customize."""
    explicit = {k: getattr(args, k) for k in FORMAT_DEFAULTS}
    if args.defaults or any(v is not None for v in explicit.values()):
        return {k: v if v is not None else FORMAT_DEFAULTS[k]
                for k, v in explicit.items()}

    fmt = dict(FORMAT_DEFAULTS)
    print(f"\nFormatting defaults: {fmt['page_size'].upper()} "
          f"{fmt['orientation']}, {fmt['font_size']:g} pt font, "
          f"{fmt['line_spacing']:g} line spacing, {fmt['margin']:g} cm margins")
    if input("Press Enter to use defaults, or 1 to customize: ").strip() == "1":
        fmt["page_size"] = _ask_choice("Page size", ["a4", "letter"], "a4")
        fmt["orientation"] = _ask_choice("Orientation",
                                         ["portrait", "landscape"], "portrait")
        fmt["font_size"] = _ask_number("Font size (pt)", 11.0)
        fmt["line_spacing"] = _ask_number("Line spacing (x font size)", 1.4)
        fmt["margin"] = _ask_number("Margins (cm)", 2.5)
    return fmt


def build_text_pdf(paragraphs, output, fmt=None):
    """Lay the cleaned paragraphs out on standard pages, styling headings
    and italics to match the source."""
    from reportlab.lib.enums import TA_CENTER, TA_JUSTIFY
    from reportlab.lib.pagesizes import A4, landscape, letter
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import cm
    from reportlab.platypus import PageBreak, Paragraph, SimpleDocTemplate

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

    doc = SimpleDocTemplate(
        output, pagesize=page,
        leftMargin=margin, rightMargin=margin,
        topMargin=margin, bottomMargin=margin,
    )
    story = []
    for para in paragraphs:
        if para == "\f":
            story.append(PageBreak())
            continue
        if isinstance(para, str):
            para = {"kind": "body", "markup": _esc(para)}
        story.append(Paragraph(para["markup"], styles[para["kind"]]))
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

    p.add_argument("-o", "--output", default="output.pdf", help="output PDF path")
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
    p.add_argument("--stop-after-repeats", type=int, default=2,
                   help="stop after N consecutive identical captures (default 2)")
    p.add_argument("--lang", default="eng",
                   help="Tesseract language code(s), e.g. eng, deu, eng+fra")
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

    # --- formatting (text mode only) ----------------------------------------
    fmt = resolve_formatting(args) if args.mode == "text" else None

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
        pages = extract_structured(images, args.lang)
        pages = strip_headers_footers(pages)
        paragraphs = reflow_paragraphs(pages, args.keep_page_breaks)
        build_text_pdf(paragraphs, args.output, fmt)
        if args.save_text:
            with open(args.save_text, "w", encoding="utf-8") as f:
                f.write("\n\n".join(p["plain"] for p in paragraphs
                                    if isinstance(p, dict)))
            print(f"Text written to {args.save_text}")
    else:
        build_image_pdf(images, args.output)

    size_kb = os.path.getsize(args.output) / 1024
    print(f"Done: {args.output} ({len(images)} pages, {size_kb:.0f} KB)")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nInterrupted.")
        sys.exit(130)
