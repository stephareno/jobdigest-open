# tools/build_public_companies.py — build the public segment of data/company_lists/starter_companies.csv (PUB-11, PUB-15)
#
#   GMAIL_ADDRESS=you@example.com python tools/build_public_companies.py --out /tmp/public.csv
#       [--limit N] [--guess-websites] [--no-ladder]
#   python tools/build_starter_companies.py --public /tmp/public.csv
#
# --out is required: the build is one segment, and
# tools/build_starter_companies.py merges it into starter_companies.csv beside
# the startup rows. Writing straight over starter_companies.csv would drop them.
#
# Public-domain sources only:
#   SEC EDGAR company_tickers_exchange.json   name, ticker, exchange, CIK
#   SEC submissions (data.sec.gov)            SIC code and description, state
#   Wikidata (CC0), one SPARQL query          official website, by CIK or ticker
# SEC asks every client for a User-Agent naming a contact; this sends
# GMAIL_ADDRESS (required) and stays under SEC's 10 requests per second.
# Each submissions reply is cached, reduced to the fields used, under
# .sec_cache/ (gitignored), so a re-run fetches only what is new.
#
# PUB-12: every website then goes through the website ladder (websites.py)
# and is kept only once the page it serves names the company. Rung 1 is the
# CIK/ticker match from Wikidata (website_source "wikidata"), then SEC's own
# website field ("list"); rung 2 Wikidata by name ("wikidata"); rung 3 domain
# guesses ("guess"); rung 4, only with --guess-websites, one utility-model call
# ("model"; run here by the maintainer, every result verified). Wikidata
# replies are cached under .website_cache/ (gitignored). An existing file's
# `outcome` column (written by tools/build_public_boards.py) is carried over
# by CIK. data/company_lists/corrections.csv (PUB-13) is applied last.
#
# Kept: companies listed on NYSE, Nasdaq or CBOE whose SEC business address
# is in a US state or DC, one row per CIK (its first ticker). `state` is that
# business-address state (where the company is based), not the state of
# incorporation (Delaware for most). Columns: name, website, cik, ticker,
# exchange, sic, sic_description, state, notes. The first line is a comment
# with the build date and the sources.

import argparse
import csv
import datetime
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import company_lists  # noqa: E402
import places  # noqa: E402

# Where the build ends up, by way of tools/build_starter_companies.py.
STARTER = ROOT / "data" / "company_lists" / "starter_companies.csv"
CACHE = ROOT / ".sec_cache"
TICKERS_URL = "https://www.sec.gov/files/company_tickers_exchange.json"
SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik:010d}.json"
WIKIDATA_URL = "https://query.wikidata.org/sparql"
EXCHANGES = ("NYSE", "Nasdaq", "CBOE")
COLUMNS = ["name", "website", "website_source", "cik", "ticker", "exchange", "sic", "sic_description", "state",
           "outcome", "notes"]
WEBSITE_CACHE = ROOT / ".website_cache" / "wikidata"
SEC_INTERVAL = 0.125            # 8 requests a second, under SEC's 10
US_STATES = set(places._STATES) | {"DC"}

# Every company with a SEC CIK (P5531), or a ticker (P249) on the NYSE
# (Q13677) or Nasdaq (Q82059), and an official website (P856). Tickers on
# other exchanges are left out: the same symbol names another company there.
WIKIDATA_QUERY = """
SELECT ?cik ?ticker ?website WHERE {
  ?item wdt:P856 ?website .
  { ?item wdt:P5531 ?cik . }
  UNION
  { ?item p:P414 ?listing . ?listing ps:P414 ?exchange ; pq:P249 ?ticker .
    VALUES ?exchange { wd:Q13677 wd:Q82059 } }
}
"""


def _session(user_agent):
    import requests
    s = requests.Session()
    s.headers.update({"User-Agent": user_agent, "Accept-Encoding": "gzip, deflate"})
    return s


class Sec:
    """SEC fetches, throttled, with the submissions cache."""

    def __init__(self, get, cache_dir=CACHE, sleep=time.sleep, clock=time.monotonic):
        self.get, self.cache_dir, self.sleep, self.clock = get, Path(cache_dir), sleep, clock
        self._last = 0.0
        self.fetched = self.cached = 0

    def _wait(self):
        delay = SEC_INTERVAL - (self.clock() - self._last)
        if delay > 0:
            self.sleep(delay)
        self._last = self.clock()

    def tickers(self):
        self._wait()
        data = self.get(TICKERS_URL)
        fields = data["fields"]
        return [dict(zip(fields, row)) for row in data["data"]]

    def submission(self, cik):
        """{sic, sic_description, state, website} for one CIK, from the cache when there."""
        path = self.cache_dir / f"{int(cik):010d}.json"
        if path.exists():
            self.cached += 1
            return json.loads(path.read_text())
        self._wait()
        raw = self.get(SUBMISSIONS_URL.format(cik=int(cik)))
        business = (raw.get("addresses") or {}).get("business") or {}
        kept = {"sic": str(raw.get("sic") or ""), "sic_description": raw.get("sicDescription") or "",
                "state": business.get("stateOrCountry") or "", "website": raw.get("website") or ""}
        self.cache_dir.mkdir(exist_ok=True)
        path.write_text(json.dumps(kept))
        self.fetched += 1
        return kept


def _clean_site(url):
    url = (url or "").strip()
    if not url:
        return ""
    if "://" not in url:
        url = "https://" + url
    return url.rstrip("/")


def wikidata_websites(get_sparql):
    """({cik: website}, {TICKER: website}) from one SPARQL query. The shortest
    URL wins when an item lists several (the bare homepage over a subpage)."""
    data = get_sparql(WIKIDATA_QUERY)
    by_cik, by_ticker = {}, {}
    for b in data["results"]["bindings"]:
        site = _clean_site(b["website"]["value"])
        if "cik" in b:
            key = b["cik"]["value"].lstrip("0")
            if key and (key not in by_cik or len(site) < len(by_cik[key])):
                by_cik[key] = site
        if "ticker" in b:
            key = b["ticker"]["value"].strip().upper()
            if key and (key not in by_ticker or len(site) < len(by_ticker[key])):
                by_ticker[key] = site
    return by_cik, by_ticker


def build(sec, get_sparql, limit=None, log=print):
    listed, seen = [], set()
    for t in sec.tickers():
        cik = str(t.get("cik") or "")
        if t.get("exchange") not in EXCHANGES or not cik or cik in seen:
            continue
        seen.add(cik)
        listed.append(t)
    if limit:
        listed = listed[:limit]
    log(f"[Build] {len(listed)} companies on {', '.join(EXCHANGES)} (one row per CIK)")
    try:
        by_cik, by_ticker = wikidata_websites(get_sparql)
        log(f"[Build] Wikidata: {len(by_cik)} websites by CIK, {len(by_ticker)} by ticker")
    except Exception as e:                             # noqa: BLE001 -- websites are best effort
        log(f"[Build] Wikidata query failed ({type(e).__name__}: {e}); building without its websites")
        by_cik, by_ticker = {}, {}
    rows = []
    for i, t in enumerate(listed, 1):
        sub = sec.submission(t["cik"])
        if sub["state"] not in US_STATES:
            continue
        cik = str(t["cik"])
        rows.append({"name": " ".join(str(t["name"]).split()), "website": "", "website_source": "",
                     "cik": cik, "ticker": t["ticker"], "exchange": t["exchange"], "sic": sub["sic"],
                     "sic_description": sub["sic_description"], "state": sub["state"], "outcome": "",
                     "notes": "",
                     "_wikidata": by_cik.get(cik) or by_ticker.get(str(t["ticker"]).upper()) or "",
                     "_sec": _clean_site(sub["website"])})
        if i % 500 == 0:
            log(f"[Build] {i}/{len(listed)} submissions read ({sec.fetched} fetched, {sec.cached} cached)")
    rows.sort(key=lambda r: r["name"].casefold())
    return rows


def apply_ladder(rows, ladder, log=print, workers=16):
    """Fill website / website_source from the ladder: the Wikidata CIK/ticker
    site, then SEC's, each verified, then ladder.find (Wikidata by name, domain
    guesses, the model when on)."""
    import websites

    def find(row):
        for url, source in ((row["_wikidata"], "wikidata"), (row["_sec"], "list")):
            if url:
                site = websites.verify(ladder.http, url, row["name"])
                if site:
                    return site, source
        return ladder.find({**row, "website": ""})
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for n, (row, (site, source)) in enumerate(zip(rows, pool.map(find, rows)), 1):
            row["website"], row["website_source"] = site, source
            if n % 250 == 0:
                log(f"[Build] {n}/{len(rows)} companies through the website ladder")
    return rows


def carry_outcomes(rows, path):
    """Keep the `outcome` an existing file holds, by CIK."""
    if not Path(path).exists():
        return rows
    old = {r.get("cik"): r.get("outcome", "") for r in company_lists.read_list(str(path))}
    for r in rows:
        r["outcome"] = old.get(r["cik"], "")
    return rows


def write(rows, path, today=None):
    today = today or datetime.date.today().isoformat()
    with open(path, "w", newline="", encoding="utf-8") as fh:
        fh.write(f"# Built {today} by tools/build_public_companies.py from SEC EDGAR "
                 f"(company_tickers_exchange.json and submissions; public domain) and Wikidata (CC0).\n")
        writer = csv.DictWriter(fh, fieldnames=COLUMNS, lineterminator="\n", extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Build the public segment of data/company_lists/starter_companies.csv")
    parser.add_argument("--limit", type=int, help="only the first N listed companies (a trial run)")
    parser.add_argument("--guess-websites", action="store_true",
                        help="ladder rung 4: one utility-model call per company still without a verified website")
    parser.add_argument("--no-ladder", action="store_true", help="skip website verification (a quick trial)")
    parser.add_argument("--out", required=True,
                        help=f"write the build here; tools/build_starter_companies.py --public <it> merges it "
                             f"into {STARTER.relative_to(ROOT)}")
    args = parser.parse_args(argv)
    contact = os.environ.get("GMAIL_ADDRESS", "").strip()
    if not contact:
        sys.exit("GMAIL_ADDRESS missing: SEC requires a User-Agent with a contact address. Set it, then run again.")
    session = _session(f"job-digest-agent public-company list builder ({contact})")

    def get(url):
        r = session.get(url, timeout=30)
        r.raise_for_status()
        return r.json()

    def get_sparql(query):
        r = session.get(WIKIDATA_URL, params={"query": query, "format": "json"}, timeout=120)
        r.raise_for_status()
        return r.json()

    sec = Sec(get)
    started = time.monotonic()
    rows = build(sec, get_sparql, args.limit)
    guesser = None
    if args.no_ladder:
        for r in rows:
            r["website"] = r["_wikidata"] or r["_sec"]
            r["website_source"] = "wikidata" if r["_wikidata"] else "list" if r["_sec"] else ""
    else:
        import websites
        http = websites.Http(f"job-digest-agent public-company list builder ({contact})")
        wikidata = websites.Wikidata(http.json, str(WEBSITE_CACHE))
        if args.guess_websites:
            import config
            guesser = websites.ModelGuesser(config.PROFILE_MODEL,
                                            str(ROOT / ".website_cache" / "model"))
        ladder = websites.Ladder(http, wikidata, guesser)
        apply_ladder(rows, ladder)
        print(f"[Build] website ladder: {len(ladder.rejected)} candidate sites failed the content check, "
              f"{websites.BLOCKED['n']} candidate pages refused to be read (401/403/429/503); "
              f"Wikidata {wikidata.fetched} calls, {wikidata.cached} from the cache")
    out = Path(args.out)
    carry_outcomes(rows, out)
    company_lists.apply_corrections(rows)
    write(rows, out)
    with_site = sum(1 for r in rows if r["website"])
    by_source = {s: sum(1 for r in rows if r["website_source"] == s) for s in ("list", "wikidata", "guess", "model")}
    print(f"wrote {out}: {len(rows)} companies, {with_site} with a website "
          f"({100 * with_site / max(len(rows), 1):.1f}%; by source: "
          + ", ".join(f"{s} {n}" for s, n in by_source.items())
          + f"); {sec.fetched} submissions fetched, {sec.cached} from the cache; "
          f"{(time.monotonic() - started) / 60:.0f} min")
    if guesser is not None:
        import config
        print(f"[Build] model guesses: {guesser.calls} calls ({guesser.cached} from the cache) at {guesser.model}, "
              f"{guesser.input_tokens} input and "
              f"{guesser.output_tokens} output tokens, {config.format_cost(guesser.cost())}")


if __name__ == "__main__":
    main()
