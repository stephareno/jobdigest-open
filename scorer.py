# scorer.py — scores jobs using Claude API

import re
import time
import llm
from llm import ApiAccessError, is_transient
from config import FUNCTION_FIT_VALUES, function_fit_value
from location import apply_location_tier
from prompt_template import render_file
import profile_loader
from sheet_dates import newest_first_key

# Rationale stored when the scoring call itself fails (API down, out of credit,
# unparseable reply). The job lands in Disqualified as score 0; SCO-07's
# tools/rescore_scoring_errors.py finds rows by this exact text.
SCORING_ERROR = "Scoring error"

# DEL-13: how much JD text the scoring call sees. Scope detail sits deep in the
# responsibilities list, past the old 3,000-character cut.
JD_CHARS = 8000
# Room for the three-line rationale plus the other fields, so the JSON never truncates.
MAX_TOKENS = 1000

# DEL-13: the digest blurb's parts, in order: (JSON key, label). Only the
# culture part carries a label.
BLURB_PARTS = (("company", ""), ("role", ""), ("culture", "Culture:"))


def build_system_prompt(profile):
    """PUB-02: the scoring prompt, prompts/scoring_system.txt rendered from the
    profile. The prompt's prose and output format live in the template; every
    personal value (who, which sectors, how many points) comes from profile.yaml."""
    return render_file("scoring_system.txt", profile).rstrip("\n")


# Built once at import from profile.yaml, so an invalid profile or template
# stops the run at startup instead of mid-batch, and every caller that reads
# scorer.SYSTEM_PROMPT (tools/rescore_dry_run.py, the tests) keeps working.
PROFILE = profile_loader.active()
SYSTEM_PROMPT = build_system_prompt(PROFILE)
# PUB-04: the scoring model (scoring.model); the digest blurb rewrite and,
# unless scoring.summary_model is set, the weekly summary use it too.
MODEL = PROFILE["scoring"]["model"]

# PUB-12: scoring calls answered and the tokens they used (the API's usage
# fields), for calibrate's cost lines. Never reset by the pipeline.
# REV-06: "input" is uncached input; cache writes and reads are counted apart.
USAGE = {"calls": 0, "input": 0, "output": 0, "cache_write": 0, "cache_read": 0}

# DEL-15: a bare domain, at least one dot, letters / digits / hyphens.
_DOMAIN_RE = re.compile(r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)+")


def score_jobs(jobs, declined_feedback=None):
    feedback_block = _format_feedback(declined_feedback or [])
    scored = []
    total = len(jobs)
    for i, job in enumerate(jobs):
        print(f"[Scorer] {i+1}/{total}: {job.get('title', '')} at {job.get('company', '')}")
        blocked = _is_hard_disqualify(job)
        if blocked:
            job["score"] = 0
            job["rationale"] = f"Auto-disqualified: {blocked} industry"
            job["hard_block_industry"] = blocked
            job["is_cool_company_wrong_role"] = False
            scored.append(job)
            continue
        result = _score_single(job, feedback_block)
        job.update(result)
        # work_model above is often more accurate than what the ATS parser gave
        # main.py (FIL-08): most ATS APIs never expose a hybrid/onsite signal,
        # so the pre-scoring work_model is frequently blank, and needs_relo()/
        # location_penalty() computed from it in main.py stay wrong even after
        # Claude reads the actual JD and corrects work_model right above. Recompute
        # both here, now that work_model is as good as it will get, before the
        # penalty is applied and before the digest/Sheet read job["relo"].
        # loc_city / loc_state from the same response are the fallback when the
        # posting's own location string can't be placed (FIL-09). The tier
        # penalty applies to every hybrid/onsite job outside the commutable
        # band — no company or base-score exemption (FIL-11) — and never takes
        # a score below 1; a 0 stays 0. A hybrid/onsite job resolving within the
        # profile's home_bonus_radius_miles of the home metro then takes the
        # home bonus (GAP-07), capped at max_score.
        apply_location_tier(job, base_score=job["score"])
        scored.append(job)
        time.sleep(0.5)
    return scored


def cap_calls(jobs, cap, dream_names=()):
    """PUB-12: (to score, held) under scoring.max_calls_per_run. Dream-company
    postings come first, so the cap falls on the long tail. A posting the
    hard-block pre-filter scores 0 costs no call and is never held; past the
    cap the rest are held, in their order, for the next run."""
    dreams = {n.casefold() for n in dream_names}

    def dream(job):
        return bool(job.get("is_dream_company")) or str(job.get("company", "")).casefold() in dreams
    ordered = sorted(jobs, key=lambda j: not dream(j))
    keep, held, calls = [], [], 0
    for job in ordered:
        if _is_hard_disqualify(job):
            keep.append(job)
        elif cap is None or calls < cap:
            keep.append(job)
            calls += 1
        else:
            held.append(job)
    return keep, held


def _format_feedback(feedback):
    if not feedback:
        return ""
    name = PROFILE["candidate"]["short_name"]
    lines = [f"DECLINED ROLES — {name} passed on these; factor patterns into scoring:"]
    for f in feedback[:20]:  # cap at 20 to avoid bloating the prompt
        note = f.get("notes", "")
        company = f.get("company", "")
        title = f.get("title", "")
        lines.append(f"  - {company} / {title}: {note}")
    return "\n".join(lines)


def checked_score(result, max_score=None):
    """REV-05: the reply with its base score checked at parse time. The score
    must be an integer in [0, scoring.max_score]; a float with no fraction
    (10.0) is read as that integer. A bool, a string ("8"), null, a missing
    score, a fraction or a number out of range is a malformed reply: raises
    ValueError naming the raw value, so llm.complete_json asks once more and
    a second bad reply is recorded as a scoring error, never with the bad
    number. Never clamps: 99 is a bad reply, not a 10. `score` is the only
    number the reply carries; every modifier is computed in code."""
    top = _max_score() if max_score is None else max_score
    raw = result.get("score") if isinstance(result, dict) else None
    value = raw
    if isinstance(raw, float) and raw.is_integer():
        value = int(raw)
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= top:
        raise ValueError(f"score {raw!r} is not an integer from 0 to {top}")
    return {**result, "score": value}


def _max_score():
    return profile_loader.active()["scoring"]["max_score"]


def _score_single(job, feedback_block=""):
    jd = f"""
Title: {job.get('title', '')}
Company: {job.get('company', '')}
Location: {job.get('location', '')}
Work Model: {job.get('work_model', '')}
Comp: {job.get('comp', 'Not listed')}
Description: {job.get('description', 'None')[:JD_CHARS]}
"""
    if job.get("title_only"):
        # REV-08: a LinkedIn alert with no page fetched (fetch_pages false).
        jd = (f"\n{TITLE_ONLY_LINE}\n" + jd)
    user_content = f"Score this job:\n{jd}"
    if feedback_block:
        user_content = f"{feedback_block}\n\n{user_content}"
    try:
        result, _ = llm.complete_json(
            MODEL, SYSTEM_PROMPT, [{"role": "user", "content": user_content}], MAX_TOKENS,
            purpose="scoring call", on_usage=_count_usage, validate=checked_score,
            cache_system=True)                       # REV-06: the same prompt on every call
        return {
            "score": result["score"],                  # REV-05: checked by checked_score
            "rationale": format_rationale(result.get("rationale")),
            # PUB-04: one of industries.hard_block, or None (was is_insurance)
            "hard_block_industry": _hard_block_industry(result, job),
            "is_cool_company_wrong_role": result.get("is_cool_company_wrong_role", False),
            "work_model": result.get("work_model", job.get("work_model", "Unknown")),
            # FIL-09: fallback for locations the posting string can't place
            "loc_city": result.get("city") or "",
            "loc_state": result.get("state") or "",
            # SCO-06: the client behind a staffing-firm posting; stored on the
            # job, not rendered in the digest
            "hiring_company": result.get("hiring_company") or "",
            # DEL-15: the profile step's last way to a company site, used only
            # after a homepage name check; never read by the score
            "website": website_domain(result.get("website")),
            # SCO-09: anything but leads_department caps the score (location.apply_location_tier)
            "function_fit": _function_fit(result, job),
            # PUB-09: the scoring.role_criteria ids the call found in the posting
            "criteria_hits": criteria_hits(result, job),
            # PUB-19: the sentence quoted for each reported dealbreaker; the
            # code checks it (location.verify_dealbreaker) before applying it
            "dealbreaker_evidence": dealbreaker_evidence(result),
        }
    except ApiAccessError:
        # OPS-12: billing/auth/permission fails every call the same way. Scoring
        # on would record every posting at 0 and mark it seen; abort instead.
        raise
    except Exception as e:
        # A malformed reply (REV-05: including a bad score, raw value named)
        # after its one retry lands here and is recorded as a scoring error.
        print(f"[Scorer] Error: {e} — recorded as '{SCORING_ERROR}' "
              f"({job.get('company', '')} / {str(job.get('title', ''))[:60]})")
        result = {"score": 0, "rationale": SCORING_ERROR, "hard_block_industry": None,
                  "is_cool_company_wrong_role": False}
        if is_transient(e):
            # main.fetch_run defers the posting to a later run instead of
            # recording this 0, up to MAX_SCORING_DEFERRALS times (OPS-12).
            result["scoring_error"] = " ".join(f"{type(e).__name__}: {e}".split())[:200]
        return result


def _count_usage(usage):
    USAGE["calls"] += 1
    USAGE["input"] += usage.input_tokens
    USAGE["output"] += usage.output_tokens
    USAGE["cache_write"] = USAGE.get("cache_write", 0) + (getattr(usage, "cache_creation_input_tokens", 0) or 0)
    USAGE["cache_read"] = USAGE.get("cache_read", 0) + (getattr(usage, "cache_read_input_tokens", 0) or 0)


def usage_line(usage=None, model=None):
    """REV-06: one line of scoring calls, cached vs uncached input and cost."""
    from config import format_cost, model_cost
    u = usage or USAGE
    model = model or MODEL
    cost = model_cost(model, u["input"], u["output"], u.get("cache_write", 0), u.get("cache_read", 0))
    return (f"{u['calls']} scoring calls at {model}: input {u['input'] + u.get('cache_write', 0) + u.get('cache_read', 0):,} "
            f"tokens (uncached {u['input']:,}, cache write {u.get('cache_write', 0):,}, "
            f"cache read {u.get('cache_read', 0):,}), output {u['output']:,}; cost {format_cost(cost)}")


# The reply parser every JSON call shares (PUB-14: llm.parse_json).
_parse_reply = llm.parse_json


def website_domain(value):
    """DEL-15: the reply's website as a bare lower-case domain, or "" when it
    is missing, null or not a domain. A scheme, "www." or a path is dropped."""
    text = str(value or "").strip().lower()
    if text in ("", "null", "none", "n/a"):
        return ""
    text = re.sub(r"^[a-z]+://", "", text).split("/")[0].split("?")[0].split(":")[0]
    text = text.removeprefix("www.").rstrip(".")
    return text if _DOMAIN_RE.fullmatch(text) else ""


def format_rationale(value):
    """DEL-13: the reply's rationale object as the one-line blurb the card
    shows: "<company> <role> Culture: <keywords>". A missing part is left out
    and logged; a plain string (an older prompt) is kept as it is."""
    if not isinstance(value, dict):
        return " ".join(str(value or "").split())
    parts = []
    for key, label in BLURB_PARTS:
        text = " ".join(str(value.get(key) or "").split())
        if label and text.lower().startswith(label.lower()):
            text = text[len(label):].strip()
        if text:
            # parts run together on one line: close each so they never read as one
            if text[-1] not in ".!?":
                text += "."
            parts.append(f"{label} {text}".strip())
        else:
            print(f"[Scorer] rationale missing its {key} part")
    return " ".join(parts)


def criteria_hits(result, job, criteria=None):
    """PUB-09: the reply's criteria_hits as a list of scoring.role_criteria
    ids, in profile order. An id the profile does not define is logged and
    dropped; a missing or malformed field is an empty list."""
    known = [c["id"] for c in (criteria or PROFILE["scoring"]["role_criteria"])]
    value = result.get("criteria_hits")
    if isinstance(value, str):
        value = [v for v in re.split(r"[,\s|]+", value) if v]
    if not isinstance(value, (list, tuple)):
        return []
    hits = {str(v).strip().strip("[]").lower() for v in value if str(v).strip()}
    unknown = sorted(hits - set(known))
    if unknown:
        print(f"[Scorer] criteria_hits {unknown} not in scoring.role_criteria for "
              f"{job.get('title', '')} at {job.get('company', '')}: dropped")
    return [cid for cid in known if cid in hits]


def dealbreaker_evidence(result, criteria=None):
    """PUB-19: the reply's dealbreaker_evidence as {dealbreaker id: quoted
    sentence}, for the profile's dealbreaker criteria only. Anything else
    (a points criterion, an unknown id, a non-string quote) is dropped; a
    missing or malformed field is an empty dict."""
    deals = {c["id"] for c in (criteria or PROFILE["scoring"]["role_criteria"]) if c.get("dealbreaker")}
    value = result.get("dealbreaker_evidence")
    if not isinstance(value, dict):
        return {}
    out = {}
    for key, quote in value.items():
        cid = str(key).strip().lower()
        if cid in deals and isinstance(quote, str) and quote.strip():
            out[cid] = " ".join(quote.split())
    return out


# REV-08: the line a title-only posting's scoring call carries.
TITLE_ONLY_LINE = ('Description unavailable: judge from the title and company only. '
                   'function_fit may be "unknown".')


def _function_fit(result, job):
    """The reply's function_fit, normalised. A missing or unknown value is
    logged and kept as "unknown", which the SCO-09 gate caps like any value
    other than leads_department. A pre-PUB-10 name (it_leadership...) is
    mapped to its new one. REV-08: a title-only posting is always "unknown":
    the department is judged from the description, which it does not have,
    and location.apply_location_tier does not cap it."""
    if job.get("title_only"):
        return "unknown"
    fit = function_fit_value(result.get("function_fit") or "")
    if fit in FUNCTION_FIT_VALUES:
        return fit
    print(f"[Scorer] function_fit missing or invalid ({result.get('function_fit')!r}) for "
          f"{job.get('title', '')} at {job.get('company', '')}: treated as not leading the department")
    return "unknown"


def _hard_block_industry(result, job):
    """The reply's hard_block_industry: the industries.hard_block entry it
    names, or None. A value outside the list is logged and kept, so the role
    still stays out of the digest."""
    value = result.get("hard_block_industry")
    text = str(value or "").strip()
    if text.lower() in ("", "null", "none", "n/a", "false"):
        return None
    for name in PROFILE["industries"]["hard_block"]:
        if text.casefold() == name.casefold():
            return name
    print(f"[Scorer] hard_block_industry {value!r} is not in industries.hard_block for "
          f"{job.get('title', '')} at {job.get('company', '')}: kept as a hard block")
    return text


def is_disqualified(job):
    """PUB-09: a hard block, or a modifier set to "disqualify" fired
    (job["dealbreaker"]). Never emailed; score 0, Disqualified tab."""
    return is_hard_blocked(job) or bool(job.get("dealbreaker"))


def is_hard_blocked(job):
    """True when the role is in a hard-block industry: the scoring call or the
    name pre-filter set hard_block_industry, or (a row scored before PUB-04)
    is_insurance. Such a role is never emailed and goes to the Disqualified tab."""
    return bool(job.get("hard_block_industry") or job.get("is_insurance"))


def name_block(hard_block):
    """industries.hard_block (tier entry -> name keywords) as (pattern,
    {keyword: tier entry}). Whole-word match, so "crypto" does not fire on
    "cryptography" and "vape" not on "vaporware"."""
    tiers = {k.casefold(): tier for tier, words in hard_block.items() for k in words}
    pattern = re.compile(r"\b(" + "|".join(re.escape(k) for k in tiers) + r")\b", re.I)
    return pattern, tiers


_NAME_BLOCK = name_block(PROFILE["industries"]["hard_block"])


def _is_hard_disqualify(job, block=None):
    """Cheap pre-filter on company name and title only.

    It deliberately does NOT read the description: matching there meant any
    posting listing "health insurance" among its benefits was scored 0 and
    routed to Disqualified before Claude ever saw it. Industry judgement from
    the JD body belongs to the model, which has the context to tell an insurer
    from an employer that offers insurance.

    Returns the industries.hard_block tier entry the matched word belongs to
    ("gambling/casinos" for "casino"), the same value the scoring call gives,
    or None.
    """
    pattern, tiers = block or _NAME_BLOCK
    text = f"{job.get('title', '')} {job.get('company', '')}"
    m = pattern.search(text)
    return tiers[m.group(1).casefold()] if m else None


def _digest_order(job):
    return (-job.get("score", 0), newest_first_key(job))


def filter_jobs(scored_jobs, thresholds=None):
    """Digest sections by final score; thresholds is scoring.thresholds
    (the committed profile's unless given)."""
    t = thresholds or PROFILE["scoring"]["thresholds"]
    main, worth_a_look, cool_wrong_role = [], [], []

    for job in scored_jobs:
        score = job.get("score", 0)
        if is_disqualified(job):
            continue
        if job.get("is_cool_company_wrong_role") and score < t["worth_a_look"]:
            cool_wrong_role.append(job)
            continue
        # SCO-09: a function-capped role is logged to the Sheet, never emailed
        # in main or worth_a_look -- the dream-company shortcut below included.
        if job.get("function_capped"):
            continue
        if job.get("is_dream_company") and score >= t["dream_company_main"]:
            main.append(job)
            continue
        if score >= t["main_digest"]:
            main.append(job)
        elif score >= t["worth_a_look"]:
            worth_a_look.append(job)

    # Score first, then newest posting first; undated roles after dated roles
    # of the same score (DEL-05, PRD §7).
    main.sort(key=_digest_order)
    worth_a_look.sort(key=_digest_order)

    return {
        "main": main,
        "worth_a_look": worth_a_look,
        "cool_wrong_role": cool_wrong_role,
        "all_scored": scored_jobs,
    }
