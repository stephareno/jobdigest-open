"""REV-04: Sheet header safety. Never insert a row or a column into a live Sheet.

Covers: reordered columns write to the right cells; a renamed required column
fails loudly, naming it and the tab, and writes nothing; an empty new tab gets
its header row; a row 1 with data but no headers fails without inserting; an
absent optional column is skipped and reported once; a repeated header is an
error; headers match after trimming; the Disqualified tab has its own required
set; status writes skip a missing Status Date; a header failure goes through
the REV-03 retry file and the email's banner, and on an empty day sends the
REV-02 alert once; doctor's header report. All against stubbed Sheets: the
conftest guard refuses every live entry point.
"""

import inspect
import json
import re
import smtplib
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_ops04_sharded_fetch import _job, main, pending, pipeline, sheets, workdir  # noqa: E402,F401
from test_rev03_sheet_retry import FlakySheet, URLS, _files, _queue, sheet_digest  # noqa: E402,F401
from test_trk06_sheet_writes import HEADERS  # noqa: E402

import doctor  # noqa: E402
import emailer  # noqa: E402
import sheet_layout  # noqa: E402
import sheet_retry  # noqa: E402
from config import DISQUALIFIED_SHEET_NAME, SHEET_NAME  # noqa: E402


def _sheet(headers, rows=()):
    sheet = FlakySheet([])
    sheet.rows = [list(headers)] + [list(r) for r in rows]
    sheet.row_count = len(sheet.rows)
    return sheet


def _use(monkeypatch, sheet):
    monkeypatch.setattr(sheets, "_get_sheet", lambda: sheet)
    return sheet


def _cell(sheet, row, header):
    return sheet.rows[row - 1][[h.strip() for h in sheet.rows[0]].index(header)]


# ------------------------------------------------------------ the constants

def test_required_header_constants():
    assert sheet_layout.REQUIRED_HEADERS == {
        SHEET_NAME: ("Job Title", "Company", "Apply Link", "Status", "Date First Seen", "Score"),
        DISQUALIFIED_SHEET_NAME: ("Job Title", "Company", "Apply Link", "Date First Seen", "Score",
                                  "Disqualify Reason"),
    }
    assert sheets.REQUIRED_HEADERS is sheet_layout.REQUIRED_HEADERS
    for tab, layout in sheet_layout.TABS:            # sheet-init's layout always passes
        assert sheet_layout.header_problems(tab, layout) == ([], [])


def test_nothing_in_sheets_inserts_a_row_or_creates_a_column():
    src = inspect.getsource(sheets)
    assert "insert_row" not in src and "insert_cols" not in src and "_ensure_header" not in src
    assert not re.search(r"update_cell\(\s*1\s*,", src)        # no write into row 1 by cell


# ------------------------------------------------------------ write path

def test_reordered_columns_write_to_the_right_cells(monkeypatch):
    shuffled = list(reversed(HEADERS))
    sheet = _use(monkeypatch, _sheet(shuffled))
    job = _job("https://new.example/1", title="VP IT", company="Beta", score=9)
    assert sheets.append_jobs([job]) == 1
    assert sheet.rows[0] == shuffled                              # header untouched
    assert _cell(sheet, 2, "Job Title") == "VP IT"
    assert _cell(sheet, 2, "Company") == "Beta"
    assert _cell(sheet, 2, "Score") == 9
    assert _cell(sheet, 2, "Apply Link") == "https://new.example/1"
    assert _cell(sheet, 2, "Status") == "New"


def test_headers_match_after_trimming(monkeypatch):
    padded = [f"  {h} " if h in ("Apply Link", "Company") else h for h in HEADERS]
    sheet = _use(monkeypatch, _sheet(padded))
    assert sheets.append_jobs([_job("https://new.example/1", company="Beta")]) == 1
    assert _cell(sheet, 2, "Company") == "Beta"


@pytest.mark.parametrize("renamed, new_name", [("Apply Link", "Apply URL"), ("Job Title", "Role"),
                                               ("Status", "Stage")])
def test_a_renamed_required_column_fails_naming_it_and_writes_nothing(monkeypatch, renamed, new_name):
    headers = [new_name if h == renamed else h for h in HEADERS]
    sheet = _use(monkeypatch, _sheet(headers, [["Old"] * len(headers)]))
    before = sheet.get_all_values()
    with pytest.raises(sheets.SheetHeaderError) as err:
        sheets.append_jobs([_job("https://new.example/1")])
    assert f"'{SHEET_NAME}' tab: required header missing from row 1: {renamed}." in str(err.value)
    assert "Nothing was written" in str(err.value)
    assert sheet.get_all_values() == before                      # no row, no column, no insert


def test_a_repeated_header_is_an_error_naming_it(monkeypatch):
    headers = HEADERS + ["Company"]
    sheet = _use(monkeypatch, _sheet(headers))
    with pytest.raises(sheets.SheetHeaderError, match=r"header repeated in row 1: Company\."):
        sheets.append_jobs([_job("https://new.example/1")])
    assert len(sheet.rows) == 1


def test_an_absent_optional_column_is_skipped_and_reported_once(monkeypatch, capsys):
    monkeypatch.setattr(sheets, "_REPORTED_OPTIONAL", set())
    headers = [h for h in HEADERS if h not in ("Tier", "Relo")]
    sheet = _use(monkeypatch, _sheet(headers))
    sheets.append_jobs([{**_job("https://new.example/1"), "relo": True, "tier": "dream"}])
    sheets.append_jobs([_job("https://new.example/2")])
    assert sheet.rows[0] == headers                               # never created
    assert all(len(r) == len(headers) for r in sheet.rows)
    out = capsys.readouterr().out
    for name in ("Tier", "Relo"):
        line = f"[Sheets] '{SHEET_NAME}': optional column missing: {name} (not created"
        assert out.count(line) == 1


def test_disqualified_tab_has_its_own_required_set(monkeypatch):
    headers = [h for h in sheets._DISQ_COLUMNS if h != "Disqualify Reason"]
    sheet = FlakySheet([], title=DISQUALIFIED_SHEET_NAME)
    sheet.rows = [headers]
    monkeypatch.setattr(sheets, "_get_spreadsheet", lambda: None)
    monkeypatch.setattr(sheets, "_get_or_create_sheet", lambda ss, title, h: sheet)
    with pytest.raises(sheets.SheetHeaderError,
                       match=f"'{DISQUALIFIED_SHEET_NAME}' tab: required header missing from row 1: "
                             f"Disqualify Reason"):
        sheets.append_disqualified_jobs([_job("https://dq.example/1", score=0)])
    assert sheet.rows == [headers]


def test_status_updates_skip_a_missing_status_date_without_creating_it(monkeypatch, capsys):
    monkeypatch.setattr(sheets, "_REPORTED_OPTIONAL", set())
    headers = list(HEADERS)                                        # no Status Date
    sheet = _use(monkeypatch, _sheet(headers, [["Old", "Co"] + [""] * (len(headers) - 2)]))
    assert sheets.update_statuses([(2, "Rejected")]) == 1
    assert _cell(sheet, 2, "Status") == "Rejected" and sheet.rows[0] == headers
    assert "optional column missing: Status Date" in capsys.readouterr().out
    assert sheets.ensure_columns(["Status Date"]) == headers       # reported, never created
    assert sheet.rows[0] == headers


# ------------------------------------------------------- _get_or_create_sheet

class HeaderTab:
    """A tab for _get_or_create_sheet: records every call that could write."""

    def __init__(self, row1, title=SHEET_NAME):
        self.title = title
        self.row1 = list(row1)
        self.writes = []

    def row_values(self, n):
        return list(self.row1)

    def update(self, values, range_name, value_input_option=None):
        self.writes.append((range_name, values))
        if range_name == "A1":
            self.row1 = list(values[0])

    def format(self, *a, **k):
        pass

    def insert_row(self, *a, **k):                                 # pragma: no cover -- must never run
        raise AssertionError("insert_row called")

    def insert_cols(self, *a, **k):                                # pragma: no cover
        raise AssertionError("insert_cols called")


class Book:
    def __init__(self, tabs):
        self.tabs = dict(tabs)
        self.added = []

    def worksheet(self, title):
        if title not in self.tabs:
            raise sheets.gspread.WorksheetNotFound(title)
        return self.tabs[title]

    def add_worksheet(self, title, rows, cols):
        tab = HeaderTab([], title)
        self.tabs[title] = tab
        self.added.append(title)
        return tab


def test_an_empty_new_tab_gets_the_header_row():
    book = Book({})
    tab = sheets._get_or_create_sheet(book, SHEET_NAME, sheet_layout.SHEET_COLUMNS)
    assert book.added == [SHEET_NAME]
    assert tab.writes == [("A1", [sheet_layout.SHEET_COLUMNS])]


def test_an_existing_tab_with_an_empty_row_1_gets_the_header_row():
    tab = HeaderTab(["", "  ", ""])
    sheets._get_or_create_sheet(Book({SHEET_NAME: tab}), SHEET_NAME, sheet_layout.SHEET_COLUMNS)
    assert tab.writes == [("A1", [sheet_layout.SHEET_COLUMNS])]


def test_a_row_1_with_data_but_no_headers_fails_and_inserts_nothing():
    tab = HeaderTab(["Director of IT", "Acme", "Remote", "", "", "8"])
    with pytest.raises(sheets.SheetHeaderError, match="required headers missing from row 1: "
                                                      "Job Title, Company, Apply Link"):
        sheets._get_or_create_sheet(Book({SHEET_NAME: tab}), SHEET_NAME, sheet_layout.SHEET_COLUMNS)
    assert tab.writes == []


def test_column_a_moved_is_fine_when_every_required_header_is_there():
    moved = ["Company"] + [h for h in sheet_layout.SHEET_COLUMNS if h != "Company"]
    tab = HeaderTab(moved)
    assert sheets._get_or_create_sheet(Book({SHEET_NAME: tab}), SHEET_NAME, sheet_layout.SHEET_COLUMNS) is tab
    assert tab.writes == []                                         # the old check inserted a header here


def test_check_tabs_reports_header_problems_read_only(monkeypatch):
    main_tab = HeaderTab([h for h in sheet_layout.SHEET_COLUMNS if h != "Apply Link"])
    disq = HeaderTab(sheet_layout.DISQUALIFIED_COLUMNS, DISQUALIFIED_SHEET_NAME)
    monkeypatch.setattr(sheets, "_get_spreadsheet", lambda: Book({SHEET_NAME: main_tab,
                                                                 DISQUALIFIED_SHEET_NAME: disq}))
    [problem] = sheets.check_tabs()
    assert problem.startswith(f"'{SHEET_NAME}' tab: required header missing from row 1: Apply Link.")
    assert main_tab.writes == [] and disq.writes == []


def test_check_tabs_treats_an_unopenable_sheet_as_no_header_problem(capsys):
    assert sheets.check_tabs() == []                                # conftest refuses the live Sheet
    assert "header check skipped" in capsys.readouterr().out


# --------------------------------------------- digest: REV-03 and REV-02 paths

def _book_for(monkeypatch, main_tab, disq_tab):
    monkeypatch.setattr(sheets, "_get_spreadsheet", lambda: Book({SHEET_NAME: main_tab,
                                                                 DISQUALIFIED_SHEET_NAME: disq_tab}))


def test_a_header_failure_keeps_the_rows_emails_with_the_banner_then_fails(sheet_digest, monkeypatch):
    calls, catalog, main_tab, disq_tab = sheet_digest
    main_tab.rows[0] = ["Apply URL" if h == "Apply Link" else h for h in main_tab.rows[0]]
    _book_for(monkeypatch, main_tab, disq_tab)
    _queue(catalog, URLS[:2])
    with pytest.raises(main.SheetWriteError, match="required header missing from row 1: Apply Link"):
        main.digest_run()
    assert len(calls["emails"]) == 1 and pending.load_pending()[1] == []
    assert len(main_tab.rows) == 1                                  # nothing written, nothing inserted
    [path] = _files()
    data = json.loads(path.read_text())
    assert [i["row"]["Apply Link"] for i in data["rows"]] == URLS[:2]
    assert "SheetHeaderError" in data["last_error"]
    notices = calls["emails"][0][1]["notices"]
    assert notices[0].startswith(f"'{SHEET_NAME}' tab: required header missing from row 1: Apply Link.")
    assert notices[1].startswith("2 postings from ")

    # Header fixed: the next digest writes the rows and clears the file.
    main_tab.rows[0] = list(HEADERS)
    main.digest_run()
    assert main_tab.urls() == URLS[:2] and _files() == []


def test_an_empty_day_with_a_header_failure_sends_the_alert_once(sheet_digest, monkeypatch):
    calls, catalog, main_tab, disq_tab = sheet_digest
    main_tab.rows[0] = [h for h in main_tab.rows[0] if h != "Status"]
    _book_for(monkeypatch, main_tab, disq_tab)
    with pytest.raises(main.SheetWriteError, match="Status"):
        main.digest_run()
    assert calls["emails"] == []
    [(runs, kw)] = calls["alerts"]
    assert runs == [] and kw["notices"][0].startswith(
        f"'{SHEET_NAME}' tab: required header missing from row 1: Status.")
    subject, html = emailer.build_alert_only(runs, notices=kw["notices"])
    assert subject == "Job digest: Sheet write failed"
    assert emailer.SHEET_ALERT_NOTE in html and "Status." in html
    assert "fetch runs above" not in html

    with pytest.raises(main.SheetWriteError):                       # still broken next day
        main.digest_run()
    assert len(calls["alerts"]) == 1                                # reported once

    main_tab.rows[0] = list(HEADERS)                                # fixed, then broken again
    main.digest_run()
    main_tab.rows[0] = [h for h in main_tab.rows[0] if h != "Status"]
    with pytest.raises(main.SheetWriteError):
        main.digest_run()
    assert len(calls["alerts"]) == 2                                # a new occurrence alerts again


def test_an_empty_day_with_a_retry_file_waiting_sends_the_alert_once(sheet_digest, monkeypatch):
    calls, catalog, main_tab, disq_tab = sheet_digest
    main_tab.down = "APIError: [503]"
    _queue(catalog, URLS[:1])
    with pytest.raises(main.SheetWriteError):
        main.digest_run()                                           # postings day: normal email
    assert len(calls["emails"]) == 1 and calls["alerts"] == []
    with pytest.raises(main.SheetWriteError):
        main.digest_run()                                           # empty day, same file: reported already
    assert calls["alerts"] == []

    # A second file appears on a later day: the empty day alerts for it.
    sheet_retry.write_retry_file([(SHEET_NAME, [sheets.main_row_data(_job(URLS[3]))], "x")],
                                 now=__import__("datetime").datetime(2026, 12, 1,
                                                                     tzinfo=__import__("datetime").timezone.utc))
    with pytest.raises(main.SheetWriteError):
        main.digest_run()
    [(runs, kw)] = calls["alerts"]
    assert runs == [] and len(kw["notices"]) == 2


def test_alert_subject_when_fetch_runs_and_the_sheet_both_failed():
    run = {"run_id": 1, "started_at": "2026-11-08T04:17:00Z", "conclusion": "failure", "url": "",
           "error_title": "Anthropic API access failed", "error_message": ""}
    subject, html = emailer.build_alert_only([run], notices=["1 posting from 2026-11-08 is waiting"])
    assert subject == "Job digest: fetch runs and Sheet write failed"
    assert emailer.ALERT_ONLY_NOTE in html


# ------------------------------------------------------------------ doctor

@pytest.mark.parametrize("row1, status, text", [
    (sheet_layout.SHEET_COLUMNS + sheet_layout.OPTIONAL_COLUMNS, "PASS", "all 6 required present; no optional"),
    ([h for h in sheet_layout.SHEET_COLUMNS if h != "Score"], "FAIL", "required header missing: Score"),
    (sheet_layout.SHEET_COLUMNS + ["Score"], "FAIL", "header repeated: Score"),
    (sheet_layout.SHEET_COLUMNS, "WARN", "optional column missing: Function Fit; optional column missing: "
                                         "Criteria; optional column missing: Dream"),
    ([], "WARN", "row 1 is empty"),
])
def test_doctor_header_check(row1, status, text):
    got = doctor.header_check(SHEET_NAME, row1)
    assert got["status"] == status and text in got["reason"], got
    assert got["check"] == f"GOOGLE headers {SHEET_NAME!r}"


def test_doctor_header_check_disqualified():
    got = doctor.header_check(DISQUALIFIED_SHEET_NAME, sheet_layout.DISQUALIFIED_COLUMNS)
    assert got["status"] == "PASS" and "all 6 required present" in got["reason"]
    got = doctor.header_check(DISQUALIFIED_SHEET_NAME, [h for h in sheet_layout.DISQUALIFIED_COLUMNS
                                                        if h != "Disqualify Reason"])
    assert got["status"] == "FAIL" and "Disqualify Reason" in got["reason"]


def test_doctor_sheet_check_returns_both_header_rows(monkeypatch):
    """check_google adds one header line per tab (phase 2 and no-phase)."""
    src = inspect.getsource(doctor.check_google)
    assert "header_check(title, row1)" in src
    assert "out.extend(got if isinstance(got, list) else [got])" in inspect.getsource(doctor.phase2)


# ----------------------------------------------------------- the guard

def test_the_guard_refuses_smtp_and_gspread():
    import gspread
    with pytest.raises(RuntimeError, match="never reach the live"):
        smtplib.SMTP_SSL("smtp.gmail.com", 465)
    with pytest.raises(RuntimeError, match="never reach the live"):
        gspread.authorize(None)
