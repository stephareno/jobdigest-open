# sheet_init.py — `python main.py sheet-init`: lay out a new Sheet (PUB-10)
#
#   python main.py sheet-init
#
# Opens the Sheet in GOOGLE_SHEET_ID as the service account in
# GOOGLE_SERVICE_ACCOUNT_JSON (printing its email: share the Sheet with it as
# Editor), then for the main tab and the Disqualified tab (sheet_layout.TABS):
# creates the tab if it is missing, writes the header row the pipeline
# expects, bolds it and freezes it.
#
# Safe to run again: a tab whose header already carries every expected column
# is only re-frozen. A tab with any non-blank row below the header is never
# touched, and the run says which and exits 1. Needs no profile.yaml.

import json
import os

from sheet_layout import TABS

SCOPES = ["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"]


def _open_book(info, sheet_id):
    import gspread
    from google.oauth2.service_account import Credentials
    creds = Credentials.from_service_account_info(info, scopes=SCOPES)
    return gspread.authorize(creds).open_by_key(sheet_id)


def _worksheet(book, title):
    """The tab named `title`, or None."""
    for ws in book.worksheets():
        if ws.title == title:
            return ws
    return None


def _data_rows(values):
    """1-based row numbers below the header that hold anything."""
    return [n for n, row in enumerate(values[1:], 2) if any(str(c).strip() for c in row)]


def init_tab(book, title, headers):
    """(ok, message) for one tab."""
    ws = _worksheet(book, title)
    if ws is None:
        ws = book.add_worksheet(title=title, rows=1000, cols=len(headers) + 2)
        values, created = [], True
    else:
        values, created = ws.get_all_values(), False
        rows = _data_rows(values)
        if rows:
            return False, (f"{title!r}: NOT touched — {len(rows)} row(s) below the header "
                           f"(first at row {rows[0]}). sheet-init only lays out empty tabs; "
                           f"use a new Sheet, or clear the tab yourself if it holds nothing you need.")
    have = [h for h in (values[0] if values else []) if str(h).strip()]
    if have and all(h in have for h in headers):
        ws.freeze(rows=1)
        return True, f"{title!r}: already initialised ({len(have)} columns); header row frozen."
    ws.update(values=[headers], range_name="A1")
    if have:
        # A header row that was there but incomplete: clear any cells past the new one.
        extra = len(have) - len(headers)
        if extra > 0:
            ws.batch_clear([f"R1C{len(headers) + 1}:R1C{len(have)}"])
    try:
        ws.format("1:1", {"textFormat": {"bold": True}})
    except Exception as e:                             # noqa: BLE001 -- cosmetic
        print(f"[SheetInit] could not bold the header of {title!r}: {e}")
    ws.freeze(rows=1)
    what = "created with" if created else "header written:"
    return True, f"{title!r}: {what} {len(headers)} columns; header row frozen."


def main(argv=None, open_book=_open_book):
    if argv:
        print("usage: python main.py sheet-init  (takes no options)")
        return 2
    import envfile
    missing = [n for n in ("GOOGLE_SERVICE_ACCOUNT_JSON", "GOOGLE_SHEET_ID") if not os.environ.get(n, "").strip()]
    if missing:
        for name in missing:
            print(f"{name} missing: {envfile.secret(name).where}.")
        print("Set it in .env (see .env.example) or your shell, then run again.")
        return 1
    try:
        info = json.loads(os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"])
        email = info["client_email"]
    except (ValueError, KeyError, TypeError) as e:
        print(f"GOOGLE_SERVICE_ACCOUNT_JSON does not parse as a service account key ({type(e).__name__}): "
              f"paste the whole downloaded .json file's contents, on one line, in single quotes.")
        return 1
    print(f"Acting as {email}")
    try:
        book = open_book(info, os.environ["GOOGLE_SHEET_ID"].strip())
    except Exception as e:                             # noqa: BLE001
        print(f"The Sheet would not open ({type(e).__name__}): check GOOGLE_SHEET_ID and share the Sheet "
              f"with {email} as Editor.")
        return 1
    failed = 0
    for title, headers in TABS:
        ok, message = init_tab(book, title, headers)
        print(("  " if ok else "  REFUSED ") + message)
        failed += not ok
    others = [ws.title for ws in book.worksheets() if ws.title not in dict(TABS)]
    if others:
        print(f"Other tabs left as they are: {', '.join(map(repr, others))}.")
    print("SHEET-INIT " + ("done." if not failed else f"refused {failed} tab(s); nothing written to them."))
    return 1 if failed else 0
