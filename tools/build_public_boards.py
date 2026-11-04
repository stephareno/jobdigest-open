# tools/build_public_boards.py — ship the maintainer's pre-resolve of the public segment (PUB-12, PUB-15)
#
#   JOB_DIGEST_PROFILE=<throwaway profile listing only starter_companies.csv, tier watch,
#                       filter segment: [public]> \
#     python main.py resolve --yes --max 500 --workdir <dir>      # repeated until nothing is left
#   python tools/build_public_boards.py --workdir <dir>
#
# Reads the resolution files `resolve --workdir` wrote (never the engine's own)
# and writes:
#   data/company_lists/starter_boards.csv   its public rows (name, ats, url,
#       website): every board and Workday tenant found, plus careers pages as
#       ats `careers_page` (kept for coverage; not fetched as boards:
#       sources/bespoke.py reads only sourcing/startups_unresolved.csv). The
#       startup rows (tools/build_starter_boards.py) are kept, first; a public
#       board that repeats one of them is written once.
#   data/company_lists/starter_companies.csv   the `outcome` column of its
#       public rows filled for every company the run settled: board | workday | careers_page |
#       unsupported:<vendor> | none | none/workable_unchecked (no website and
#       Workable not probed, so a probe from another address should retry only
#       those). Deferred companies (HTTP 429) stay empty, and with --mark-untried
#       (shipping a partial run) every row still empty is `untried`.
#       Its website column takes the website the run used when the list had
#       none (and website_source, in a list that has the column).
# data/company_lists/corrections.csv (PUB-13) is applied last to both files, so
# a hand correction survives every rebuild.
# Unsupported vendors and failures are NOT shipped as parked rows; the outcome
# column is the record. Every board URL must give back its token
# (company_lists.board_token), or the run stops.

import argparse
import csv
import os
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import company_lists  # noqa: E402
import resolve  # noqa: E402
from tools import build_starter_boards  # noqa: E402
from tools import build_starter_companies as starter  # noqa: E402

LIST = starter.COMPANIES
OUT = starter.BOARDS
HEADER = build_starter_boards.HEADER

csv.field_size_limit(10 ** 9)


def _rows(path):
    if not os.path.exists(path):
        return []
    with open(path, newline="") as fh:
        return list(csv.DictReader(fh))


def collect(workdir):
    """(boards rows, {name casefold: outcome}, {name casefold: (website, source)})."""
    paths = resolve._paths(workdir)
    outcome, sites, boards = {}, {}, []
    resolved = paths["resolved"]
    if os.path.exists(resolved):
        websites = {r["company"].casefold(): r.get("website", "") for r in _rows(resolved)}
        for b in build_starter_boards.rows(resolved):
            b["website"] = websites.get(b["name"].casefold(), "")
            boards.append(b)
            key = b["name"].casefold()
            outcome.setdefault(key, "workday" if b["ats"] == "workday" else "board")
    # A sister company on the same board keeps its outcome; the board file holds one row a board.
    for r in _rows(resolved):
        if r.get("ats") and r.get("board_url"):
            outcome.setdefault(r["company"].casefold(), "workday" if r["ats"] == "workday" else "board")
    for r in _rows(resolved) + _rows(paths["bespoke"]):
        if r.get("website"):
            sites.setdefault(r["company"].casefold(), (r["website"], r.get("website_source", "")))
    for r in _rows(paths["bespoke"]):
        key = r["company"].casefold()
        if key in outcome or not r.get("careers_url"):
            continue
        outcome[key] = "careers_page"
        boards.append({"name": r["company"], "ats": "careers_page", "url": r["careers_url"],
                       "website": r.get("website", "")})
    for r in _rows(paths["parked"]):
        key = r["company"].casefold()
        if key in outcome:
            continue
        reason = r.get("reason", "")
        if reason.startswith("unsupported vendor:"):
            outcome[key] = f"unsupported:{reason.split(':', 1)[1].strip()}"
        elif "workable not probed" in reason:
            outcome[key] = "none/workable_unchecked"
        else:
            outcome[key] = "none"
    return boards, outcome, sites


def write_boards(boards, path=OUT, companies=LIST):
    """Replace the public rows of the boards file: the startup companies' rows
    stay, first, and a board that repeats one of them is dropped."""
    startup = starter.segment_names(starter.read_companies(companies), "startup")
    kept = [b for b in starter.read_boards(path) if b["name"].casefold() in startup]
    keys = {starter.board_key(b) for b in kept}
    starter.write_boards(kept + [b for b in boards if starter.board_key(b) not in keys], path)


def update_list(outcome, sites, path=LIST, mark_untried=False):
    """Fill `outcome` (and a website the list lacked) in place; the comment
    line is kept. Startup rows (segment startup) are left as they are."""
    with open(path, encoding="utf-8") as fh:
        comments = [line for line in fh if line.lstrip().startswith("#")]
    rows = company_lists.read_list(str(path))
    with open(path, newline="") as fh:
        header = next(csv.reader(line for line in fh if not line.lstrip().startswith("#")))
    if "outcome" not in header:
        header.insert(header.index("notes") if "notes" in header else len(header), "outcome")
    for r in rows:
        if r.get("segment") == "startup":
            continue
        key = r["name"].casefold()
        if key in outcome:
            r["outcome"] = outcome[key]
        elif mark_untried and not r.get("outcome"):
            r["outcome"] = "untried"
        if not r.get("website") and key in sites and sites[key][0]:
            r["website"], r["website_source"] = sites[key]
    company_lists.apply_corrections(rows)
    with open(path, "w", newline="", encoding="utf-8") as fh:
        fh.writelines(comments)
        writer = csv.DictWriter(fh, fieldnames=header, lineterminator="\n", extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    return rows


def check_guessed(path, boards_path=OUT):
    """The rows in the boards file whose website is a guessed domain and fails
    the stricter check (PUB-13). `path` is a list that carries website_source:
    a tools/build_public_companies.py build (the shipped starter list does not
    carry the column). Fetches each homepage; writes nothing."""
    from concurrent.futures import ThreadPoolExecutor

    import websites
    boards = {r["name"].casefold(): r for r in company_lists.read_list(str(boards_path))}
    listed = company_lists.read_list(str(path))
    if listed and "website_source" not in listed[0]:
        sys.exit(f"{path} has no website_source column: pass a tools/build_public_companies.py build "
                 f"with --list")
    rows = [r for r in listed
            if r.get("website_source") == "guess" and r["name"].casefold() in boards]
    http = websites.Http("job-digest-agent website ladder")

    def fails(row):
        status, _, html = http.get(row["website"])
        return 200 <= status < 300 and not websites.title_names_company(html, row["name"], row["website"])

    with ThreadPoolExecutor(16) as pool:
        verdicts = list(pool.map(fails, rows))
    failed = [boards[r["name"].casefold()] for r, bad in zip(rows, verdicts) if bad]
    print(f"{len(rows)} rows have a guessed website; {len(failed)} fail the stricter check")
    for b in failed:
        print(f"  {b['name']}: {b['ats']}, {b['website']}")
    return failed


def main(argv=None):
    parser = argparse.ArgumentParser(description="Write the public rows of starter_boards.csv and the public "
                                                 "segment's outcome column")
    parser.add_argument("--workdir", help="the directory `resolve --workdir` wrote")
    parser.add_argument("--check-guessed", action="store_true",
                        help="report the shipped rows on a guessed domain that fail the stricter check; writes "
                             "nothing (needs --list)")
    parser.add_argument("--list", dest="list_file",
                        help="with --check-guessed: a tools/build_public_companies.py build (it has website_source)")
    parser.add_argument("--mark-untried", action="store_true",
                        help="ship a partial run: mark every company the run has not settled `untried`")
    args = parser.parse_args(argv)
    if args.check_guessed:
        if not args.list_file:
            parser.error("--check-guessed needs --list (a tools/build_public_companies.py build)")
        check_guessed(args.list_file)
        return
    if not args.workdir:
        parser.error("--workdir is required")
    boards, outcome, sites = collect(args.workdir)
    boards = company_lists.correct_boards(boards)
    write_boards(boards)
    rows = [r for r in update_list(outcome, sites, mark_untried=args.mark_untried) if r.get("segment") != "startup"]
    counts = Counter((r.get("outcome") or "untried").split(":")[0] for r in rows)
    vendors = Counter(r["outcome"].split(":", 1)[1] for r in rows if r.get("outcome", "").startswith("unsupported:"))
    print(f"wrote {OUT.relative_to(ROOT)}: {len(boards)} public rows "
          f"({sum(1 for b in boards if b['ats'] != 'careers_page')} boards and tenants, "
          f"{sum(1 for b in boards if b['ats'] == 'careers_page')} careers pages)")
    print(f"updated {LIST.relative_to(ROOT)}: " + ", ".join(f"{k} {n}" for k, n in counts.most_common()))
    if vendors:
        print("  unsupported vendors: " + ", ".join(f"{v} {n}" for v, n in vendors.most_common()))


if __name__ == "__main__":
    main()
