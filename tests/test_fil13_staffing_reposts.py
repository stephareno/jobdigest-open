"""FIL-13: staffing reposts and cross-name duplicates (folds in FIL-12).

(a) FIL-10's same-board guard applies to employer boards only. On LinkedIn and
    the aggregators a posting id is a listing, so a staffing firm reposting one
    opening under fresh ids is one job; on an ATS host, a company domain or
    usajobs.gov two ids are still two requisitions (MITRE). A dropped repeat
    re-stamps its content key's retention timestamp (FIL-12).
(b) After scoring, the digest shows one card per hiring company + title, within
    the batch and against recent Sheet rows. Every row is still written to the
    Sheet; only the email collapses.

Links are the real ones from the Oct 8, 11 and 13 digests. No network access.
"""

import json
from datetime import date, datetime, timedelta

import pytest

# First: it sets the env vars and module stubs everything below needs.
from test_fil10_cross_source_dedup import MITRE_A, MITRE_B, _job, pipeline  # noqa: F401  isort: skip

import emailer  # noqa: E402
import main  # noqa: E402
import pending  # noqa: E402
import state  # noqa: E402

CYBERCODERS = ("https://www.linkedin.com/jobs/view/4474403160",   # Oct 8 digest
               "https://www.linkedin.com/jobs/view/4475662643",   # Oct 13 digest
               "https://www.linkedin.com/jobs/view/4475631272")   # Oct 13 digest
SWOOPED = ("https://www.linkedin.com/jobs/view/4476524231",
           "https://www.linkedin.com/jobs/view/4477895776",
           "https://www.linkedin.com/jobs/view/4478088919")
WFP_LINKEDIN = "https://www.linkedin.com/jobs/view/4475480917"
NTEN_LINKEDIN = "https://www.linkedin.com/jobs/view/4478004357"
WFP_ATS = "https://job-boards.greenhouse.io/wfpusa/jobs/7012345"


def _cybercoders(url, score=6):
    return _job(url, "VP of IT", "CyberCoders", "LinkedIn", location="Worcester, MA",
                score=score)


# ------------------------------------------------------------ (a) the guard

@pytest.mark.parametrize("url_a, url_b, same", [
    (CYBERCODERS[0], CYBERCODERS[1], True),                    # LinkedIn listing ids
    ("https://www.adzuna.com/details/5866632840",
     "https://www.adzuna.com/details/5866632999", True),         # aggregator
    ("https://www.indeed.com/viewjob/jk-1234567",
     "https://www.indeed.com/viewjob/jk-7654321", True),         # job board via JSearch
    (MITRE_A, MITRE_B, False),                                 # company domain
    ("https://job-boards.greenhouse.io/dragos/jobs/5399442008",
     "https://job-boards.greenhouse.io/dragos/jobs/5399442999", False),  # ATS vendor
    ("https://www.usajobs.gov/job/887858300",
     "https://www.usajobs.gov/job/887858399", False),          # agency announcements
])
def test_guard_applies_on_employer_boards_only(url_a, url_b, same):
    assert state.same_posting([state.board_id(url_a)], url_b) is same


def test_a_staffing_repost_under_three_ids_costs_one_scoring_call(pipeline):
    """CyberCoders "VP of IT, Worcester": three LinkedIn ids in five days."""
    calls, catalog = pipeline
    catalog["linkedin_email"] = [_cybercoders(CYBERCODERS[0])]
    main.fetch_run(shard=None)
    main.digest_run()
    catalog["linkedin_email"] = [_cybercoders(CYBERCODERS[1]), _cybercoders(CYBERCODERS[2])]
    main.fetch_run(shard=None)
    main.digest_run()
    assert calls["scored"] == ["CyberCoders|VP of IT"]
    assert len(calls["sheet"]) == 1


def test_reposts_queued_by_two_fetch_runs_merge_in_the_digest(pipeline):
    _calls, _catalog = pipeline
    queued = [{**_cybercoders(u), "found_at": f"2026-10-1{i}"} for i, u in enumerate(CYBERCODERS)]
    assert len(pending.dedupe(queued)) == 1


def test_two_requisitions_on_an_employer_board_still_stay_two(pipeline):
    calls, catalog = pipeline
    catalog["ats"] = [_job("https://job-boards.greenhouse.io/dragos/jobs/5399442008",
                           "Director, IT", "Dragos", "ATS", location="Hanover, MD"),
                      _job("https://job-boards.greenhouse.io/dragos/jobs/5399442999",
                           "Director, IT", "Dragos", "ATS", location="Hanover, MD")]
    main.fetch_run(shard=None)
    assert len(calls["scored"]) == 2


# ------------------------------------------------------------ FIL-12 re-stamp

def test_a_dropped_repeat_restamps_its_content_key(pipeline):
    """Swooped reposts every few days. Without the re-stamp the key ages out 60
    days after the first sighting and the next repost is scored and sent again."""
    calls, catalog = pipeline
    key = state.content_key({"company": "Swooped", "title": "Director, Corporate IT",
                             "location": "United States"})
    old = (datetime.utcnow() - timedelta(days=state.RETENTION_DAYS - 2)).isoformat()
    data = json.loads(open(state.STATE_FILE).read())
    data[state.content_hash(key)] = {"ts": old, "boards": ["www.linkedin.com|4476524231"]}
    open(state.STATE_FILE, "w").write(json.dumps(data))

    catalog["linkedin_email"] = [_job(SWOOPED[1], "Director, Corporate IT", "Swooped", "LinkedIn",
                                location="United States")]
    main.fetch_run(shard=None)
    assert calls["scored"] == []

    entry = json.loads(open(state.STATE_FILE).read())[state.content_hash(key)]
    assert entry["ts"] > old
    assert "www.linkedin.com|4477895776" in entry["boards"]
    # Five days on, the original sighting would have aged out; the re-stamp keeps it.
    state.purge_old_entries()
    assert state.content_hash(key) in state.load_content_index()


def test_a_deferred_posting_is_not_persisted_by_the_restamp(pipeline, monkeypatch):
    """A repeat matched only within this run must not write the key of a
    posting whose scoring was deferred (OPS-12), or the retry never happens."""
    calls, catalog = pipeline
    def overloaded(jobs, declined_feedback=None):
        for j in jobs:  # in place, as scorer.score_jobs does: fetch_run tracks by id()
            j.update(score=0, rationale="Scoring error", scoring_error="InternalServerError: 529")
        return jobs

    monkeypatch.setattr(main, "score_jobs", overloaded)
    catalog["linkedin_email"] = [_cybercoders(CYBERCODERS[1]), _cybercoders(CYBERCODERS[2])]
    main.fetch_run(shard=None)
    assert state.load_content_index() == {}


# ------------------------------------------------------------ (b) digest collapse

def _wfp(url, company, source="LinkedIn", location="Washington DC-Baltimore Area", score=8,
         title="Vice President, Technology, Innovation & AI"):
    return {**_job(url, title, company, source, location=location, score=score),
            "hiring_company": "World Food Program USA", "rationale": f"{company} read"}


def _run_digest(pipeline, monkeypatch, queued, recent=()):
    calls, _catalog = pipeline
    monkeypatch.setattr(main, "get_recent_index_rows", lambda days: list(recent))
    pending.write_batch(queued, "test", found_at=datetime.utcnow())
    main.digest_run()
    sections = calls["emails"][-1]
    return calls, sections["main"] + sections["worth_a_look"]


def test_the_same_opening_under_two_names_is_one_card(pipeline, monkeypatch):
    """WFP USA on LinkedIn and on NTEN's board: one card, both rows in the Sheet."""
    calls, cards = _run_digest(pipeline, monkeypatch, [
        _wfp(WFP_LINKEDIN, "World Food Program USA"),
        _wfp(NTEN_LINKEDIN, "NTEN", location="Maryland Junction, WV",
             title="Vice President, Tech, Innovation & AI"),
    ])
    assert len(calls["sheet"]) == 2
    assert len(cards) == 1
    assert [l["url"] for l in cards[0]["other_links"]] == [NTEN_LINKEDIN]


def test_the_direct_link_wins_and_the_highest_score_travels_with_it(pipeline, monkeypatch):
    """A thin ATS read scoring 4 must not drop the opening below the line the
    LinkedIn copy's 8 cleared."""
    _calls, cards = _run_digest(pipeline, monkeypatch, [
        _wfp(WFP_LINKEDIN, "World Food Program USA", score=8),
        _wfp(WFP_ATS, "World Food Program USA", source="ATS", score=4,
             location="Arlington, VA"),   # another place, so FIL-10 leaves both
    ])
    assert len(cards) == 1
    card = cards[0]
    assert card["apply_link"] == WFP_ATS
    assert (card["score"], card["rationale"]) == (8, "World Food Program USA read")
    assert card["location"] == "Washington DC-Baltimore Area"


def test_an_opening_emailed_in_the_last_14_days_is_left_out(pipeline, monkeypatch):
    sent = {"title": "VP of IT", "company": "CyberCoders", "hiring_company": "",
            "apply_link": CYBERCODERS[0], "source": "LinkedIn", "score": "6",
            "first_seen": date.today() - timedelta(days=5)}
    calls, cards = _run_digest(pipeline, monkeypatch, [
        {**_cybercoders(CYBERCODERS[1]), "location": "Boston, MA"},
        _wfp(WFP_LINKEDIN, "World Food Program USA")], recent=[sent])
    assert len(calls["sheet"]) == 2
    assert [c["company"] for c in cards] == ["World Food Program USA"]


def test_a_better_link_than_the_one_emailed_is_shown_again(pipeline, monkeypatch):
    sent = {"title": "Vice President, Technology, Innovation & AI", "company": "NTEN",
            "hiring_company": "", "apply_link": NTEN_LINKEDIN, "source": "LinkedIn",
            "score": 8, "first_seen": date.today() - timedelta(days=3)}
    sent_wfp = {**sent, "company": "World Food Program USA"}
    _calls, cards = _run_digest(pipeline, monkeypatch, [
        _wfp(WFP_ATS, "World Food Program USA", source="ATS")], recent=[sent_wfp])
    assert [c["apply_link"] for c in cards] == [WFP_ATS]
    assert cards[0]["other_links"][0]["sent"] == str(sent["first_seen"])


@pytest.mark.parametrize("age, score, same_url", [
    (15, 8, False),   # older than the lookback
    (2, 3, False),    # never emailed (below worth-a-look)
    (0, 8, True),     # this digest's own row, written by an earlier failed attempt
])
def test_sheet_rows_that_do_not_suppress_a_card(pipeline, monkeypatch, age, score, same_url):
    url = CYBERCODERS[1]
    sent = {"title": "VP of IT", "company": "CyberCoders", "hiring_company": "",
            "apply_link": url if same_url else CYBERCODERS[0], "source": "LinkedIn",
            "score": score, "first_seen": date.today() - timedelta(days=age)}
    _calls, cards = _run_digest(pipeline, monkeypatch, [_cybercoders(url)], recent=[sent])
    assert len(cards) == 1


def test_a_score_0_copy_never_joins_a_group(pipeline, monkeypatch):
    calls, cards = _run_digest(pipeline, monkeypatch, [
        _wfp(WFP_LINKEDIN, "World Food Program USA", score=8),
        _wfp(WFP_ATS, "World Food Program USA", source="ATS", score=0, location="Rome, Italy"),
    ])
    assert [c["apply_link"] for c in cards] == [WFP_LINKEDIN]
    assert len(calls["disq"]) == 1


def test_collapse_never_modifies_the_rows_written_to_the_sheet(pipeline):
    jobs = [_wfp(WFP_LINKEDIN, "World Food Program USA", score=8),
            _wfp(WFP_ATS, "World Food Program USA", source="ATS", score=4)]
    before = json.dumps(jobs, sort_keys=True)
    pending.collapse_hiring(jobs)
    assert json.dumps(jobs, sort_keys=True) == before


def test_the_card_renders_every_collapsed_link():
    card = {**_wfp(WFP_ATS, "World Food Program USA", source="ATS"),
            "linkedin_url": WFP_LINKEDIN,
            "other_links": [{"url": WFP_LINKEDIN, "source": "LinkedIn",
                             "company": "World Food Program USA", "sent": ""},
                            {"url": NTEN_LINKEDIN, "source": "LinkedIn", "company": "NTEN",
                             "sent": "2026-10-08"}]}
    html = emailer._job_card(card)
    assert html.count(WFP_LINKEDIN) == 1        # DEL-04's button, not twice
    assert NTEN_LINKEDIN in html
    assert "LinkedIn · NTEN (sent 2026-10-08)" in html


def test_two_requisitions_on_an_employer_board_stay_two_cards(pipeline, monkeypatch):
    """MITRE: same company and title, two ids on its own careers site."""
    _calls, cards = _run_digest(pipeline, monkeypatch, [
        _job(MITRE_A, "Director, Information Technology", "MITRE", "JSearch",
             location="McLean, Virginia"),
        _job(MITRE_B, "Director of IT", "MITRE", "JSearch", location="Bedford, MA"),
    ])
    assert sorted(c["apply_link"] for c in cards) == sorted([MITRE_A, MITRE_B])


AXON = "https://job-boards.greenhouse.io/axon/jobs/{}"


def test_one_title_at_several_requisitions_is_never_hidden_by_an_earlier_one(pipeline, monkeypatch):
    """Axon posted one title as a Greenhouse requisition per city. The Sterling,
    VA one (score 7) must not be hidden behind the Scottsdale one sent earlier."""
    sent = {"title": "Senior Director, Enterprise Applications", "company": "Axon",
            "hiring_company": "", "apply_link": AXON.format(7981860003), "source": "Startup",
            "score": 6, "first_seen": date.today() - timedelta(days=7)}
    _calls, cards = _run_digest(pipeline, monkeypatch, [
        _job(AXON.format(7981858003), "Senior Director, Enterprise Applications", "Axon",
             "Startup", location="Sterling, Virginia", score=7)], recent=[sent])
    assert [c["apply_link"] for c in cards] == [AXON.format(7981858003)]


def test_a_higher_score_than_anything_sent_is_shown_again(pipeline, monkeypatch):
    """NASA Deputy CIO: Washington emailed at 6, Greenbelt arrives at 9 (LinkedIn both)."""
    sent = {"title": "Deputy Chief Information Officer",
            "company": "NASA - National Aeronautics and Space Administration",
            "hiring_company": "", "apply_link": "https://www.linkedin.com/jobs/view/4473611500",
            "source": "LinkedIn", "score": 6, "first_seen": date.today() - timedelta(days=2)}
    greenbelt = "https://www.linkedin.com/jobs/view/4473620176"
    _calls, cards = _run_digest(pipeline, monkeypatch, [
        _job(greenbelt, "Deputy Chief Information Officer",
             "NASA - National Aeronautics and Space Administration", "LinkedIn",
             location="Greenbelt, MD", score=9)], recent=[sent])
    assert [c["apply_link"] for c in cards] == [greenbelt]
    assert cards[0]["other_links"][0]["sent"]


# ------------------------------------------------------------ employer in Company

ACME = "Acme Robotics"


def _for_client(job, client=ACME):
    return {**job, "hiring_company": client}


@pytest.mark.parametrize("hiring, expected", [
    (ACME, "Acme Robotics (via CyberCoders)"),
    ("", "CyberCoders"),                                   # client not named
    ("CyberCoders, Inc.", "CyberCoders"),                  # the poster is the employer
    ("a hospital system in Washington DC", "CyberCoders"), # a description, not a name
    ("Confidential", "CyberCoders"),
    ("CYBERCODERS (US)", "CyberCoders"),                   # bracketed aside, same firm
])
def test_company_names_the_employer_via_the_poster(hiring, expected):
    job = {**_cybercoders(CYBERCODERS[0]), "hiring_company": hiring}
    before = state.content_key(job)
    state.name_employer(job)
    assert job["company"] == expected
    assert job["posted_company"] == "CyberCoders"
    assert state.content_key(job) == before          # FIL-10 still keys on the poster
    state.name_employer(job)                         # idempotent
    assert job["company"] == expected


def test_a_sheet_rows_via_tail_keys_on_the_poster():
    row = {"company": "Acme Robotics (via CyberCoders)", "title": "VP of IT",
           "location": "Worcester, MA"}
    assert state.content_key(row) == state.content_key({**row, "company": "CyberCoders"})
    assert state.hiring_key(row) == state.hiring_key({**row, "company": ACME})
    assert state.company_names(row["company"]) == [ACME, "CyberCoders"]
    assert state.company_names(ACME) == [ACME]


def test_the_sheet_row_carries_the_employer(pipeline):
    calls, catalog = pipeline
    catalog["linkedin_email"] = [_for_client(_cybercoders(CYBERCODERS[0]))]
    main.fetch_run(shard=None)
    main.digest_run()
    assert [j["company"] for j in calls["sheet"]] == ["Acme Robotics (via CyberCoders)"]


def test_a_rejection_from_the_staffing_firm_reaches_the_row():
    from sources import status_email
    row = {"row": 1700, "company": "Acme Robotics (via CyberCoders)", "title": "VP of IT",
           "status": "Applied"}
    other = {"row": 1701, "company": "Globex (via CyberCoders)", "title": "Director, IT",
             "status": "Applied"}
    text = status_email._normalize(
        "Hi Sam, thank you for your application for the VP of IT role with CyberCoders. "
        "Unfortunately we have decided to move forward with other candidates.")
    # The poster's name makes both CyberCoders rows candidates (TRK-11: Claude
    # picks the row); the title puts this one first and passes the Rejected guard.
    jobs = status_email.candidate_jobs(text, [row, other])
    assert [j["row"] for j in jobs] == [row, other]
    result = {"status": status_email.STATUS_REJECTED, "job": jobs[0], "confidence": "high", "reason": ""}
    assert status_email.decide(result, text) == ([row], None)
    # The client's own name still matches too.
    text = status_email._normalize("Your application for VP of IT at Acme Robotics was received.")
    assert [j["row"] for j in status_email.candidate_jobs(text, [row, other])] == [row]


def test_the_applied_seed_matches_either_name():
    from sources import applied_doc
    rows = [{"row": 1700, "company": "Acme Robotics (via CyberCoders)", "title": "VP of IT",
             "status": "New"}]
    assert applied_doc._find_row(rows, "CyberCoders", "VP of IT") is rows[0]
    assert applied_doc._find_row(rows, "Acme Robotics", "VP of IT") is rows[0]


def test_the_nten_copy_names_wfp_and_collapses_under_its_card(pipeline, monkeypatch):
    """Oct 11: WFP USA's VP Technology on its own listing and on NTEN's."""
    calls, cards = _run_digest(pipeline, monkeypatch, [
        _wfp(WFP_LINKEDIN, "World Food Program USA"),
        _wfp(NTEN_LINKEDIN, "NTEN", location="Maryland Junction, WV",
             title="Vice President, Tech, Innovation & AI"),
    ])
    assert sorted(j["company"] for j in calls["sheet"]) == [
        "World Food Program USA", "World Food Program USA (via NTEN)"]
    assert [c["company"] for c in cards] == ["World Food Program USA"]
    assert "LinkedIn · via NTEN" in emailer._job_card(cards[0])


def test_a_poster_with_brackets_of_its_own_still_parses():
    company = "Department of Veterans Affairs, Office of Information and Technology (via Grant Thornton (US))"
    assert state.employer_name(company) == "Department of Veterans Affairs, Office of Information and Technology"
    assert state.poster_name(company) == "Grant Thornton (US)"
    job = {"company": "Grant Thornton (US)", "hiring_company": "Grant Thornton", "title": "t"}
    assert not state.name_employer(job) and job["company"] == "Grant Thornton (US)"
