# sheet_retry.py — Sheet rows a digest could not write, kept until they are (REV-03)
#
# When a digest's Sheet write fails, the rows it was writing go to
# state/sheet_retry/<ISO date>-<run id>.json, and daily.yml commits the file.
# The queue batches are still deleted: the file now holds the rows. Every
# digest replays every file, oldest first, before it builds the email, and
# deletes a file only once every row in it is on its tab (written now, or
# found there already: sheets.write_rows skips an Apply Link the tab has, so a
# replay after a partial write adds only the missing rows).
#
# The digest owns this folder (AGENTS.md rule 9): fetch never reads or writes
# it. A file that does not parse is renamed <name>.bad and reported in every
# digest banner and by doctor until someone deals with it; it is never dropped.
#
# File format (version 1):
#   {"version": 1, "created_at": "2026-11-09T11:42:05+00:00", "run_id": "42550000000",
#    "attempts": 1, "last_error": "append_jobs failed for 2 jobs: APIError: ...",
#    "rows": [{"tab": "Job Digest Index Sheet", "row": {"Job Title": ..., "Apply Link": ...}}, ...]}
# Each "row" is the {header: value} dict sheets.write_rows writes, so a replay
# re-creates the row exactly (Date First Seen included).

import json
import os
from datetime import datetime, timezone
from pathlib import Path

from config import SHEET_NAME, DISQUALIFIED_SHEET_NAME
from state import write_json_atomic

REPO_ROOT = Path(__file__).resolve().parent
RETRY_DIR = os.path.join("state", "sheet_retry")
VERSION = 1
BANNER_FILES = 5           # banner lines for the oldest five files, then a count
TABS = (SHEET_NAME, DISQUALIFIED_SHEET_NAME)


def retry_dir():
    return REPO_ROOT / RETRY_DIR


def _run_id():
    return os.environ.get("GITHUB_RUN_ID", "").strip() or "local"


def write_retry_file(failures, now=None):
    """failures: [(tab, rows, error)]. Writes this run's file and returns its
    path, or None when there are no rows. A file of the same name (a re-run of
    the same Actions run, or two local runs in a day) that is still waiting is
    merged into, never overwritten: its rows stay, this run's are added."""
    rows = [{"tab": tab, "row": row} for tab, tab_rows, _ in failures for row in tab_rows]
    if not rows:
        return None
    now = now or datetime.now(timezone.utc)
    folder = retry_dir()
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{now.date().isoformat()}-{_run_id()}.json"
    data = {"version": VERSION, "created_at": now.isoformat(timespec="seconds"),
            "run_id": _run_id(), "attempts": 1,
            "last_error": "; ".join(err for _, _, err in failures), "rows": rows}
    if path.exists():
        try:
            old = _load(path)
        except ValueError as e:
            _mark_bad(path, e)                   # kept as .bad, reported; this run's file is new
        else:
            have = {(i["tab"], str(i["row"].get("Apply Link", ""))) for i in old["rows"]}
            data["rows"] = old["rows"] + [i for i in rows
                                          if (i["tab"], str(i["row"].get("Apply Link", ""))) not in have]
            data["created_at"] = old.get("created_at") or data["created_at"]
            data["attempts"] = old.get("attempts") or 1
    write_json_atomic(str(path), data, indent=2)
    print(f"[SheetRetry] {len(rows)} rows the Sheet write did not take saved to "
          f"{os.path.relpath(path, REPO_ROOT)}; the next digest retries them")
    return path


def _files():
    return sorted(retry_dir().glob("*.json")) if retry_dir().is_dir() else []


def _bad_files():
    return sorted(retry_dir().glob("*.bad")) if retry_dir().is_dir() else []


def _load(path):
    """The parsed file, or raises ValueError with the reason."""
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError) as e:
        raise ValueError(f"{type(e).__name__}: {e}") from e
    if not isinstance(data, dict) or not isinstance(data.get("rows"), list):
        raise ValueError("no 'rows' list")
    for i, item in enumerate(data["rows"]):
        if not isinstance(item, dict) or item.get("tab") not in TABS or not isinstance(item.get("row"), dict):
            raise ValueError(f"row {i} is not {{'tab': one of {list(TABS)}, 'row': {{...}}}}")
    return data


def _mark_bad(path, reason):
    bad = path.with_suffix(".bad")
    path.rename(bad)
    print(f"::error title=Sheet retry file unreadable::{path.name} ({reason}) renamed {bad.name}; "
          f"its rows were not written — fix it and rename it back to .json, or write them by hand")
    return bad


def _sort_key(item):
    path, data = item
    return (data.get("created_at") or "", path.name)


def replay(write):
    """Retry every file, oldest first. write(tab, rows) is sheets.write_rows.
    Returns a list of error strings (empty when everything was written)."""
    loaded = []
    for path in _files():
        try:
            loaded.append((path, _load(path)))
        except ValueError as e:
            _mark_bad(path, e)
    if not loaded:
        if not _bad_files():
            print("[SheetRetry] nothing waiting in state/sheet_retry/")
        return []
    errors = []
    for path, data in sorted(loaded, key=_sort_key):
        left, file_errors = [], []
        for tab in TABS:
            rows = [item["row"] for item in data["rows"] if item["tab"] == tab]
            if not rows:
                continue
            try:
                n = write(tab, rows)
                print(f"[SheetRetry] {path.name}: {tab}: {n} of {len(rows)} rows written "
                      f"(the rest were on the tab already)")
            except Exception as e:                           # noqa: BLE001 -- kept, reported, retried
                file_errors.append(f"{tab}: {type(e).__name__}: {e}")
                left += [item for item in data["rows"] if item["tab"] == tab]
        if not left:
            path.unlink()
            print(f"[SheetRetry] {path.name}: every row is on the Sheet; file deleted")
            continue
        data.update(rows=left, attempts=int(data.get("attempts") or 1) + 1,
                    last_error="; ".join(file_errors))
        write_json_atomic(str(path), data, indent=2)
        msg = f"{path.name}: {len(left)} rows still not written: {data['last_error']}"
        print(f"::error title=Sheet retry failed::{msg}")
        errors.append(msg)
    return errors


def _scan():
    """(readable [(path, data)] oldest first, unreadable [(path, reason)]). Read only."""
    good, unreadable = [], []
    for path in _files():
        try:
            good.append((path, _load(path)))
        except ValueError as e:
            unreadable.append((path, str(e)))
    return sorted(good, key=_sort_key), unreadable


def waiting():
    """[(path, data)] for every readable file, oldest first. Read only."""
    return _scan()[0]


def waiting_rows():
    """Every row waiting in a readable file: [{"tab", "row"}]."""
    return [item for _, data in waiting() for item in data["rows"]]


def _date(data, path):
    return (data.get("created_at") or path.name)[:10]


def banner_lines():
    """The digest banner's lines: one per file waiting (oldest five, then a
    count of the rest) and one per .bad file. Plain text; the emailer escapes it."""
    files, unreadable = _scan()
    lines = []
    for path, data in files[:BANNER_FILES]:
        n = len(data["rows"])
        error = (data.get("last_error") or "unknown").strip().splitlines()[0][:200]
        lines.append(f"{n} posting{'s' if n != 1 else ''} from {_date(data, path)} "
                     f"{'are' if n != 1 else 'is'} waiting to be written to the Sheet; last error: {error}")
    rest = len(files) - BANNER_FILES
    if rest > 0:
        lines.append(f"{rest} more Sheet retry file{'s' if rest != 1 else ''} waiting in state/sheet_retry/.")
    for path, _ in unreadable:        # a dry run reads, never renames
        lines.append(f"Sheet retry file {path.name} could not be read; the next digest renames it .bad.")
    for bad in _bad_files():
        lines.append(f"Sheet retry file {bad.name} could not be read, so its postings were not written; "
                     f"fix it and rename it to .json (state/sheet_retry/).")
    return lines


def alert_keys():
    """REV-04: one key per file waiting or renamed .bad, so an empty day
    alerts once per file (state.unreported_sheet_alerts)."""
    files, unreadable = _scan()
    return ([f"retry:{p.name}" for p, _ in files] + [f"retry:{p.name}" for p, _ in unreadable]
            + [f"retry:{b.name}" for b in _bad_files()])


def summary():
    """(files waiting, oldest date or None, .bad file names) for doctor."""
    files = waiting()
    return len(files), (_date(files[0][1], files[0][0]) if files else None), [b.name for b in _bad_files()]
