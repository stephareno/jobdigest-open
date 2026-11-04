"""SRC-17 part 2: Bespoke state across a careers-URL change, and parked-domain URLs."""

from __future__ import annotations

import csv
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import sources.bespoke as bespoke  # noqa: E402
from sources.bespoke import CheckResult, prior_for, record_check  # noqa: E402

OLD = {"url": "https://guessed-domain.io/careers", "etag": '"abc"',
       "last_modified": "Mon, 21 Sep 2026 00:00:00 GMT", "fingerprint": "f00",
       "last_extracted": "2026-09-18", "words": 900}


def test_prior_state_is_kept_for_the_same_url():
    state = {"acme": dict(OLD)}
    assert prior_for(state, "Acme", OLD["url"]) == OLD


def test_prior_state_is_dropped_when_the_careers_url_moves():
    state = {"acme": dict(OLD)}
    assert prior_for(state, "Acme", "https://acme.com/careers") == {}


def test_a_moved_url_reads_as_never_extracted():
    state = {"acme": dict(OLD)}
    r = CheckResult("Acme", "https://acme.com/careers")
    r.status, r.fingerprint, r.words, r.changed = 200, "f00", 900, False
    stats = bespoke._new_stats(1)
    entry, needs = record_check(r, prior_for(state, "Acme", r.url), "2026-09-22", stats, [])
    assert needs  # the new page is read, not skipped as already extracted
    assert "last_extracted" not in entry and "etag" not in entry


def test_domain_marketplace_urls_are_skipped(tmp_path, capsys):
    path = tmp_path / "unresolved.csv"
    with path.open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=["company", "careers_url", "reason"])
        w.writeheader()
        w.writerow({"company": "LyteLoop", "reason": "bespoke_page",
                    "careers_url": "https://www.hugedomains.com/domain_profile.cfm?d=lyteloop.com"})
        w.writerow({"company": "Co-Develop", "reason": "bespoke_page",
                    "careers_url": "https://forsale.dynadot.com/co-develop.com"})
        w.writerow({"company": "Acme", "reason": "bespoke_page", "careers_url": "https://acme.com/jobs"})
    companies = bespoke.load_companies(path)
    assert [c["company"] for c in companies] == ["Acme"]
    assert "skipped 2 careers URLs" in capsys.readouterr().out
