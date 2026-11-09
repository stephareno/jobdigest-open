"""REV-03: Sheet write failures are retried, never lost.

Covers: a failed write keeps its rows in state/sheet_retry/ while the email
still goes and the queue still drains; the next digest replays the file
before it reads the Sheet and deletes it; a write that fails on the third row
is replayed without duplicating the two it wrote; a retry that fails again
keeps the file and puts a banner line in the email; a file that does not
parse is renamed .bad and reported; Disqualified-tab rows take the same path;
the banner shows the oldest five files and a count; doctor's count; the
Applied-seed and status passes wait for rows that are only in a retry file;
the dry run shows the lines and retries nothing. No network access.
"""

import json
import sys
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_ops04_sharded_fetch import _job, main, pending, pipeline, sheets, workdir  # noqa: E402,F401
from test_trk06_sheet_writes import HEADERS, FakeSheet  # noqa: E402

import doctor  # noqa: E402
import dry_run  # noqa: E402
import sheet_retry  # noqa: E402
from config import DISQUALIFIED_SHEET_NAME, SHEET_NAME  # noqa: E402

APPLY = HEADERS.index("Apply Link")


class FlakySheet(FakeSheet):
    """FakeSheet that can be down (every read fails) or fail mid-write: with
    fail_on_row=3 the first two rows of a write land, then the API errors."""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.down = None
        self.fail_on_row = None

    def col_values(self, col):
        if self.down:
            raise RuntimeError(self.down)
        return super().col_values(col)

    def update(self, values, range_name, value_input_option=None):
        if self.fail_on_row and len(values) >= self.fail_on_row:
            super().update(values[:self.fail_on_row - 1], range_name, value_input_option)
            raise RuntimeError(f"APIError: [503]: write stopped at row {self.fail_on_row}")
        return super().update(values, range_name, value_input_option)

    def urls(self):
        return [r[APPLY] if len(r) > APPLY else "" for r in self.rows[1:]]


@pytest.fixture
def sheet_digest(pipeline, monkeypatch):
    """The ops04 pipeline with the real Sheet writers over two FlakySheets."""
    calls, catalog = pipeline
    main_tab = FlakySheet([])
    disq_tab = FlakySheet([], title=DISQUALIFIED_SHEET_NAME)
    disq_tab.rows = [list(sheets._DISQ_COLUMNS)]
    disq_tab.urls = lambda: [r[sheets._DISQ_COLUMNS.index("Apply Link")] for r in disq_tab.rows[1:]]
    monkeypatch.setattr(sheets, "_get_sheet", lambda: main_tab)
    monkeypatch.setattr(sheets, "_get_spreadsheet", lambda: None)
    monkeypatch.setattr(sheets, "_get_or_create_sheet", lambda ss, title, headers: disq_tab)
    monkeypatch.setattr(main, "append_jobs", sheets.append_jobs)
    monkeypatch.setattr(main, "append_disqualified_jobs", sheets.append_disqualified_jobs)
    monkeypatch.setattr(main, "get_recent_index_rows", lambda days: [])
    monkeypatch.setattr(main, "send_digest",
                        lambda sections, **kw: calls["emails"].append((sections, kw)))
    calls["alerts"] = []
    monkeypatch.setattr(main, "send_alert_only",
                        lambda runs, **kw: calls["alerts"].append((runs, kw)))
    return calls, catalog, main_tab, disq_tab


def _files():
    return sorted(sheet_retry.retry_dir().glob("*"))


def _queue(catalog, urls, score=8):
    catalog["Startup"] = [_job(u, company=f"Co{u[-1]}", title=f"Director of IT {u[-1]}", score=score)
                          for u in urls]
    main.fetch_run()


URLS = [f"https://boards.greenhouse.io/co/jobs/{i}" for i in range(5)]


# ------------------------------------------------------- write failure path

def test_write_failure_keeps_the_rows_still_emails_and_drains(sheet_digest, monkeypatch):
    calls, catalog, main_tab, _ = sheet_digest
    monkeypatch.setenv("GITHUB_RUN_ID", "4242")
    main_tab.down = "APIError: [403]: The caller does not have permission"
    _queue(catalog, URLS[:2])
    with pytest.raises(main.SheetWriteError, match="append_jobs failed for 2 jobs"):
        main.digest_run()

    assert len(calls["emails"]) == 1                                   # email still went
    assert pending.load_pending()[1] == []                             # queue still drained
    [path] = _files()
    assert path.name.endswith("-4242.json")
    data = json.loads(path.read_text())
    assert data["version"] == 1 and data["run_id"] == "4242" and data["attempts"] == 1
    assert [i["tab"] for i in data["rows"]] == [SHEET_NAME] * 2
    assert [i["row"]["Apply Link"] for i in data["rows"]] == URLS[:2]
    assert set(data["rows"][0]["row"]) == set(sheets.main_row_data({}))  # the full row dict
    assert "403" in data["last_error"]
    notices = calls["emails"][0][1]["notices"]
    assert notices == [f"2 postings from {data['created_at'][:10]} are waiting to be written to the "
                       f"Sheet; last error: {data['last_error']}"]


def test_next_digest_retries_the_file_and_deletes_it(sheet_digest):
    calls, catalog, main_tab, _ = sheet_digest
    main_tab.down = "APIError: [500]: backend error"
    _queue(catalog, URLS[:2])
    with pytest.raises(main.SheetWriteError):
        main.digest_run()
    saved = [i["row"] for i in json.loads(_files()[0].read_text())["rows"]]

    main_tab.down = None
    main.digest_run()                                                  # nothing new queued: no raise
    assert _files() == []
    assert main_tab.urls() == URLS[:2]
    row = dict(zip(main_tab.rows[0], main_tab.rows[1]))
    assert row["Date First Seen"] == sheets.date_cell(saved[0]["Date First Seen"])   # re-created exactly
    assert row["Job Title"] == saved[0]["Job Title"] and row["Status"] == "New"


def test_replay_runs_before_the_sheet_is_read_for_the_email(sheet_digest, monkeypatch):
    calls, catalog, main_tab, _ = sheet_digest
    main_tab.down = "APIError: [500]"
    _queue(catalog, URLS[:1])
    with pytest.raises(main.SheetWriteError):
        main.digest_run()
    main_tab.down = None
    order = []
    real_replay = sheet_retry.replay
    monkeypatch.setattr(sheet_retry, "replay", lambda write: order.append("replay") or real_replay(write))
    monkeypatch.setattr(main, "get_recent_index_rows", lambda days: order.append("read") or [])
    _queue(catalog, URLS[1:2])
    main.digest_run()
    assert order == ["replay", "read"]


def test_a_write_that_fails_on_the_third_row_is_replayed_without_duplicates(sheet_digest):
    calls, catalog, main_tab, _ = sheet_digest
    main_tab.fail_on_row = 3
    _queue(catalog, URLS)
    with pytest.raises(main.SheetWriteError, match="row 3"):
        main.digest_run()
    assert main_tab.urls() == URLS[:2]                                 # two landed before the error
    assert len(json.loads(_files()[0].read_text())["rows"]) == 5       # the file holds all five

    main_tab.fail_on_row = None
    main.digest_run()
    assert main_tab.urls() == URLS                                     # each once
    assert _files() == []


def test_a_retry_that_fails_again_keeps_the_file_and_adds_a_banner_line(sheet_digest):
    calls, catalog, main_tab, _ = sheet_digest
    main_tab.down = "APIError: [500]: first"
    _queue(catalog, URLS[:1])
    with pytest.raises(main.SheetWriteError):
        main.digest_run()
    [path] = _files()

    main_tab.down = "APIError: [403]: share removed"
    _queue(catalog, URLS[1:2])                                         # a new posting, so an email goes
    with pytest.raises(main.SheetWriteError, match="rows still not written"):
        main.digest_run()
    # Same day, same run id ("local"): the new failure merges into the old file.
    assert _files() == [path]
    old = json.loads(path.read_text())
    assert [i["row"]["Apply Link"] for i in old["rows"]] == URLS[:2]
    assert old["attempts"] == 2
    notices = calls["emails"][-1][1]["notices"]
    assert notices == [f"2 postings from {old['created_at'][:10]} are waiting to be written "
                       f"to the Sheet; last error: {old['last_error']}"]
    assert "share removed" in old["last_error"]


def test_a_retry_that_fails_again_in_another_run_keeps_both_files(sheet_digest, monkeypatch):
    calls, catalog, main_tab, _ = sheet_digest
    monkeypatch.setenv("GITHUB_RUN_ID", "100")
    main_tab.down = "APIError: [500]: first"
    _queue(catalog, URLS[:1])
    with pytest.raises(main.SheetWriteError):
        main.digest_run()
    monkeypatch.setenv("GITHUB_RUN_ID", "101")
    main_tab.down = "APIError: [403]: share removed"
    _queue(catalog, URLS[1:2])
    with pytest.raises(main.SheetWriteError, match="rows still not written"):
        main.digest_run()
    first, second = _files()
    assert first.name.endswith("-100.json") and second.name.endswith("-101.json")
    old = json.loads(first.read_text())
    assert old["attempts"] == 2
    assert old["last_error"] == f"{SHEET_NAME}: RuntimeError: APIError: [403]: share removed"
    notices = calls["emails"][-1][1]["notices"]
    assert len(notices) == 2
    assert notices[0] == (f"1 posting from {old['created_at'][:10]} is waiting to be written to the "
                          f"Sheet; last error: {old['last_error']}")


def test_banner_line_reaches_the_email_html(sheet_digest):
    import emailer
    html = emailer._build_html([], [], [], [], False, False,
                               notices=["3 postings from 2026-11-07 are waiting to be written to the Sheet; "
                                        "last error: <b>quota</b>"])
    assert "3 postings from 2026-11-07 are waiting to be written to the Sheet; last error: &lt;b&gt;quota" in html
    assert "background:#FCEBEB" in html                                # the red banner


def test_disqualified_rows_take_the_same_path(sheet_digest):
    calls, catalog, main_tab, disq_tab = sheet_digest
    disq_tab.down = "APIError: [429]: quota"
    _queue(catalog, URLS[:2], score=0)
    with pytest.raises(main.SheetWriteError, match="append_disqualified_jobs failed"):
        main.digest_run()
    data = json.loads(_files()[0].read_text())
    assert {i["tab"] for i in data["rows"]} == {DISQUALIFIED_SHEET_NAME}
    assert set(data["rows"][0]["row"]) == set(sheets.disqualified_row_data({}))

    # One of the two did reach the tab some other way: the replay skips it.
    disq_tab.down = None
    sheets.write_rows(DISQUALIFIED_SHEET_NAME, [data["rows"][0]["row"]])
    main.digest_run()
    assert disq_tab.urls() == URLS[:2]
    assert main_tab.urls() == [] and _files() == []


def test_write_rows_skips_urls_on_either_tab_and_repeats(sheet_digest):
    _, _, main_tab, disq_tab = sheet_digest
    row = sheets.main_row_data(_job(URLS[0]))
    assert sheets.write_rows(SHEET_NAME, [row, dict(row)]) == 1
    assert sheets.write_rows(SHEET_NAME, [row]) == 0
    drow = sheets.disqualified_row_data(_job(URLS[1], score=0))
    assert sheets.write_rows(DISQUALIFIED_SHEET_NAME, [drow]) == 1
    assert sheets.write_rows(DISQUALIFIED_SHEET_NAME, [drow]) == 0
    with pytest.raises(ValueError, match="unknown tab"):
        sheets.write_rows("Sheet9", [row])


# ----------------------------------------------------------- the files

def _write(name, rows, created, error="boom"):
    folder = sheet_retry.retry_dir()
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / name
    path.write_text(json.dumps({"version": 1, "created_at": created, "run_id": "1", "attempts": 1,
                                "last_error": error, "rows": rows}))
    return path


def _rows(*urls, tab=SHEET_NAME):
    return [{"tab": tab, "row": sheets.main_row_data(_job(u))} for u in urls]


def test_a_corrupt_file_is_renamed_bad_and_reported(sheet_digest, capsys):
    calls, catalog, main_tab, _ = sheet_digest
    folder = sheet_retry.retry_dir()
    folder.mkdir(parents=True)
    (folder / "2026-11-01-7.json").write_text("{not json")
    (folder / "2026-11-02-8.json").write_text(json.dumps({"rows": [{"tab": "Nope", "row": {}}]}))
    good = _write("2026-11-03-9.json", _rows(URLS[0]), "2026-11-03T10:00:00+00:00")
    _queue(catalog, URLS[1:2])
    main.digest_run()

    names = sorted(p.name for p in _files())
    assert names == ["2026-11-01-7.bad", "2026-11-02-8.bad"]           # never dropped; good one done
    assert not good.exists() and main_tab.urls() == [URLS[0], URLS[1]]
    out = capsys.readouterr().out
    assert "::error title=Sheet retry file unreadable::2026-11-01-7.json" in out
    notices = calls["emails"][-1][1]["notices"]
    assert [n.split(" could not")[0] for n in notices] == ["Sheet retry file 2026-11-01-7.bad",
                                                           "Sheet retry file 2026-11-02-8.bad"]
    [check] = doctor.sheet_retry_files()
    assert check["status"] == doctor.FAIL and "2026-11-01-7.bad" in check["reason"]


def test_files_replay_oldest_first():
    _write("2026-11-05-2.json", _rows(URLS[1]), "2026-11-05T10:00:00+00:00")
    _write("2026-11-04-1.json", _rows(URLS[0]), "2026-11-04T10:00:00+00:00")
    seen = []
    assert sheet_retry.replay(lambda tab, rows: seen.append(rows[0]["Apply Link"]) or len(rows)) == []
    assert seen == URLS[:2] and _files() == []


def test_banner_caps_at_the_oldest_five_files():
    start = datetime(2026, 11, 1, tzinfo=timezone.utc)
    for i in range(7):
        day = start + timedelta(days=i)
        _write(f"{day.date()}-{i}.json", _rows(URLS[0], URLS[1]) if i == 0 else _rows(URLS[0]),
               day.isoformat(), error=f"err {i}")
    lines = sheet_retry.banner_lines()
    assert len(lines) == 6
    assert lines[0] == ("2 postings from 2026-11-01 are waiting to be written to the Sheet; "
                        "last error: err 0")
    assert lines[4].startswith("1 posting from 2026-11-05 is waiting")
    assert lines[5] == "2 more Sheet retry files waiting in state/sheet_retry/."


def test_doctor_reports_the_waiting_files_and_the_oldest_date():
    [check] = doctor.sheet_retry_files()
    assert check["status"] == doctor.PASS
    _write("2026-11-06-2.json", _rows(URLS[1], URLS[2]), "2026-11-06T10:00:00+00:00")
    _write("2026-11-04-1.json", _rows(URLS[0]), "2026-11-04T10:00:00+00:00")
    [check] = doctor.sheet_retry_files()
    assert check["status"] == doctor.WARN
    assert check["reason"].startswith("2 file(s) waiting (3 postings), oldest 2026-11-04")


def test_new_failure_written_with_no_rows_writes_no_file():
    assert sheet_retry.write_retry_file([(SHEET_NAME, [], "x")]) is None
    assert _files() == []


# ------------------------------------------------------ status updates wait

def test_applied_seed_skips_an_entry_whose_row_is_only_in_a_retry_file(monkeypatch, capsys):
    from sources import applied_doc
    row = sheets.main_row_data(_job(URLS[0], company="Acme", title="Director of IT"))
    _write("2026-11-04-1.json", [{"tab": SHEET_NAME, "row": row}], "2026-11-04T10:00:00+00:00")
    appended = []
    monkeypatch.setattr(sheets, "ensure_columns", lambda names: None)
    monkeypatch.setattr(sheets, "get_index_rows", lambda: [])
    monkeypatch.setattr(sheets, "append_applied_row", lambda *a: appended.append(a) or 2)
    monkeypatch.setattr(applied_doc, "read_entries", lambda: [
        {"heading": "12 Acme - Director of IT", "body": "Applied 11/3/2026"},
        {"heading": "13 Beta - VP IT", "body": "Applied 11/3/2026"}])
    applied_doc.seed_from_doc()
    assert [a[0] for a in appended] == ["Beta"]                        # Acme waits for its row
    assert "SKIPPED Acme — Director of IT: its row is waiting in state/sheet_retry/" in capsys.readouterr().out


def test_inbox_readers_log_the_waiting_postings(monkeypatch, capsys):
    _write("2026-11-04-1.json", _rows(URLS[0], URLS[1]), "2026-11-04T10:00:00+00:00")
    monkeypatch.setitem(sys.modules, "sources.applied_doc", types.SimpleNamespace(seed_from_doc=lambda: None))
    monkeypatch.setitem(sys.modules, "sources.status_email",
                        types.SimpleNamespace(check_status_emails=lambda: None))
    main.run_inbox_readers({"applied_doc": {"enabled": False}, "status_tracking": {"enabled": True}})
    assert ("[SheetRetry] 2 postings waiting in state/sheet_retry/ are not on the Sheet yet: "
            "status updates for them are skipped until they are written") in capsys.readouterr().out


# ----------------------------------------------------------------- dry run

def test_dry_run_shows_the_lines_and_retries_nothing(workdir, monkeypatch):
    path = _write("2026-11-04-1.json", _rows(URLS[0]), "2026-11-04T10:00:00+00:00", error="quota")
    (sheet_retry.retry_dir() / "2026-11-05-2.json").write_text("{bad")
    before = path.read_text()
    out = workdir / "out.html"
    monkeypatch.setattr(dry_run, "OUT", str(out))
    assert dry_run.main([]) == 0
    html = out.read_text()
    assert "1 posting from 2026-11-04 is waiting to be written to the Sheet; last error: quota" in html
    assert "2026-11-05-2.json could not be read; the next digest renames it .bad." in html
    assert path.read_text() == before
    assert (sheet_retry.retry_dir() / "2026-11-05-2.json").exists()    # not renamed by a dry run


# ------------------------------------------------------- test isolation

def test_the_suite_never_reaches_a_live_sheet():
    """conftest refuses the live Sheet and replaces exported credentials, so a
    test that forgets a stub fails instead of writing the real tracker."""
    import os
    with pytest.raises(RuntimeError, match="never reach the live Google Sheet or mail"):
        sheets._get_sheet()
    with pytest.raises(RuntimeError, match="never reach the live Google Sheet or mail"):
        sheets._get_spreadsheet()
    assert "private_key" not in os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "")   # never a real key
    assert sheet_retry.retry_dir() != sheet_retry.REPO_ROOT / sheet_retry.RETRY_DIR
