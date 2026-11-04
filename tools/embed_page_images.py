# tools/embed_page_images.py — put the generated sample images into docs/index.html (PUB-16)
#
#   python tools/render_sample_sheet.py    # writes docs/assets/sample_digest.png and docs/assets/sample_sheet.png
#   python tools/embed_page_images.py      # replaces the page's two embedded images with them
#   python tools/embed_page_images.py --check   # exit 1 if the page does not carry the current PNGs
#
# The Pages site is one self-contained file, so its two images are data: URIs.
# Each is found by the start of its alt text, never by position, and replaced
# whole; its width attribute becomes the PNG's CSS width (pixels / scale) and
# any height attribute is dropped, so the page keeps the image's own aspect.
# Nothing else in the page changes.

import argparse
import base64
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PAGE = ROOT / "docs" / "index.html"

# (alt text starts with, PNG, the scale it was rendered at)
IMAGES = (
    ("A sample digest email", ROOT / "docs" / "assets" / "sample_digest.png", 1),
    ("A Google Sheet", ROOT / "docs" / "assets" / "sample_sheet.png", 1.5),
)

IMG_RE = re.compile(r'<img\b[^>]*>', re.S)


def png_width(path):
    return int.from_bytes(path.read_bytes()[16:20], "big")


def embed(page, alt_start, png, scale):
    """The page with the <img> whose alt starts with alt_start carrying png."""
    tags = [m for m in IMG_RE.finditer(page) if re.search(r'\balt="' + re.escape(alt_start), m.group(0))]
    if len(tags) != 1:
        raise SystemExit(f'{PAGE.relative_to(ROOT)}: expected one <img> with alt starting "{alt_start}", '
                         f"found {len(tags)}")
    tag = tags[0].group(0)
    uri = "data:image/png;base64," + base64.b64encode(png.read_bytes()).decode()
    new = re.sub(r'src="data:image/png;base64,[^"]*"', f'src="{uri}"', tag)
    new = re.sub(r'\swidth="\d+"', f' width="{round(png_width(png) / scale)}"', new)
    new = re.sub(r'\sheight="\d+"', "", new)
    return page.replace(tag, new)


def build(page):
    for alt_start, png, scale in IMAGES:
        if not png.exists():
            raise SystemExit(f"{png.relative_to(ROOT)} is missing: run tools/render_sample_sheet.py first")
        page = embed(page, alt_start, png, scale)
    return page


def main(argv=None):
    parser = argparse.ArgumentParser(description="Embed the generated sample PNGs in docs/index.html (PUB-16)")
    parser.add_argument("--check", action="store_true", help="report whether the page is current; write nothing")
    args = parser.parse_args(argv)
    page = PAGE.read_text()
    new = build(page)
    if args.check:
        current = new == page
        print(f"{PAGE.relative_to(ROOT)}: {'current' if current else 'out of date: run tools/embed_page_images.py'}")
        return 0 if current else 1
    PAGE.write_text(new)
    print(f"[Embed] {PAGE.relative_to(ROOT)}: " + ", ".join(
        f"{png.name} ({png_width(png)} px at {scale}x)" for _, png, scale in IMAGES))
    return 0


if __name__ == "__main__":
    sys.exit(main())
