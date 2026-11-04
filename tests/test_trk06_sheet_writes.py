"""Regression tests for TRK-06: main index rows were lost silently from 22 Sep 2026.

Sheets' values.append (gspread append_row) chose its own target row. On the
main tab it settled on row 1212 of a 1213-row block, so every "append"
overwrote that row and the run logged "Added N new jobs". FakeSheet below
reproduces that: its append_row writes to a fixed row, like the live tab did.
The fix writes to an explicit range below the last row and checks the range
the API reports. A failed write now fails the digest after the email is sent.
No network access.
"""

import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_ops04_sharded_fetch import _job, main, pending, pipeline, sheets, workdir  # noqa: E402,F401

HEADERS = ["Job Title", "Company", "Location", "Work Model", "Comp", "Score", "Status",
           "Claude Rationale", "Source", "Date First Seen", "Apply Link", "Notes",
           "Warm Contact", "Relo", "Applied Date", "Tier", "Posted Date"]


class FakeSheet:
    """A worksheet whose values.append lands on a fixed row, like the live tab."""

    def __init__(self, data_rows, append_lands_on=None, title="Job Digest Index Sheet"):
        self.title = title
        self.rows = [list(HEADERS)] + [list(r) for r in data_rows]
        self.row_count = len(self.rows)
        self.append_lands_on = append_lands_on  # 1-based row values.append overwrites
        self.report_row_offset = 0              # >0 makes update() report the wrong range

    def row_values(self, n):
        return list(self.rows[n - 1])

    def col_values(self, col):
        return [r[col - 1] if len(r) >= col else "" for r in self.rows]

    def get_all_values(self):
        return [list(r) for r in self.rows]

    def update_cell(self, row, col, value):
        self.rows[row - 1] += [""] * (col - len(self.rows[row - 1]))
        self.rows[row - 1][col - 1] = value

    def add_rows(self, n):
        self.row_count += n

    id = 0
    spreadsheet = type("Spreadsheet", (), {"batch_update": lambda self, body: None})()  # TRK-08 formats

    def append_row(self, row, value_input_option=None):
        self.rows[self.append_lands_on - 1] = list(row)

    def append_rows(self, rows, value_input_option=None):
        for r in rows:
            self.append_row(r)

    def update(self, values, range_name, value_input_option=None):
        start, _ = re.match(r"A(\d+):[A-Z]+(\d+)", range_name).groups()
        start = int(start)
        assert start + len(values) - 1 <= self.row_count, "wrote past the grid"
        for i, v in enumerate(values):
            idx = start - 1 + i
            if idx < len(self.rows):
                self.rows[idx] = list(v)
            else:
                self.rows.append(list(v))
        shown = start + self.report_row_offset
        return {"updatedRange": f"'{self.title}'!A{shown}:Q{shown + len(values) - 1}"}


def _existing(n):
    return [[f"Old {i}", f"Co {i}"] + [""] * 8 + [f"https://old.example/{i}"] + [""] * 6
            for i in range(n)]


def test_append_jobs_adds_rows_below_the_last_row_and_overwrites_nothing(monkeypatch):
    # 1212 data rows below the header; values.append would overwrite row 1212.
    sheet = FakeSheet(_existing(1212), append_lands_on=1212)
    before = sheet.get_all_values()
    monkeypatch.setattr(sheets, "_get_sheet", lambda: sheet)
    jobs = [_job(f"https://new.example/{i}", company=f"New {i}") for i in range(3)]

    assert sheets.append_jobs(jobs) == 3

    after = sheet.get_all_values()
    assert after[:1213] == before                       # nothing overwritten
    assert [r[HEADERS.index("Company")] for r in after[1213:]] == ["New 0", "New 1", "New 2"]
    assert [r[HEADERS.index("Status")] for r in after[1213:]] == ["New"] * 3


def test_old_append_path_reproduces_the_lost_rows():
    """Documents the bug: three append_row calls leave one new row, not three."""
    sheet = FakeSheet(_existing(1212), append_lands_on=1212)
    for i in range(3):
        sheet.append_row([f"New {i}"])
    assert len(sheet.get_all_values()) == 1213
    assert sheet.rows[1211] == ["New 2"]


def test_append_jobs_skips_urls_already_in_the_sheet(monkeypatch):
    sheet = FakeSheet(_existing(5))
    monkeypatch.setattr(sheets, "_get_sheet", lambda: sheet)
    jobs = [_job("https://old.example/2"), _job("https://new.example/x")]
    assert sheets.append_jobs(jobs) == 1
    assert len(sheet.get_all_values()) == 7


def test_failed_url_read_raises_and_writes_nothing(monkeypatch):
    sheet = FakeSheet(_existing(5))
    before = sheet.get_all_values()

    def quota(col):
        raise RuntimeError("APIError: [429]: Quota exceeded for 'Read requests'")
    sheet.col_values = quota
    monkeypatch.setattr(sheets, "_get_sheet", lambda: sheet)
    with pytest.raises(RuntimeError, match="429"):
        sheets.append_jobs([_job("https://old.example/2"), _job("https://new.example/x")])
    assert sheet.get_all_values() == before             # no duplicate re-append


def test_write_reported_at_another_row_raises(monkeypatch):
    sheet = FakeSheet(_existing(5))
    sheet.report_row_offset = -1
    monkeypatch.setattr(sheets, "_get_sheet", lambda: sheet)
    with pytest.raises(RuntimeError, match="API reports"):
        sheets.append_jobs([_job("https://new.example/x")])


def test_append_jobs_raises_instead_of_returning_zero(monkeypatch):
    def boom():
        raise RuntimeError("quota")
    monkeypatch.setattr(sheets, "_get_sheet", boom)
    with pytest.raises(RuntimeError, match="quota"):
        sheets.append_jobs([_job("https://new.example/x")])
    assert sheets.append_jobs([]) == 0                  # empty list never touches the Sheet


def test_append_disqualified_jobs_uses_the_same_explicit_write(monkeypatch):
    sheet = FakeSheet([], append_lands_on=1, title="Disqualified")
    sheet.rows = [list(sheets._DISQ_COLUMNS)]
    monkeypatch.setattr(sheets, "_get_spreadsheet", lambda: None)
    monkeypatch.setattr(sheets, "_get_or_create_sheet", lambda ss, title, headers: sheet)
    assert sheets.append_disqualified_jobs([_job("https://dq.example/1", score=0)]) == 1
    assert sheet.rows[0] == list(sheets._DISQ_COLUMNS)  # header intact
    assert sheet.rows[1][sheets._DISQ_COLUMNS.index("Apply Link")] == "https://dq.example/1"


def test_append_applied_row_returns_the_new_row_number(monkeypatch):
    sheet = FakeSheet(_existing(1212), append_lands_on=1212)
    monkeypatch.setattr(sheets, "_get_sheet", lambda: sheet)
    assert sheets.append_applied_row("Marriott International", "Senior Director") == 1214
    assert sheet.rows[1211][1] == "Co 1210"             # row 1212 untouched


def test_digest_still_emails_and_dequeues_then_fails_when_a_sheet_write_fails(pipeline, monkeypatch, capsys):
    calls, catalog = pipeline

    def failing_append(jobs):
        raise RuntimeError("APIError: [429]: Quota exceeded")
    monkeypatch.setattr(main, "append_jobs", failing_append)

    catalog["Startup"] = [_job("https://boards.greenhouse.io/acme/jobs/1")]
    main.fetch_run()
    with pytest.raises(main.SheetWriteError, match="append_jobs failed for 1 jobs"):
        main.digest_run()

    assert len(calls["emails"]) == 1                    # email still went
    assert pending.load_pending()[1] == []              # queue still drained
    out = capsys.readouterr().out
    assert "::error title=Sheet write failed::append_jobs failed for 1 jobs: RuntimeError: APIError: [429]" in out


def test_digest_with_nothing_to_write_does_not_fail(pipeline, monkeypatch):
    calls, catalog = pipeline

    def failing_append(jobs):
        if jobs:
            raise RuntimeError("should not be called with jobs")
        return 0
    monkeypatch.setattr(main, "append_disqualified_jobs", failing_append)
    catalog["Startup"] = [_job("https://boards.greenhouse.io/acme/jobs/1")]
    main.fetch_run()
    main.digest_run()                                   # no score-0 jobs: no failure
    assert len(calls["emails"]) == 1
