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
import sys
import tempfile
import time

from PIL import Image

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


def build_text_pdf(texts, output):
    """Re-flow the OCR text into a clean A4 PDF."""
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.lib.units import cm
    from reportlab.platypus import PageBreak, Paragraph, SimpleDocTemplate, Spacer

    styles = getSampleStyleSheet()
    body = styles["BodyText"]
    body.fontSize = 11
    body.leading = 15

    doc = SimpleDocTemplate(
        output, pagesize=A4,
        leftMargin=2.5 * cm, rightMargin=2.5 * cm,
        topMargin=2.5 * cm, bottomMargin=2.5 * cm,
    )
    story = []
    for i, text in enumerate(texts):
        for para in text.split("\n\n"):
            para = para.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            para = para.replace("\n", " ").strip()
            if para:
                story.append(Paragraph(para, body))
                story.append(Spacer(1, 6))
        if i < len(texts) - 1:
            story.append(PageBreak())
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
    p.add_argument("--mode", choices=["searchable", "text", "image"],
                   default="searchable",
                   help="searchable: images + invisible OCR text layer (default); "
                        "text: re-flowed OCR text; image: images only")
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


def main():
    args = parse_args()

    if args.tesseract_cmd:
        import pytesseract
        pytesseract.pytesseract.tesseract_cmd = args.tesseract_cmd

    if args.mode in ("searchable", "text"):
        import pytesseract
        try:
            pytesseract.get_tesseract_version()
        except Exception:
            sys.exit(
                "Tesseract OCR engine not found. Install it "
                "(https://tesseract-ocr.github.io/tessdoc/Installation.html) "
                "or pass --tesseract-cmd /path/to/tesseract."
            )

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
        texts = extract_text(images, args.lang)
        build_text_pdf(texts, args.output)
        if args.save_text:
            with open(args.save_text, "w", encoding="utf-8") as f:
                f.write("\n\n\f\n\n".join(texts))
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
