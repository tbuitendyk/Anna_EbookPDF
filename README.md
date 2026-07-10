# Anna_EbookPDF

Convert on-screen digital text (e-book readers, document viewers, web pages)
into a PDF using **screen capture + OCR**. You pick the window that shows the
text; the tool captures each page, sends a keystroke to turn the page
automatically, and stops by itself at the end of the book. A slow or dropped
page turn is retried (3 attempts with a growing wait) before the tool
concludes the book is finished, and captured fingerprints guarantee no page
appears twice. All pages are then assembled into a PDF with
[pytesseract](https://github.com/madmaze/pytesseract). In the default text
mode each chapter starts on a fresh page, matching the source book.

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

- **Windows**: the tool declares itself DPI-aware so captures are correct
  with display scaling (125%, 150%, ...). If a capture still looks cropped,
  use `--select-region` to drag the exact area.
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
| `text`       | **Default.** OCR text re-flowed onto clean standard pages. Running headers/footers and reader chrome (page counters, progress %) are stripped, and paragraphs that continue across a page turn are joined so the text flows naturally. Source formatting is matched where possible: chapter numbers and titles become large centered headings (detected from their relative text size), italic passages stay italic (detected from glyph slant), and intentional line breaks — title/subtitle lines, place-and-date blocks, verse — are kept, since a line that stops well short of the right margin mid-paragraph was broken on purpose. Low-confidence OCR is discarded (words below confidence 20, paragraphs averaging below `--min-confidence`, default 55), which keeps artwork and decorative pages from injecting gibberish. Use `--keep-page-breaks` to preserve the original page boundaries instead. |
| `searchable` | Page images with an invisible OCR text layer — looks identical to the source, but text is selectable/searchable. |
| `image`      | Page images only, no OCR.                                               |

In text mode all conversion settings are offered as a single summary —
output format, PDF layout (page size, orientation, font size, line spacing,
margins), EPUB margins, the OCR confidence filter, and the heading-detection
sizes. Press Enter to proceed with the defaults, or enter `1` to walk through
them (only the sections relevant to the chosen format are asked). Pass
`--defaults` (or any setting flag) to skip the prompt in scripted runs.

Text mode can produce a **PDF, an EPUB, or both** — set at the settings
prompt, with `--format pdf|epub|both`, or via `"format"` in the config
file. The EPUB carries the same structure as
the PDF: cover, title page, chapters with headings kept, italics, centered
poem/epigraph lines, a navigation table of contents, and
title/author/series metadata (Calibre-compatible series tags).

Text mode also asks for the book's details during setup: title, author,
optional series (with a book number), an optional cover image, and a
destination folder. The PDF then opens with the cover image (if given), a
stylized title page, and a clickable table of contents listing every chapter
(also added as PDF bookmarks/outline). The file is named after the title —
`<dest>/<Title>.pdf` — unless `-o` is given explicitly. The corresponding
flags (`--title`, `--author`, `--series`, `--cover`, `--dest`) or
`--defaults` skip these prompts too.

The EPUB stylesheet declares page margins several ways (`@page` margins,
`body` padding, and a top offset on each chapter) so the text doesn't sit
flush against the top of the screen — readers differ in which declaration
they honor, so all are included. Set them at the settings prompt or with
`--epub-vmargin` / `--epub-hmargin`. Heading detection is adjustable the
same way: centered text at least `--h1-ratio` (default 1.8) times the body
size becomes a chapter title, at least `--h2-ratio` (default 1.35) a
subheading — raise them if a book's decorated text keeps being mistaken
for headings.

## Editing an existing book

```bash
python ebook2pdf.py --edit "C:\Books\My Great Book.epub"
python ebook2pdf.py --edit "C:\Books\My Great Book.pdf"
```

Lists the book's chapters and offers, for **EPUB**: remove a chapter's
heading (its text merges into the previous section — for headings that were
detected by mistake), delete a section entirely, fix the page margins — the
margin rules are written into the book's stylesheet, replacing any margins
this tool set before, so older EPUBs can be repaired in place — change the
cover image (replaces the embedded cover; sets one if the book has none),
or add a chapter heading: pick the paragraph where the chapter starts and
the section is split there with a new heading and contents entry. For
**PDF**: remove pages by number/range (`3,5-7`), remove a whole chapter's
pages (from its bookmark to the next chapter), change the cover page
(replace page 1 with a new cover image, or insert one if the book has no
cover), or add a chapter bookmark (title + page number). Cover prompts default to the newest image in the
configured covers folder. Changes are saved to `<name>-edited.epub/pdf` by
default; enter a path (including the original) to override.

## Personal defaults (`ebook2pdf_config.json`)

A config file next to the script stores your defaults:

```json
{
  "dest": "C:\\Users\\Bee2026\\Documents\\Ebooks\\Books",
  "cover_dir": "C:\\Users\\Bee2026\\Documents\\Ebooks\\Covers",
  "authors": [],
  "series": []
}
```

- `dest` — default destination folder, offered at the prompt (Enter accepts)
  and used automatically in `--defaults`/flag runs.
- `cover_dir` — the newest image in this folder is offered as a one-key
  choice at the cover prompt (Enter = no cover, `1` = newest, or a path).
- `authors` / `series` — remembered names shown as numbered lists at the
  prompts; pick by number or type a new name. New names are saved back
  automatically. Picking a series also asks for the book number in the
  series and builds the title-page line (`My Saga, Book 3`).

## Options

| Option                  | Default      | Description                                          |
|-------------------------|--------------|------------------------------------------------------|
| `-o, --output`          | title-based  | Output PDF path (default `<dest>/<Title>.pdf`)       |
| `--title TITLE`         | ask          | Book title (title page + file name)                  |
| `--author NAME`         | ask          | Author for the title page                            |
| `--series TEXT`         | ask          | Series line for the title page                       |
| `--cover IMAGE`         | ask          | Cover image file for the first page                  |
| `--dest DIR`            | ask          | Destination folder for the PDF                       |
| `--format F`            | ask          | Text mode output: `pdf`, `epub`, or `both`           |
| `--window TITLE`        | —            | Capture first window whose title contains TITLE      |
| `--select-region`       | —            | Drag-select the capture area on screen               |
| `--region X,Y,W,H`      | —            | Explicit capture rectangle                           |
| `--pages N`             | ask          | Capture at most N pages (auto-stops early at the last page) |
| `--to-end`              | ask          | Scan until the end of the book, no page-count prompt |
| `--key KEY`             | `right`      | Page-turn key: `right`, `pagedown`, `space`, ...     |
| `--delay SEC`           | 1.5          | Wait after each page turn (raise for slow readers)   |
| `--start-delay SEC`     | 5            | Countdown before capture starts                      |
| `--turn-retries N`      | 3            | Attempts per page turn (growing wait) before ending  |
| `--page-size SIZE`      | ask/`a4`     | Text mode page size: `a4` or `letter`                |
| `--orientation O`       | ask/`portrait` | Text mode: `portrait` or `landscape`               |
| `--font-size PT`        | ask/`11`     | Text mode body font size in points                   |
| `--line-spacing X`      | ask/`1.4`    | Text mode line spacing (multiple of font size)       |
| `--margin CM`           | ask/`2.5`    | Text mode page margins in cm                         |
| `--defaults`            | off          | Use default formatting without prompting             |
| `--keep-page-breaks`    | off          | Text mode: keep original page boundaries             |
| `--lang CODE`           | `eng`        | Tesseract language(s), e.g. `eng`, `deu`, `eng+fra`  |
| `--save-text FILE`      | —            | Also dump the OCR text to a file                     |
| `--save-images DIR`     | —            | Also save every captured page as PNG                 |
| `--tesseract-cmd PATH`  | auto         | Tesseract executable. If omitted, the tool checks the `TESSERACT_CMD` env var, PATH, then common install locations (incl. `C:\Program Files\Tesseract-OCR\tesseract.exe`) |

## Tips for good OCR

- Make the reader window as large as possible and use a comfortable font size —
  more pixels per letter means better OCR.
- Capture just the text area (`--select-region`) to keep toolbars, page
  numbers, and margins out of the OCR.
- If pages render slowly (animations, e-ink emulation), increase `--delay`.
- Install the right Tesseract language pack (e.g. `tesseract-ocr-deu`) and
  pass it with `--lang`.
