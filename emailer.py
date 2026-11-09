# emailer.py — builds HTML digest email and sends via Gmail

import html as html_lib
import os
import smtplib
from urllib.parse import urlparse
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from datetime import datetime
import profile_loader
from config import TZ_LABELS, source_display
from location import function_note, split_rationale
from sheet_dates import is_fresh, newest_first_key, posted_age_text
from state import employer_name, poster_name

# PUB-04: digest section thresholds and the cool-company cap (profile.yaml scoring).
_SCORING = profile_loader.active()["scoring"]
# PUB-05: where it goes and whose day it is (profile.yaml delivery).
RECIPIENT_EMAIL = profile_loader.active()["delivery"]["email_to"]
_TZ = profile_loader.local_tz()
_TZ_LABEL = TZ_LABELS.get(str(_TZ))

GMAIL_ADDRESS = os.environ["GMAIL_ADDRESS"].strip()
GMAIL_APP_PASSWORD = os.environ["GMAIL_APP_PASSWORD"].strip()


def send_digest(sections, is_first_run=False, is_fallback=False, failed_fetch_runs=None, notices=None):
    main = sections.get("main", [])
    worth_a_look = sections.get("worth_a_look", [])
    cool_wrong_role = sections.get("cool_wrong_role", [])
    fallback_jobs = sections.get("fallback_jobs", [])
    total = len(main) + len(worth_a_look)

    if is_fallback:
        subject = f"Job digest — no new matches · resurface list · {_today()}"
    else:
        subject = f"Job digest — {total} match{'es' if total != 1 else ''} · {_today()}"

    html = _build_html(main, worth_a_look, cool_wrong_role, fallback_jobs, is_first_run, is_fallback,
                       failed_fetch_runs=failed_fetch_runs, notices=notices)
    _send(subject, html)
    print(f"[Emailer] Sent: {subject}")


# REV-02: the subject and the one sentence of an alert-only email -- failed or
# cancelled fetch runs since the last digest and no new postings to send.
ALERT_ONLY_SUBJECT = "Job digest: fetch runs failed"
ALERT_ONLY_NOTE = ("No new postings were scored since the last digest, most likely because the "
                   "fetch runs above stopped before scoring anything (a scoring API key that is "
                   "out of credit or revoked stops every run).")
# REV-04: the same alert-only form when the problem is the Sheet (rows waiting
# in state/sheet_retry/, or a header row the writer refuses) on a day with
# nothing new.
SHEET_ALERT_SUBJECT = "Job digest: Sheet write failed"
BOTH_ALERT_SUBJECT = "Job digest: fetch runs and Sheet write failed"
SHEET_ALERT_NOTE = ("No new postings since the last digest; this email is about the Sheet problem "
                    "above, which keeps postings off the tracker until it is fixed.")
SHEET_NO_MATCHES_NOTE = ("New postings were scored since the last digest, but none cleared the digest "
                         "thresholds; this email is about the Sheet problem above.")
ALERT_NO_MATCHES_NOTE = ("New postings were scored since the last digest, but none cleared the digest "
                         "thresholds and there was nothing to resurface; the fetch runs above did not "
                         "score theirs.")


def send_alert_only(failed_fetch_runs, note=None, notices=None):
    """REV-02: the alert-only digest, sent when fetch runs failed and the queue
    is empty, whatever delivery.skip_empty_days says."""
    subject, html = build_alert_only(failed_fetch_runs, note=note, notices=notices)
    _send(subject, html)
    print(f"[Emailer] Sent: {subject}")


def build_alert_only(failed_fetch_runs, subject_prefix="", note=None, notices=None):
    """(subject, html) of the alert-only digest: the normal header, the
    banner (failed runs, then Sheet notices), one sentence, the footer. The
    subject names what failed (REV-04)."""
    if failed_fetch_runs:
        subject = BOTH_ALERT_SUBJECT if notices else ALERT_ONLY_SUBJECT
        note = note or ALERT_ONLY_NOTE
    else:
        subject, note = SHEET_ALERT_SUBJECT, note or SHEET_ALERT_NOTE
    body = (_fetch_failure_banner(failed_fetch_runs, notices)
            + f'<div style="font-size:14px;color:#555;margin:0 0 24px;">{_text(note)}</div>')
    return f"{subject_prefix}{subject}", _wrap(body)


def _send(subject, html_body):
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = GMAIL_ADDRESS
    msg["To"] = RECIPIENT_EMAIL
    msg.attach(MIMEText(html_body, "html"))
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD)
        server.sendmail(GMAIL_ADDRESS, RECIPIENT_EMAIL, msg.as_string())


def _today():
    return datetime.now(_TZ).strftime("%B %d, %Y")


# PUB-21: every value that comes from a posting or a model reply goes through
# _text before it is put into the HTML, and every link through _link: only an
# http(s) URL becomes an href (escaped as an attribute); anything else
# (javascript:, data:, a bare path) renders as the label alone, with no link.
def _text(value):
    """A posting or model value as HTML text: &, <, >, " and ' escaped."""
    return html_lib.escape("" if value is None else str(value), quote=True)


def _safe_href(url):
    """The URL escaped for an href attribute, or None unless it is http(s)
    with a host."""
    try:
        parsed = urlparse(str(url or "").strip())
    except ValueError:
        return None
    if parsed.scheme.lower() not in ("http", "https") or not parsed.netloc:
        return None
    return html_lib.escape(str(url).strip(), quote=True)


def _link(url, label_html, style):
    """<a href=...> for an http(s) URL; otherwise the label as plain text in a
    span with the same style. label_html is already escaped."""
    href = _safe_href(url)
    if href is None:
        return f'<span style="{style}">{label_html}</span>'
    return f'<a href="{href}" style="{style}">{label_html}</a>'


def _score_badge(score):
    if score >= 8:
        color, bg = "#1D9E75", "#E1F5EE"
    elif score >= 6:
        color, bg = "#185FA5", "#E6F1FB"
    else:
        color, bg = "#888780", "#F1EFE8"
    return f'<span style="display:inline-block;padding:2px 10px;border-radius:20px;background:{bg};color:{color};font-size:12px;font-weight:600;">{_text(score)}/10</span>'


def _warm_badge(warm_contact):
    if not warm_contact:
        return ""
    return f'<span style="display:inline-block;padding:2px 10px;border-radius:20px;background:#FAEEDA;color:#854F0B;font-size:12px;font-weight:600;margin-left:6px;">&#128100; {_text(warm_contact)}</span>'


def _source_badge(source):
    colors = {
        "Adzuna": ("#185FA5", "#E6F1FB"),
        "JSearch": ("#0F6E56", "#E1F5EE"),
        "LinkedIn": ("#185FA5", "#E6F1FB"),
        "Built In": ("#534AB7", "#EEEDFE"),
        "Wellfound": ("#993C1D", "#FAECE7"),
        "Watchlist": ("#3B6D11", "#EAF3DE"),
    }
    color, bg = colors.get(source, ("#888780", "#F1EFE8"))
    return f'<span style="display:inline-block;padding:2px 8px;border-radius:20px;background:{bg};color:{color};font-size:11px;">{_text(source)}</span>'


def _title_only_badge(job):
    """REV-08: grey 'title only' pill beside the score: scored from the LinkedIn
    alert alone, with no description (sources.linkedin_email.fetch_pages false)."""
    if not job.get("title_only"):
        return ""
    return ('<span style="display:inline-block;padding:2px 8px;border-radius:20px;background:#F1EFE8;'
            'color:#5F5E5A;font-size:11px;margin-left:6px;">title only</span>')


def _startup_badge(job):
    """Blue 'startup' pill — marks a role from the startup-tier ATS boards."""
    if job.get("tier") != "startup":
        return ""
    return ('<span style="background:#E8F0FE;color:#185FA5;font-size:11px;'
            'font-weight:600;padding:2px 7px;border-radius:10px;'
            'margin-left:6px;">startup</span>')


def _relo_badge(job):
    """Orange 'relo' pill — shown whenever the relo penalty applied."""
    if not job.get("relo"):
        return ""
    return ('<span style="display:inline-block;padding:2px 8px;border-radius:20px;'
            'background:#FAEEDA;color:#854F0B;font-size:11px;font-weight:600;'
            'margin-left:6px;">relo</span>')


def _dream_badge(job):
    """Purple 'dream' pill (PUB-16) for a role at a dream company: the profile's
    dream_companies file or a dream-tier company list."""
    if not job.get("is_dream_company"):
        return ""
    return ('<span style="display:inline-block;padding:2px 8px;border-radius:20px;'
            'background:#FBEAF0;color:#993556;font-size:11px;font-weight:600;'
            'margin-left:6px;">dream</span>')


def _fresh_badge(job):
    """Green 'fresh' pill: posted within config.FRESH_WINDOW_HOURS, or today or
    yesterday for date-only sources; never for NO_FRESH_FLAG_VENDORS (DEL-05)."""
    if not is_fresh(job):
        return ""
    return ('<span style="display:inline-block;padding:2px 8px;border-radius:20px;'
            'background:#E1F5EE;color:#0F6E56;font-size:11px;font-weight:600;'
            'margin-left:6px;">fresh</span>')


def _age_html(job):
    """ "posted 5h ago" / "posted yesterday" in grey, or nothing when the source
    has no reliable posted date."""
    text = posted_age_text(job)
    if not text:
        return ""
    return f'<span style="font-size:12px;color:#888;margin-left:8px;">{_text(text)}</span>'


def _linkedin_link(job, small=False):
    """A LinkedIn link only when a real posting URL is known (set in main.py
    from a canonical /jobs/view/<id> URL). No placeholder, never constructed."""
    url = job.get("linkedin_url", "")
    if not url or url == job.get("apply_link", ""):
        return ""
    if small:
        return _link(url, "LinkedIn &#8594;", "font-size:12px;color:#185FA5;text-decoration:none;margin-left:12px;")
    return _link(url, "LinkedIn", "display:inline-block;padding:6px 16px;border:1px solid #185FA5;"
                 "color:#185FA5;border-radius:6px;font-size:13px;text-decoration:none;font-weight:500;")


def _other_links(job, small=False):
    """FIL-13: the links of the copies collapsed into this card, after the
    LinkedIn link the way DEL-04 shows it. A copy posted for the same employer
    by someone else names the poster ("LinkedIn · via NTEN"), one for another
    name names it ("LinkedIn · NTEN"); one emailed before says when
    ("sent 2026-10-08")."""
    links = []
    for other in job.get("other_links") or []:
        url = other.get("url", "")
        if not url or url in (job.get("apply_link", ""), job.get("linkedin_url", "")):
            continue
        label = source_display(other.get("source")) or "Link"
        company = other.get("company", "")
        if employer_name(company).casefold() != employer_name(str(job.get("company", ""))).casefold():
            label += f" · {company}"
        elif poster_name(company).casefold() != poster_name(str(job.get("company", ""))).casefold():
            label += f" · via {poster_name(company)}"
        if other.get("sent"):
            label += f" (sent {other['sent']})"
        style = "font-size:12px;color:#185FA5;text-decoration:none;" + ("margin-left:12px;" if small else "")
        links.append(_link(url, f"{_text(label)} &#8594;", style))
    return "".join(links)


def _job_card(job, show_status=False):
    title = job.get("title", "Unknown Title")
    company = job.get("company", "Unknown Company")
    location = job.get("location", "")
    work_model = job.get("work_model", "")
    comp = job.get("comp", "")
    score = job.get("score", 0)
    # DEL-13: the italic line is the blurb alone; the comp, relo / DC and
    # function-cap notes stay in the Sheet text, not on the card.
    rationale = split_rationale(job.get("rationale", ""))[0]
    apply_link = job.get("apply_link", "#")
    source = source_display(job.get("source", ""))
    warm_contact = job.get("warm_contact", "")
    status = job.get("status", "")
    location_line = " · ".join(filter(None, [location, work_model, comp]))
    status_html = f'<span style="font-size:11px;color:#888;margin-left:8px;">({_text(status)})</span>' if show_status and status else ""
    apply_html = _link(apply_link, "Apply (ATS)", "display:inline-block;padding:6px 16px;background:#1a1a1a;"
                       "color:#ffffff;border-radius:6px;font-size:13px;text-decoration:none;font-weight:500;")

    return f"""
<div style="background:#ffffff;border:1px solid #e8e8e8;border-radius:10px;padding:16px 20px;margin-bottom:12px;">
  <div style="margin-bottom:6px;">{_score_badge(score)}{_title_only_badge(job)}{_dream_badge(job)}{_fresh_badge(job)}{_warm_badge(warm_contact)}{_relo_badge(job)}{_startup_badge(job)}{_age_html(job)}{status_html}</div>
  <div style="font-size:16px;font-weight:600;color:#1a1a1a;margin-bottom:2px;">{_text(title)}</div>
  <div style="font-size:14px;color:#444;margin-bottom:4px;">{_text(company)}</div>
  <div style="font-size:13px;color:#888;margin-bottom:8px;">{_text(location_line)}</div>
  <div style="font-size:13px;color:#555;font-style:italic;margin-bottom:10px;">{_text(rationale)}</div>
  <div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap;">
    {apply_html}
    {_linkedin_link(job)}{_other_links(job)}{_source_badge(source)}
  </div>
</div>"""


def _section_header(title, count=None, color="#1a1a1a"):
    count_str = f" ({count})" if count is not None else ""
    return f'<div style="margin:28px 0 12px;padding-bottom:8px;border-bottom:2px solid #f0f0f0;"><span style="font-size:13px;font-weight:700;color:{color};text-transform:uppercase;letter-spacing:0.06em;">{_text(title)}{count_str}</span></div>'


def _function_cap_html(job):
    """SCO-09: name the function cap on a cool-company card, as the rationale does elsewhere."""
    if not job.get("function_capped"):
        return ""
    return (f'<div style="font-size:12px;color:#888;font-style:italic;margin:-4px 0 8px;">'
            f'{_text(function_note(job.get("function_fit")))}</div>')


def _cool_rank(job):
    return (bool(job.get("is_dream_company")), job.get("score", 0))


def _cool_order(job):
    """Dream first, then score, then newest posting first (DEL-05)."""
    return (not job.get("is_dream_company"), -job.get("score", 0), newest_first_key(job))


def _best_row_per_company(jobs):
    """DEL-09: keep the best row per company (dream first, then score).
    Company names match on the employer (FIL-13: the via tail is ignored),
    case- and whitespace-insensitively; a row with no
    company name is kept on its own rather than merged with other blanks."""
    best = {}
    for i, job in enumerate(jobs):
        name = " ".join(employer_name(str(job.get("company") or "")).split()).casefold()
        key = name or f"__no_company_{i}"
        if key not in best or _cool_rank(job) > _cool_rank(best[key]):
            best[key] = job
    return list(best.values())


def _fetch_run_time_et(started_at):
    """'Tue Oct 6, 12:17 AM ET' from a GitHub ISO timestamp, in the profile's
    delivery.timezone (its short label, else the zone's abbreviation)."""
    dt = datetime.fromisoformat(started_at.replace("Z", "+00:00")).astimezone(_TZ)
    return f"{dt:%a %b} {dt.day}, {dt.hour % 12 or 12}:{dt:%M %p} {_TZ_LABEL or dt.tzname()}"


def _fetch_failure_cause(run):
    if run.get("error_title"):
        return run["error_title"]
    message = (run.get("error_message") or "").strip().splitlines()
    if message:
        return message[0][:160]
    return "no error message"


# REV-02: the banner line names each run's conclusion.
_FETCH_CONCLUSION_TEXT = {"failure": "failed", "cancelled": "was cancelled",
                          "timed_out": "timed out", "startup_failure": "failed to start"}


def _fetch_conclusion_text(run):
    conclusion = run.get("conclusion") or "unknown"
    return _FETCH_CONCLUSION_TEXT.get(conclusion, f"ended {conclusion}")


def _fetch_failure_banner(failed_fetch_runs, notices=None):
    """OPS-12: one red line per fetch run that failed since the last digest:
    start time, conclusion (REV-02), ::error:: title, link. REV-03: then one
    line per notice (plain text, escaped here), e.g. Sheet rows waiting in
    state/sheet_retry/."""
    if not failed_fetch_runs and not notices:
        return ""
    lines = "".join(
        f'<div style="font-size:13px;color:#791F1F;margin:4px 0;">'
        f'Fetch run {_text(_fetch_run_time_et(run["started_at"]))} {_text(_fetch_conclusion_text(run))}: '
        f'{_text(_fetch_failure_cause(run).rstrip(". "))}. '
        f'Postings from that run were not scored. '
        f'{_link(run.get("url"), "View run &#8594;", "color:#A32D2D;font-weight:600;")}</div>'
        for run in failed_fetch_runs or ())
    lines += "".join(f'<div style="font-size:13px;color:#791F1F;margin:4px 0;">{_text(note)}</div>'
                     for note in notices or ())
    return ('<div style="background:#FCEBEB;border:1px solid #F09595;border-radius:10px;'
            f'padding:12px 18px;margin-bottom:24px;">{lines}</div>')


def _build_html(main, worth_a_look, cool_wrong_role, fallback_jobs, is_first_run, is_fallback,
                failed_fetch_runs=None, notices=None):
    banner = _fetch_failure_banner(failed_fetch_runs, notices)
    body = banner

    if is_first_run:
        body += '<div style="background:#E1F5EE;border:1px solid #9FE1CB;border-radius:10px;padding:14px 18px;margin-bottom:24px;"><span style="font-size:13px;font-weight:600;color:#085041;">First run — showing top 10 from full backfill</span></div>'

    if is_fallback:
        body += '<div style="background:#FAEEDA;border:1px solid #FAC775;border-radius:10px;padding:14px 18px;margin-bottom:24px;"><span style="font-size:13px;font-weight:600;color:#633806;">No new matches today. Resurfacing roles you haven\'t acted on yet.</span></div>'
        for job in fallback_jobs[:15]:
            body += _job_card(job, show_status=True)
        return _wrap(body)

    if main:
        # DEL-08: "main" also carries dream/watchlist matches down to score 3
        # via scorer.filter_jobs's shortcut, so this header can't promise a
        # score floor the way "Worth a look" below can — each card's own score
        # badge is the only per-row score claim. SCO-09: never a function-capped
        # role; those reach the email only as "Cool company, wrong role".
        body += _section_header("Top matches", len(main), "#1D9E75")
        for job in main:
            body += _job_card(job)

    if worth_a_look:
        body += _section_header(f"Worth a look — score {_SCORING['thresholds']['worth_a_look']}–{_SCORING['thresholds']['main_digest'] - 1}", len(worth_a_look), "#185FA5")
        for job in worth_a_look:
            body += _job_card(job)

    if cool_wrong_role:
        # CLAUDE.md constraint #7 / PRD §7: email the top few only — dream
        # companies first, then by score. Every match is already in the Sheet.
        # DEL-09: one card per company, so a single company can't fill every
        # slot. This per-company rule is for this section only — "Top matches"
        # and "Worth a look" list every qualifying role, several per company
        # if that's what cleared the threshold. "hidden" counts everything not
        # shown, including other roles at a company that did get a card.
        shown = sorted(_best_row_per_company(cool_wrong_role),
                       key=_cool_order)[:_SCORING["cool_company_wrong_role_cap"]]
        hidden = len(cool_wrong_role) - len(shown)
        body += _section_header("Cool company, wrong role", len(shown), "#854F0B")
        if hidden:
            body += f'<div style="font-size:12px;color:#888;margin:-4px 0 10px;">{hidden} more logged to the Sheet only.</div>'
        for job in shown:
            body += f'''<div style="background:#FAFAF8;border:1px solid #e8e8e8;border-radius:10px;padding:14px 18px;margin-bottom:10px;">
  <div style="font-size:14px;font-weight:600;color:#1a1a1a;">{_text(job.get("company", ""))}{_dream_badge(job)}{_fresh_badge(job)}{_relo_badge(job)}{_startup_badge(job)}{_age_html(job)}</div>
  <div style="font-size:13px;color:#555;margin:2px 0 8px;">{_text(job.get("title", ""))}</div>{_function_cap_html(job)}
  {_link(job.get("apply_link"), "View role &#8594;", "font-size:12px;color:#185FA5;text-decoration:none;")}{_linkedin_link(job, small=True)}{_other_links(job, small=True)}
</div>'''

    if body == banner:
        body += '<div style="padding:32px;text-align:center;color:#888;font-size:14px;">No matches found today.</div>'

    return _wrap(body)


def _wrap(content):
    return f"""<!DOCTYPE html>
<html>
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="margin:0;padding:0;background:#f5f5f3;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;">
  <div style="max-width:640px;margin:0 auto;padding:24px 16px;">
    <div style="margin-bottom:24px;">
      <div style="font-size:11px;font-weight:600;color:#888;text-transform:uppercase;letter-spacing:0.08em;margin-bottom:4px;">Job Digest Agent</div>
      <div style="font-size:22px;font-weight:700;color:#1a1a1a;">{_today()}</div>
    </div>
    {content}
    <div style="margin-top:32px;padding-top:16px;border-top:1px solid #e8e8e8;font-size:11px;color:#aaa;text-align:center;">
      Job Digest Agent · {_text(RECIPIENT_EMAIL)} · All jobs logged in your Google Sheet
    </div>
  </div>
</body>
</html>"""
