# tools/render_sample_sheet.py — the sample digest and Sheet snapshot the docs show (PUB-16)
#
#   python tools/render_sample_sheet.py --refresh   # score examples/postings (real calls, about nine) and save the results
#   python tools/render_sample_sheet.py             # render from the saved results, no model calls
#   python tools/embed_page_images.py               # then put the two PNGs into docs/index.html
#
# Always runs under profile.example.yaml, whatever JOB_DIGEST_PROFILE says, so
# nothing of the maintainer's profile reaches the docs. --refresh runs the
# pipeline's own calibrate steps on examples/postings and saves every scored posting
# to docs/assets/sample_calibrate.json; the renders read only that file, so both
# images show the same scores and are reproducible without an API key.
#
# Writes:
#   docs/assets/sample_digest.html / .png   the digest dry_run.build makes from those
#                                    postings; PNG 900 CSS px wide at 1x
#   docs/assets/sample_sheet.html / .png    the same postings in the Sheet's columns
#                                    (sheet_layout: SHEET_COLUMNS, OPTIONAL_COLUMNS,
#                                    DISQUALIFIED_COLUMNS), styled like a
#                                    spreadsheet; PNG 1400 CSS px wide at 1.5x
#
# The Sheet snapshot is an illustration. Rows and cells come from
# sheets.main_row_data, as the digest writes them, with three exceptions that
# are made up so the Status column shows its use: the highest-scoring row is
# Applied, the second Interested, and the lowest-scoring Declined (with a short
# note), each dated the day the results were scored. Postings dropped before
# scoring (outside the US) never reach the Sheet, so they are not shown; the
# Disqualified tab holds the hard blocks and dealbreakers.
#
# PNGs are taken with headless Google Chrome (CHROME, or the macOS default
# path): the page height is measured first, so the image has no empty tail.

import argparse
import html
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import date, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Before any engine import: the example profile, and no real address in the footer.
os.environ["JOB_DIGEST_PROFILE"] = str(ROOT / "profile.example.yaml")
os.environ["GMAIL_ADDRESS"] = "you@example.com"
for _name in ("GMAIL_APP_PASSWORD", "GOOGLE_SHEET_ID", "GOOGLE_SERVICE_ACCOUNT_JSON"):
    os.environ.setdefault(_name, "")

SAMPLES = ROOT / "examples" / "postings"
RESULTS = ROOT / "docs" / "assets" / "sample_calibrate.json"
DIGEST_HTML = ROOT / "docs" / "assets" / "sample_digest.html"
DIGEST_PNG = ROOT / "docs" / "assets" / "sample_digest.png"
SHEET_HTML = ROOT / "docs" / "assets" / "sample_sheet.html"
SHEET_PNG = ROOT / "docs" / "assets" / "sample_sheet.png"
DIGEST_WIDTH, DIGEST_SCALE = 900, 1
SHEET_WIDTH, SHEET_SCALE = 1400, 1.5
CHROME_DEFAULT = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"

# Illustrative statuses, by rank among the main-tab rows (see the header).
ILLUSTRATIVE = (("Applied", 0), ("Interested", 1), ("Declined", -1))

# CSS px per column, sized so every main-tab column (Dream last) fits the
# 1400 px frame; the rest goes to the rationale, clipped as a narrow cell is.
WIDTHS = {"Job Title": 124, "Company": 140, "Location": 76, "Work Model": 56, "Comp": 66, "Score": 42,
          "Source": 52, "Date First Seen": 58, "Apply Link": 50, "Status": 60, "Applied Date": 58,
          "Notes": 66, "Warm Contact": 40, "Relo": 34, "Tier": 36, "Posted Date": 48, "Status Date": 58,
          "Function Fit": 64, "Criteria": 56, "Dream": 44, "Disqualify Reason": 300}
ROW_NUMBER_WIDTH = 30


# --- results ---------------------------------------------------------------------------

def refresh(path=RESULTS):
    """Score examples/postings under the example profile (real calls) and save every
    posting; prints calibrate's table."""
    import calibrate
    import profile_loader
    ranked, calls = calibrate.run(str(SAMPLES))
    print(calibrate.render(calibrate.rows(ranked, profile_loader.active()), calls,
                           calibrate.cost_summary(calibrate.LAST_USAGE)))
    data = {"scored_on": date.today().isoformat(), "model": calibrate.LAST_USAGE.get("model"),
            "claude_calls": calibrate.LAST_USAGE.get("calls"), "postings_scored": calls, "jobs": ranked}
    path.write_text(json.dumps(data, indent=1, ensure_ascii=False, default=str) + "\n")
    print(f"[Samples] wrote {path.relative_to(ROOT)}: {len(ranked)} postings, {calls} scored, "
          f"{data['claude_calls']} Claude calls")
    return data


def load(path=RESULTS):
    if not path.exists():
        sys.exit(f"{path.relative_to(ROOT)} is missing: run with --refresh first (needs ANTHROPIC_API_KEY)")
    return json.loads(path.read_text())


def _on_sheet(jobs):
    """The postings the digest writes to the Sheet: everything scored (the
    location filter drops non-US postings before that), each named for its
    employer the way the digest names it (FIL-13: "<client> (via <poster>)")."""
    import state
    out = [dict(j) for j in jobs if not j.get("international")]
    for job in out:
        state.name_employer(job)
    return out


# --- the digest ------------------------------------------------------------------------

def render_digest(data):
    import dry_run
    import emailer
    from scorer import filter_jobs
    scored_on = datetime.strptime(data["scored_on"], "%Y-%m-%d")
    emailer._today = lambda: scored_on.strftime("%B %d, %Y")
    jobs = [dict(j, date_posted=j.get("date_posted", "")) for j in _on_sheet(data["jobs"])]
    _, page = dry_run.build(filter_jobs(jobs))
    DIGEST_HTML.write_text(page)
    return page


# --- the Sheet -------------------------------------------------------------------------

def _letters(n):
    out = ""
    while n:
        n, r = divmod(n - 1, 26)
        out = chr(65 + r) + out
    return out


def _band(header, value):
    if header != "Score" or value in ("", None):
        return ""
    score = int(value)
    if score == 0:
        return "s0"
    return "s7" if score >= 7 else "s5" if score >= 5 else "s1"


def _cell(header, value):
    from sheets import cell_value
    value = cell_value(header, value)
    if isinstance(value, (int, float)) and header in ("Date First Seen", "Applied Date", "Posted Date", "Status Date"):
        value = (date(1899, 12, 30).toordinal() + int(value))
        value = date.fromordinal(value).strftime("%-m/%-d/%Y")
    text = html.escape(str(value if value is not None else ""))
    cls = " ".join(filter(None, [_band(header, value), "num" if header in ("Score",) else ""]))
    return f'<td class="{cls}" title="{text}">{text}</td>' if cls else f'<td title="{text}">{text}</td>'


def _table(headers, rows, first_row=1):
    widths = [WIDTHS.get(h, 0) for h in headers]
    fixed = sum(w for w in widths if w)
    flex = max(90, SHEET_WIDTH - ROW_NUMBER_WIDTH - fixed - 2)
    widths = [w or flex for w in widths]
    cols = f'<col style="width:{ROW_NUMBER_WIDTH}px">' + "".join(f'<col style="width:{w}px">' for w in widths)
    letters = '<tr class="letters"><th class="corner"></th>' + "".join(
        f"<th>{_letters(i + 1)}</th>" for i in range(len(headers))) + "</tr>"
    head = f'<tr class="head"><th class="rn">{first_row}</th>' + "".join(
        f"<td>{html.escape(h)}</td>" for h in headers) + "</tr>"
    body = "".join(f'<tr><th class="rn">{first_row + i + 1}</th>' + "".join(_cell(h, r.get(h, "")) for h in headers)
                   + "</tr>" for i, r in enumerate(rows))
    return f"<table><colgroup>{cols}</colgroup>{letters}{head}{body}</table>"


def sheet_rows(data):
    """(main-tab rows, Disqualified rows) as {header: value}, with the
    illustrative statuses applied (see the header)."""
    import sheets
    from scorer import is_disqualified
    scored_on = data["scored_on"]
    main, disqualified = [], []
    for job in _on_sheet(data["jobs"]):
        job = dict(job, found_at=scored_on)
        row = sheets.main_row_data(job)
        if is_disqualified(job):
            row.update({"Score": 0, "Disqualify Reason": job.get("rationale", "")})
            disqualified.append(row)
        else:
            main.append(row)
    main.sort(key=lambda r: -int(r["Score"] or 0))
    for status, index in ILLUSTRATIVE:
        if len(main) <= max(index, -index - 1):
            continue
        row = main[index]
        row.update({"Status": status, "Status Date": scored_on})
        if status == "Applied":
            row["Applied Date"] = scored_on
        if status == "Declined":
            row["Notes"] = "Not relocating." if row.get("Relo") == "Y" else "Not the right fit."
    return main, disqualified


STYLE = """
* { box-sizing: border-box; }
body { margin: 0; background: #ffffff; font-family: Roboto, Arial, Helvetica, sans-serif; font-size: 11px; color: #202124; }
.frame { width: %(width)dpx; overflow: hidden; }
.bar { display: flex; align-items: center; gap: 10px; padding: 10px 12px; border-bottom: 1px solid #dadce0; background: #f9fbfd; }
.bar .icon { width: 22px; height: 22px; border-radius: 4px; background: #0f9d58; position: relative; }
.bar .icon::after { content: ""; position: absolute; inset: 6px 5px; border: 2px solid #fff; border-radius: 1px; }
.bar .name { font-size: 15px; color: #202124; }
.bar .note { margin-left: auto; color: #5f6368; font-size: 11px; }
.formula { height: 26px; border-bottom: 1px solid #dadce0; display: flex; align-items: center; padding-left: 12px; color: #5f6368; font-style: italic; }
table { border-collapse: collapse; table-layout: fixed; width: %(width)dpx; }
th, td { border: 1px solid #e2e3e3; height: 22px; padding: 0 4px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; text-align: left; font-weight: 400; }
tr.letters th { background: #f8f9fa; color: #5f6368; text-align: center; font-size: 11px; height: 20px; }
th.corner, th.rn { background: #f8f9fa; color: #5f6368; text-align: center; font-size: 11px; padding: 0; }
tr.head td { background: #f3f3f3; font-weight: 700; }
td.num { text-align: right; font-weight: 600; }
td.s7 { background: #e1f5ee; color: #0f6e56; }
td.s5 { background: #e6f1fb; color: #185fa5; }
td.s1 { background: #f1efe8; color: #6b6a65; }
td.s0 { background: #fcebeb; color: #a32d2d; }
.tabs { display: flex; gap: 2px; padding: 0 0 0 46px; background: #f1f3f4; border-top: 1px solid #dadce0; height: 32px; align-items: stretch; }
.tabs span { padding: 7px 18px; color: #5f6368; font-size: 12px; }
.tabs span.on { background: #ffffff; color: #188038; font-weight: 600; border-bottom: 3px solid #188038; }
.label { padding: 18px 12px 8px; font-size: 13px; font-weight: 600; color: #3c4043; }
"""


def render_sheet(data):
    import sheet_layout
    main, disqualified = sheet_rows(data)
    headers = sheet_layout.SHEET_COLUMNS + sheet_layout.OPTIONAL_COLUMNS
    page = f"""<!DOCTYPE html>
<html lang="en">
<head><meta charset="utf-8"><title>Sample Sheet snapshot</title>
<style>{STYLE % {"width": SHEET_WIDTH}}</style></head>
<body>
<div class="frame">
  <div class="bar"><span class="icon"></span><span class="name">Job Digest Index</span>
    <span class="note">Sample: postings from examples/postings under profile.example.yaml; statuses illustrative</span></div>
  <div class="formula">fx</div>
  {_table(headers, main)}
  <div class="tabs"><span class="on">Jobs</span><span>Disqualified</span></div>
  <div class="label">Disqualified tab</div>
  {_table(sheet_layout.DISQUALIFIED_COLUMNS, disqualified)}
  <div style="height:16px"></div>
</div>
</body>
</html>
"""
    SHEET_HTML.write_text(page)
    return page


# --- screenshots -----------------------------------------------------------------------

def _chrome():
    path = os.environ.get("CHROME") or (CHROME_DEFAULT if os.path.exists(CHROME_DEFAULT) else shutil.which("google-chrome"))
    if not path:
        sys.exit("Google Chrome not found: set CHROME to its binary to take the screenshots")
    return path


def _run_chrome(args):
    with tempfile.TemporaryDirectory() as profile_dir:
        return subprocess.run([_chrome(), "--headless=new", "--disable-gpu", "--hide-scrollbars",
                               "--no-first-run", "--no-default-browser-check", f"--user-data-dir={profile_dir}",
                               *args], capture_output=True, text=True, timeout=120)


def page_height(page, width):
    """The page's height in CSS px at this width, measured by Chrome."""
    probe = page.replace("</body>", '<script>document.body.setAttribute("data-h", '
                                    'Math.ceil(document.documentElement.scrollHeight));</script></body>')
    with tempfile.NamedTemporaryFile("w", suffix=".html", delete=False) as fh:
        fh.write(probe)
    try:
        # A short window: scrollHeight is never less than the viewport.
        out = _run_chrome([f"--window-size={width},100", "--dump-dom", Path(fh.name).as_uri()]).stdout
    finally:
        os.unlink(fh.name)
    match = re.search(r'data-h="(\d+)"', out)
    if not match:
        sys.exit("Chrome did not report the page height")
    return int(match.group(1))


def screenshot(html_path, png_path, width, scale):
    height = page_height(html_path.read_text(), width)
    result = _run_chrome([f"--window-size={width},{height}", f"--force-device-scale-factor={scale}",
                          f"--screenshot={png_path}", html_path.as_uri()])
    if not png_path.exists():
        sys.exit(f"Chrome wrote no screenshot: {result.stderr[-400:]}")
    print(f"[Samples] wrote {png_path.relative_to(ROOT)}: {png_size(png_path)[0]} x {png_size(png_path)[1]} px "
          f"({width} CSS px at {scale}x)")


def png_size(path):
    head = Path(path).read_bytes()[:24]
    return int.from_bytes(head[16:20], "big"), int.from_bytes(head[20:24], "big")


def main(argv=None):
    parser = argparse.ArgumentParser(description="Render the sample digest and Sheet snapshot (PUB-16)")
    parser.add_argument("--refresh", action="store_true",
                        help="score examples/postings first (real calls, about nine; needs ANTHROPIC_API_KEY)")
    parser.add_argument("--no-png", action="store_true", help="write the HTML only")
    args = parser.parse_args(argv)
    data = refresh() if args.refresh else load()
    render_digest(data)
    render_sheet(data)
    print(f"[Samples] wrote {DIGEST_HTML.relative_to(ROOT)} and {SHEET_HTML.relative_to(ROOT)}")
    if not args.no_png:
        screenshot(DIGEST_HTML, DIGEST_PNG, DIGEST_WIDTH, DIGEST_SCALE)
        screenshot(SHEET_HTML, SHEET_PNG, SHEET_WIDTH, SHEET_SCALE)
    return 0


if __name__ == "__main__":
    sys.exit(main())
