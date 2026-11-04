# pending.py — scored postings waiting for the digest (OPS-04)
#
# Fetch runs score postings and hand them to the digest through files under
# config.PENDING_DIR: one new file per fetch run, written atomically and never
# modified afterwards. The digest reads the files present when it starts, sends
# them, and deletes exactly those files.
#
# That split is what lets fetch.yml and daily.yml overlap safely:
#   - a fetch run only ever *adds* a file, the digest only *deletes* files it
#     read, so their commits never touch the same path and never conflict;
#   - a file lands in git as one commit, so the digest (which checks out main)
#     can never read a half-written batch;
#   - a batch committed after the digest checked out is not in its list, so it
#     is left for the next digest -- sent exactly once.
# Seen-state (seen_jobs.json) is updated by the fetch run that scored a posting,
# so later fetch runs never score or queue it again.

import json
import os
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

import profile_loader
from config import PENDING_DIR, function_fit_value, source_key, source_rank
from location import FIL09_TIER_FIELDS, apply_location_tier
from state import (board_id, content_key, employer_board, hiring_key, normalize_url,
                   same_posting, write_json_atomic)

REPO_ROOT = Path(__file__).resolve().parent


def pending_dir():
    return REPO_ROOT / PENDING_DIR


def write_batch(jobs, label, found_at=None, first_run=False):
    """Queue one fetch run's scored postings. Returns the file written, or None if empty."""
    if not jobs:
        return None
    found_at = found_at or datetime.utcnow()
    stamp = found_at.strftime("%Y%m%dT%H%M%S%fZ")
    path = pending_dir() / f"{stamp}-{label}.json"
    write_json_atomic(path, {
        "found_at": found_at.isoformat(),
        "label": label,
        "first_run": bool(first_run),
        "jobs": jobs,
    }, indent=1, default=str)
    return path


def load_pending():
    """Every queued batch, oldest first: (paths, jobs, first_run)."""
    directory = pending_dir()
    if not directory.exists():
        return [], [], False
    paths = sorted(p for p in directory.glob("*.json"))
    jobs, first_run = [], False
    for path in paths:
        with path.open() as f:
            batch = json.load(f)
        first_run = first_run or batch.get("first_run", False)
        for job in batch.get("jobs") or []:
            job.setdefault("found_at", batch.get("found_at", ""))
            # PUB-03: batches queued before the rename carry dc_bonus; read it
            # as home_bonus (in memory; the batch file is never rewritten).
            if "dc_bonus" in job and "home_bonus" not in job:
                job["home_bonus"] = job.pop("dc_bonus")
            # PUB-04: sources are module keys; a batch queued before carries
            # the display name ("Startup", "Built In"). is_insurance is kept
            # as it is and read by scorer.is_hard_blocked.
            if job.get("source"):
                job["source"] = source_key(job["source"])
            if job.get("merged_from"):
                job["merged_from"] = [source_key(s) for s in job["merged_from"]]
            # PUB-10: it_leadership / it_other / not_it read as their new names.
            if job.get("function_fit") is not None:
                job["function_fit"] = function_fit_value(job["function_fit"])
            jobs.append(job)
    return paths, jobs, first_run


# Fields a losing copy hands to the survivor when it scored higher (FIL-10):
# a score and the rationale that explains it must never come from two
# different scoring calls, and the FIL-09 tier fields and the GAP-07 home bonus
# are the parts that score was built from. Anything apply_location_tier writes
# belongs here, or the survivor ends up with one copy's score and another's
# reason for it. dc_bonus is the pre-PUB-03 name of home_bonus and
# is_insurance the pre-PUB-04 form of hard_block_industry, still carried by
# batches queued before them.
_SCORE_FIELDS = ("score", "rationale", "base_score", "location_penalty",
                 "location_method", "location_reason", "home_bonus", "dc_bonus", "comp_adjustment", "relo",
                 "work_model", "comp", "hard_block_industry", "is_insurance", "is_cool_company_wrong_role",
                 "function_fit", "function_capped", "criteria_hits", "work_model_adjustment", "dealbreaker",
                 "dealbreaker_criterion", "dealbreaker_evidence", "dealbreaker_unverified")


def dedupe(jobs):
    """One copy per posting across batches, keyed on URL then content (FIL-10).

    Batches from different fetch runs can hold the same posting from two
    sources (a startup shard in the morning, JSearch in the evening). The
    survivor is the copy from the highest-priority source (config.SOURCE_PRIORITY,
    CLAUDE.md rule 6), so the stored apply link is the best one; if a losing
    copy scored higher, the survivor takes that copy's score and rationale.

    Two postings on the same board under different ids are never merged
    (state.same_posting) — MITRE's R117090 and R116664 stay two rows.
    """
    ordered = sorted(jobs, key=lambda j: (source_rank(j.get("source", "")),
                                          j.get("found_at", "")))
    # One content key can hold more than one posting once the guard has split
    # them, so each key keeps a list of [survivor, idents] clusters.
    seen_urls, by_content, out = set(), {}, []
    for job in ordered:
        url = normalize_url(job.get("apply_link", ""))
        if url and url in seen_urls:
            continue
        key = content_key(job)
        clusters = by_content.setdefault(key, []) if key else []
        cluster = next((c for c in clusters if same_posting(c[1], url)), None)
        if cluster is not None:
            _merge_loser(cluster[0], job)
            cluster[1].extend(i for i in [board_id(url)] if i)
            continue
        if url:
            seen_urls.add(url)
        if key:
            clusters.append([job, [i for i in [board_id(url)] if i]])
        out.append(job)
    return out


def _merge_loser(winner, loser):
    """Fold a dropped duplicate into the copy that survived it."""
    if (loser.get("score") or 0) > (winner.get("score") or 0):
        for field in _SCORE_FIELDS:
            if field in loser:
                winner[field] = loser[field]
    if not winner.get("warm_contact") and loser.get("warm_contact"):
        winner["warm_contact"] = loser["warm_contact"]
    if not winner.get("linkedin_url") and loser.get("linkedin_url"):
        winner["linkedin_url"] = loser["linkedin_url"]
    winner.setdefault("merged_from", [])
    if loser.get("source") and loser["source"] not in winner["merged_from"]:
        winner["merged_from"].append(loser["source"])


# --- FIL-13: one card per hiring company + title -------------------------------
#
# FIL-10's content key needs the posted company and location to agree, so the
# same opening posted under two names or two places still reaches the digest
# twice: World Food Program USA's VP Technology on its own listing and on
# NTEN's, NASA's Deputy CIO on LinkedIn at Washington and at Greenbelt. The
# scoring call names the real employer (hiring_company, SCO-06), so after
# scoring the digest keys on that instead — hiring company + title, no
# location — within the batch and against main-index rows emailed in the last
# config.HIRING_COLLAPSE_LOOKBACK_DAYS days.
#
# Email only: every row is still written to the Sheet. No Claude calls.


def link_tier(url):
    """0 employer board (direct ATS, company site, usajobs.gov), 1 LinkedIn,
    2 job board, 3 aggregator, 4 no link."""
    from url_resolver import LINK_RANK, link_class
    ident = board_id(url)
    host = ident[0] if ident else (urlparse(url or "").netloc or "")
    if host and employer_board(host):
        return 0
    return max(1, LINK_RANK[link_class(url)])


def _row_age_ok(row, today, lookback_days):
    seen = row.get("first_seen")
    return seen is not None and 0 <= (today - seen).days <= lookback_days


def collapse_hiring(jobs, recent_rows=(), today=None, lookback_days=None):
    """The digest's email list: one card per hiring company + title (FIL-13).

    jobs: this digest's postings, already through dedupe(). recent_rows: main-
    index rows as dicts with title, company, hiring_company (if the Sheet has
    the column), apply_link, source, score and first_seen (a date).

    A copy on an employer board never joins one under a different posting id
    on that board — the same-board guard (state.same_posting), as in dedupe():
    MITRE's two requisitions, or Axon's one title posted as six Greenhouse
    requisitions in six cities, stay separate cards. Within what does join, the card is the copy with the best link —
    direct ATS first, then LinkedIn, then job boards and aggregators — the
    highest score breaking a tie. It takes the highest score in the batch with
    the fields that score was built from, so a collapse never pushes an opening
    below the line it cleared, and carries every other copy's link in
    other_links.

    A recent Sheet row in the same group with a link at least as good and a
    score at least as high means the opening was already emailed: no card. A
    copy that scores higher than anything sent (a nearer location, a fuller
    read) is shown again, with the sent links under it. Only Sheet rows that
    cleared scoring.thresholds.worth_a_look count, and none whose link is in this
    batch (a re-run digest). Score-0 rows never join a group. Returns new
    dicts; the jobs passed in are not modified.
    """
    from config import HIRING_COLLAPSE_LOOKBACK_DAYS
    worth_a_look = profile_loader.active()["scoring"]["thresholds"]["worth_a_look"]
    lookback_days = HIRING_COLLAPSE_LOOKBACK_DAYS if lookback_days is None else lookback_days
    today = today or datetime.utcnow().date()

    batch_urls = {normalize_url(j.get("apply_link", "")) for j in jobs} - {""}
    earlier = {}
    for row in recent_rows:
        key = hiring_key(row)
        if (key and _row_age_ok(row, today, lookback_days)
                and _score(row) >= worth_a_look
                and normalize_url(row.get("apply_link", "")) not in batch_urls):
            earlier.setdefault(key, []).append(row)

    # Score-0 rows are never emailed, and an international or hard-blocked copy
    # must never lend its link to a US one, so they stay out of every group.
    groups, order = {}, []
    for i, job in enumerate(jobs):
        key = (hiring_key(job) if _score(job) > 0 else None) or ("__unkeyed", i)
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(job)

    out = []
    for key in order:
        batch = groups[key]
        sent = earlier.get(key, [])
        if len(batch) == 1 and not sent:
            out.append(dict(batch[0]))
            continue
        for cluster in _guarded_clusters(batch, sent):
            card = _card(key, cluster)
            if card is not None:
                out.append(card)
    return out


def _rank(copy, is_sent=False):
    """Best link first, then highest score; a batch copy before a sent one."""
    return (link_tier(copy.get("apply_link", "")), -_score(copy), is_sent,
            source_rank(copy.get("source", "")))


def _guarded_clusters(batch, sent):
    """Split one hiring-key group by the same-board guard: [[(copy, is_sent)]]."""
    clusters = []
    for copy, is_sent in sorted([(j, False) for j in batch] + [(r, True) for r in sent],
                                key=lambda c: _rank(*c)):
        url = normalize_url(copy.get("apply_link", ""))
        for cluster in clusters:
            idents = [i for i in (board_id(normalize_url(c.get("apply_link", ""))) for c, _ in cluster) if i]
            if same_posting(idents, url):
                cluster.append((copy, is_sent))
                break
        else:
            clusters.append([(copy, is_sent)])
    return clusters


def _card(key, cluster):
    """The one card a guarded cluster shows, or None when it shows none."""
    batch = [c for c, is_sent in cluster if not is_sent]
    sent = [c for c, is_sent in cluster if is_sent]
    if not batch:
        return None
    best = min(batch, key=_rank)
    card = dict(best)
    top = max(batch, key=_score)
    if _score(top) > _score(card):
        # No location in the key, so the location the score was tiered on
        # travels with it.
        for field in _SCORE_FIELDS + ("location",):
            if field in top:
                card[field] = top[field]
    tier = link_tier(card.get("apply_link", ""))
    covered = [r for r in sent
               if link_tier(r.get("apply_link", "")) <= tier and _score(r) >= _score(card)]
    if covered:
        r = covered[0]
        print(f"[Collapse] {len(batch)} posting(s) of {card.get('title', '')!r} at {key[0]!r} "
              f"left out: already emailed {r['first_seen']} ({r.get('source', '')}, "
              f"score {r.get('score')}, {r.get('apply_link', '')})")
        return None
    if len(cluster) == 1:
        return card
    card["other_links"] = _other_links(card, [c for c, _ in cluster if c is not best], sent)
    print(f"[Collapse] {len(cluster)} copies of {card.get('title', '')!r} at {key[0]!r} -> one card "
          f"({card.get('source', '')}, score {card.get('score')}); also: "
          + ", ".join(f"{l['source']} {l['company']!r}" for l in card["other_links"]))
    return card


def _score(job):
    try:
        return int(float(job.get("score") or 0))
    except (TypeError, ValueError):
        return 0


def _other_links(card, others, sent):
    """The links a card's collapsed copies leave behind, best link first."""
    shown = {normalize_url(card.get("apply_link", "")), normalize_url(card.get("linkedin_url", ""))}
    links = []
    for other in sorted(others, key=lambda o: link_tier(o.get("apply_link", ""))):
        url = normalize_url(other.get("apply_link", ""))
        if not url or url in shown:
            continue
        shown.add(url)
        links.append({"url": url, "source": other.get("source", ""),
                      "company": other.get("company", ""),
                      "sent": str(other["first_seen"]) if any(other is r for r in sent) else ""})
    return links


def retier_legacy(jobs):
    """Re-tier queued postings scored before FIL-09 (no tier fields), in memory.

    Their stored score already carries the old flat location penalty. The base
    is score − stored location_penalty; the FIL-09 tier and floor are then
    applied to that base. Score-0 rows (hard blocks, scoring errors)
    are left alone. The batch files themselves are never rewritten.
    Returns the number of postings re-tiered.
    """
    count = 0
    for job in jobs:
        if all(field in job for field in FIL09_TIER_FIELDS):
            continue
        score = job.get("score", 0) or 0
        if score <= 0:
            continue
        old_penalty = job.get("location_penalty", 0) or 0
        before = score
        apply_location_tier(job, base_score=score - old_penalty)
        count += 1
        if job["score"] != before:
            print(f"[Queue] re-tiered pre-FIL-09 posting: {job.get('title', '')} at "
                  f"{job.get('company', '')} ({job.get('location', '')!r}) "
                  f"{before} → {job['score']} ({job['location_reason']})")
    return count


def content_keys():
    """{content key: [(host, id), ...]} for everything already queued.

    A fetch run seeds its dedup index with this, so a posting queued by an
    earlier fetch run in the same digest cycle is not fetched, scored and
    queued a second time. The board idents come with it, so the same-board
    guard applies to queued postings exactly as it does to persisted ones.
    """
    _, jobs, _ = load_pending()
    index = {}
    for job in jobs:
        key = content_key(job)
        if not key:
            continue
        ident = board_id(normalize_url(job.get("apply_link", "")))
        idents = index.setdefault(key, [])
        if ident and ident not in idents:
            idents.append(ident)
    return index


def clear(paths):
    """Remove the batches a digest has sent."""
    for path in paths:
        try:
            os.remove(path)
        except FileNotFoundError:
            pass
