"""SRC-18: Bespoke careers URLs that sit off the company's own website.

Bespoke reads sourcing/startups_unresolved.csv. A row's careers_url should come
from the company's real website (companies_master.csv `website`) or a page it
links to. Before SRC-17 the resolver also tried guessed domains, so some rows
point at another company's site (Umbra was recorded on umbraco.com).

  candidates  — writes resolver input and seed URLs for every Bespoke-eligible
                row outside the SRC-17 sectors whose careers_url's registrable
                domain differs from its harvest website's. Feed it to
                resolve_crunchbase_ats.py --seed-only, then merge.

  report      — compares the pre-run unresolved CSV with the current one for
                those companies: careers_url kept / replaced / dropped, why the
                kept ones are legitimate, newly resolved boards.

Companies the re-run leaves with no confirmed page keep their row in
startups_unresolved.csv (reason no_site) and are also recorded in
sourcing/bespoke_dropped.csv with the URL they used to carry, why they were
dropped and the date -- parked, never deleted (CLAUDE.md). They are NOT added
to sourcing/parked_boards.csv: the monthly probe unparks a careers_page row
whose URL returns 200, and these URLs are other companies' sites, so parking
them there would feed the wrong page straight back into Bespoke.

Re-checking them means asking their own website again, not fetching the old
URL, so it is a seed-only resolver pass, not a probe. That pass is
sourcing/recheck_dropped.py, run monthly by resolve_ats.yml (OPS-11).

Website lookups go through sourcing/harvest.py: the full companies_master.csv
when it is here, else the committed companies_min.csv.gz, so `candidates` also
runs on the GitHub Actions runner. They match on crunchbase_slug (SRC-19): a
name lookup, the original SRC-18 run's, checked 101 companies against a
same-named company's website. The seed file carries the slug for the same reason.

Usage:
    python3 sourcing/offdomain_careers.py candidates --out-dir DIR
    python3 sourcing/offdomain_careers.py report --before OLD_UNRESOLVED.csv --input DIR/offdomain_input.csv
"""

from __future__ import annotations

import argparse
import collections
import csv
import sys
from pathlib import Path
from urllib.parse import urlsplit

SOURCING = Path(__file__).resolve().parent
REPO = SOURCING.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(SOURCING))

import config  # noqa: E402
from categories import SECTORS  # noqa: E402
from harvest import company_lookup, row_key  # noqa: E402

UNRESOLVED = SOURCING / "startups_unresolved.csv"
RESOLVED = SOURCING / "startups_ats_resolved.csv"
INPUT_COLUMNS = ["company", "crunchbase_slug", "category", "hq", "market", "industries", "cb_rank"]

# Hosts that serve many employers' job pages: an off-domain careers_url here
# was reached by a link from the company's own site.
ATS_HOSTS = (
    "paylocity.com", "applytojob.com", "taleo.net", "icims.com", "jobvite.com", "myworkdayjobs.com",
    "workday.com", "successfactors.com", "hr.cloud.sap", "ultipro.com", "ukg.net", "paycor.com",
    "paycomonline.net", "dayforcehcm.com", "adp.com", "applicantpro.com", "catsone.com",
    "zohorecruit.com", "jazzhr.com", "trinethire.com", "gusto.com", "dover.com", "notion.site",
    "notion.com", "eightfold.ai", "phenom.com", "pinpointhq.com", "clearcompany.com", "getro.com",
    "breezy.hr", "bamboohr.com", "rippling.com", "recruitee.com", "kula.ai", "homerun.co",
    "hire.trakstar.com", "trakstar.com", "entertimeonline.com", "isolvedhire.com", "hirebridge.com",
    "avature.net", "hrmdirect.com", "polymer.co", "careerplug.com", "typeform.com",
    "google.com", "ycombinator.com", "wellfound.com", "linkedin.com", "indeed.com", "ziprecruiter.com",
)

csv.field_size_limit(10 ** 9)


def read(path: Path) -> list[dict[str, str]]:
    with path.open(newline="") as fh:
        return list(csv.DictReader(fh))


def host_of(url: str) -> str:
    url = (url or "").strip()
    return (urlsplit(url if "//" in url else "//" + url).hostname or "").lower()


def registrable(url: str) -> str:
    h = host_of(url)
    h = h[4:] if h.startswith("www.") else h
    return ".".join(h.split(".")[-2:])


def on_any(host: str, suffixes) -> bool:
    return any(host == s or host.endswith("." + s) for s in suffixes)


def bespoke_eligible(row: dict[str, str]) -> bool:
    return ((row.get("careers_url") or "").startswith("http")
            and (row.get("reason") or "").startswith(config.BESPOKE_REASON_PREFIXES))


def cmd_candidates(args: argparse.Namespace) -> None:
    find = company_lookup(("website", "cb_rank"))
    rows, seen = [], set()
    counts = collections.Counter()
    for r in read(UNRESOLVED):
        k = row_key(r)
        if k in seen or not bespoke_eligible(r):
            continue
        seen.add(k)
        if r.get("category") in SECTORS:
            counts["sector (done in SRC-17)"] += 1
            continue
        own = find(r.get("crunchbase_slug", ""), r["company"]) or {}
        website = (own.get("website") or "").strip()
        if not website:
            counts["no harvest website"] += 1
            continue
        if registrable(website) == registrable(r["careers_url"]):
            counts["on the harvest domain"] += 1
            continue
        counts["off-domain -> re-run"] += 1
        rows.append({**{c: r.get(c, "") for c in INPUT_COLUMNS},
                     "cb_rank": own.get("cb_rank", ""), "_website": website})
    rows.sort(key=lambda r: int(r["cb_rank"]) if (r["cb_rank"] or "").isdigit() else 10 ** 9)
    out = args.out_dir
    out.mkdir(parents=True, exist_ok=True)
    with (out / "offdomain_input.csv").open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=INPUT_COLUMNS, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    with (out / "offdomain_seed_urls.csv").open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["company", "crunchbase_slug", "url"])
        w.writerows([r["company"], r["crunchbase_slug"], r["_website"]] for r in rows)
    for label, n in counts.most_common():
        print(f"  {label:28s} {n:6d}")
    print(f"Wrote {out / 'offdomain_input.csv'} ({len(rows)} companies, cb_rank order)")


def why_kept(website: str, url: str, landing_host: str | None) -> str:
    host = host_of(url)
    if on_any(host, ATS_HOSTS):
        return "ATS / job-board host"
    if landing_host and registrable("//" + landing_host) == registrable(url):
        return "rebrand (website redirects there)"
    return "linked from the site (parent company or other)"


def cmd_report(args: argparse.Namespace) -> None:
    from tools.resolve_ats import PageCache  # cache lookup only; no network

    cache = PageCache(args.cache_dir)
    find = company_lookup()
    before = {row_key(r): r for r in read(args.before)}
    after = {row_key(r): r for r in read(UNRESOLVED)}
    boards = {row_key(r): r for r in read(RESOLVED) if (r.get("ats") or "").strip()}
    targets = [row_key(r) for r in read(args.input)]

    outcome = collections.Counter()
    kept_why = collections.Counter()
    examples: dict[str, list[str]] = collections.defaultdict(list)
    for k in targets:
        old = before[k]["careers_url"]
        website = ((find(before[k].get("crunchbase_slug", ""), before[k]["company"]) or {})
                   .get("website") or "").strip()
        if k in boards:
            label = "newly resolved to a board"
        elif k not in after or not bespoke_eligible(after[k]):
            reason = after.get(k, {}).get("reason", "(gone)")
            label = f"dropped ({reason.split(':')[0]})"
        elif after[k]["careers_url"] == old:
            label = "kept (same URL, reached from the real site)"
            hit = cache.get(website if "//" in website else "https://" + website)
            landing = host_of(hit["final_url"]) if hit and hit.get("final_url") else None
            kept_why[why_kept(website, old, landing)] += 1
        elif registrable(after[k]["careers_url"]) == registrable(website):
            label = "replaced (now on the harvest domain)"
        else:
            label = "replaced (another page linked from the real site)"
        outcome[label] += 1
        if len(examples[label]) < 8:
            examples[label].append(f"{before[k]['company']}: {old} -> "
                                   f"{after.get(k, {}).get('careers_url') or boards.get(k, {}).get('board_url', '')}")
    print(f"{len(targets)} off-domain companies re-run")
    for label, n in outcome.most_common():
        print(f"  {label:52s} {n:6d}")
    print("\nKept, by why:")
    for label, n in kept_why.most_common():
        print(f"  {label:52s} {n:6d}")
    for label, ex in examples.items():
        print(f"\n{label}:")
        for e in ex:
            print(f"    {e}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("candidates")
    c.add_argument("--out-dir", type=Path, required=True)
    c.set_defaults(func=cmd_candidates)
    r = sub.add_parser("report")
    r.add_argument("--before", type=Path, required=True)
    r.add_argument("--input", type=Path, required=True)
    r.add_argument("--cache-dir", type=Path, default=REPO / ".ats_cache")
    r.set_defaults(func=cmd_report)
    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
