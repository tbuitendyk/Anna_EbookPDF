# Anna_EbookPDF

Convert on-screen digital text (e-book readers, document viewers, web pages)
into a PDF using **screen capture + OCR**. You pick the window that shows the
text; the tool captures each page, sends a keystroke to turn the page
automatically, and stops by itself when it detects the last page (two
identical captures in a row). All pages are then assembled into a PDF with
[pytesseract](https://github.com/madmaze/pytesseract).

> Only use this on content you have the right to copy (your own documents,
> public-domain or DRM-free material, personal backups where permitted).

## Requirements

1. **Python 3.9+**
2. **Tesseract OCR engine** (the `pytesseract` package is only a wrapper):
   - Windows: <https://github.com/UB-Mannheim/tesseract/wiki>
   - macOS: `brew install tesseract`
   - Linux: `sudo apt install tesseract-ocr`
3. Python packages:

   ```bash
   pip install -r requirements.txt
   ```

Platform notes:

- **macOS**: grant your terminal *Screen Recording* and *Accessibility*
  permissions (System Settings → Privacy & Security), otherwise captures come
  out black and keystrokes are blocked.
- **Linux**: works on X11. On Wayland, screen capture of other windows is
  restricted — log into an X11 session or use `--region`.
- Window listing/matching uses PyGetWindow (fully supported on Windows). Where
  it isn't available the tool falls back to drag-selecting a screen region.

## Usage

```bash
# Fully interactive: asks whether to pick a window or drag-select a region,
# then asks for a page count (or press Enter to scan to the end of the book)
python ebook2pdf.py -o book.pdf

# Match a window by title, capture at most 120 pages
python ebook2pdf.py --window kindle --pages 120 -o book.pdf

# Scan to the end of the book with no prompts
python ebook2pdf.py --window kindle --to-end -o book.pdf

# Drag a rectangle around just the text area; turn pages with the space bar
python ebook2pdf.py --select-region --key space -o book.pdf

# Explicit region (X,Y,W,H), slower reader that needs 3s per page
python ebook2pdf.py --region 100,80,900,1200 --delay 3 -o book.pdf

# Re-flowed text-only PDF + a plain .txt dump, German OCR
python ebook2pdf.py --mode text --lang deu --save-text book.txt -o book.pdf
```

After you start the tool there is a short countdown (default 5 s) — click on
the reader window during the countdown so it receives the page-turn
keystrokes. To abort mid-capture, slam the mouse into any screen corner
(PyAutoGUI failsafe) or press `Ctrl+C` in the terminal.

## Output modes (`--mode`)

| Mode         | Result                                                                 |
|--------------|------------------------------------------------------------------------|
| `searchable` | **Default.** Page images with an invisible OCR text layer — looks identical to the source, but text is selectable/searchable. |
| `text`       | OCR text re-flowed into a clean A4 PDF (smaller file, loses layout).    |
| `image`      | Page images only, no OCR.                                               |

## Options

| Option                  | Default      | Description                                          |
|-------------------------|--------------|------------------------------------------------------|
| `-o, --output`          | `output.pdf` | Output PDF path                                      |
| `--window TITLE`        | —            | Capture first window whose title contains TITLE      |
| `--select-region`       | —            | Drag-select the capture area on screen               |
| `--region X,Y,W,H`      | —            | Explicit capture rectangle                           |
| `--pages N`             | ask          | Capture at most N pages (auto-stops early at the last page) |
| `--to-end`              | ask          | Scan until the end of the book, no page-count prompt |
| `--key KEY`             | `right`      | Page-turn key: `right`, `pagedown`, `space`, ...     |
| `--delay SEC`           | 1.5          | Wait after each page turn (raise for slow readers)   |
| `--start-delay SEC`     | 5            | Countdown before capture starts                      |
| `--stop-after-repeats N`| 2            | Stop after N identical captures in a row             |
| `--lang CODE`           | `eng`        | Tesseract language(s), e.g. `eng`, `deu`, `eng+fra`  |
| `--save-text FILE`      | —            | Also dump the OCR text to a file                     |
| `--save-images DIR`     | —            | Also save every captured page as PNG                 |
| `--tesseract-cmd PATH`  | —            | Tesseract executable if not on PATH                  |

## Tips for good OCR

- Make the reader window as large as possible and use a comfortable font size —
  more pixels per letter means better OCR.
- Capture just the text area (`--select-region`) to keep toolbars, page
  numbers, and margins out of the OCR.
- If pages render slowly (animations, e-ink emulation), increase `--delay`.
- Install the right Tesseract language pack (e.g. `tesseract-ocr-deu`) and
  pass it with `--lang`.
