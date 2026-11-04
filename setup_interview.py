# setup_interview.py — `python main.py setup`: write profile.yaml without an AI (PUB-06)
#
#   python main.py setup            # interview, write profile.yaml, check it, write the crons
#   python main.py setup --force    # the same, replacing an existing profile.yaml
#   python main.py setup --from-document resume.pdf   # PUB-10: proposed defaults from a resume or JD
#
# Plain numbered prompts on stdin/stdout: no curses, works over SSH. Every
# question shows its default in [brackets] (from profile.example.yaml); Enter
# keeps it. Lists are entered comma-separated. Metros are checked against
# data/us_places.tsv.gz as they are typed, with a retry on a miss.
#
# Sections the interview does not ask about (the title gate's finer lists,
# the other scoring point values, the source search queries) are copied from
# profile.example.yaml; profile.example.yaml explains each field. PUB-09 adds
# the role criteria (scoring.role_criteria), whether company appeal and
# mission/values are scored, the work-model outcomes and the floor's outcome.

import argparse
import os
import re
import subprocess
import sys
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml

import places
import profile_loader

ROOT = os.path.dirname(os.path.abspath(__file__))
EXAMPLE = os.path.join(ROOT, "profile.example.yaml")
PROFILE = os.path.join(ROOT, "profile.yaml")
DREAM_FILE = "data/dream_companies.txt"
NEXT_COMMAND = "python main.py doctor --phase 3"

HEADER = """# profile.yaml — written by `python main.py setup` (PUB-06)
#
# Who the job seeker is and what they want. Every field is explained in
# profile.example.yaml. Check this file with:  python main.py profile-check
# After changing delivery:, run:              python main.py cron --write

"""


class Interview:
    """The prompts, numbered in order. `ask` is input() unless a test passes
    its own; `say` is print()."""

    def __init__(self, ask=input, say=print):
        self._ask, self.say, self.n = ask, say, 0

    def _prompt(self, text, shown):
        self.n += 1
        return self._ask(f"{self.n}. {text}{f' [{shown}]' if shown != '' else ''}: ").strip()

    def text(self, question, default, check=None):
        while True:
            answer = self._prompt(question, default) or str(default)
            problem = check(answer) if check else None
            if not problem:
                return answer
            self.say(f"   {problem}")

    def number(self, question, default):
        return int(self.text(question, default, lambda a: None if a.isdigit() else "a whole number, please"))

    def yes(self, question, default):
        answer = self.text(question, "y" if default else "n",
                           lambda a: None if a.lower() in ("y", "yes", "n", "no") else "y or n, please")
        return answer.lower() in ("y", "yes")

    def items(self, question, default, check=None, allow_empty=True, metros=False):
        """A comma-separated list. Enter keeps the default; "-" means none.
        metros: entries are "City, ST", so a two-letter part joins the one before."""
        shown = ", ".join(default) if len(default) <= 6 else ", ".join(default[:6]) + f", ... ({len(default)} in all)"
        while True:
            answer = self._prompt(question + " (comma-separated" + (", - for none)" if allow_empty else ")"), shown)
            if not answer:
                return list(default)
            values = [] if answer == "-" else [v.strip() for v in answer.split(",") if v.strip()]
            if metros:
                values = _pair_states(values)
            if not values and not allow_empty:
                self.say("   at least one, please")
                continue
            problems = [p for p in (check(v) for v in values) if p] if check else []
            if not problems:
                return values
            for p in problems:
                self.say(f"   {p}")


    def lines(self, question, default):
        """One entry per prompt, for entries that may hold commas. Enter at the
        first prompt keeps the default; "-" there means none; afterwards an
        empty line ends the list."""
        self.say(f"   {question}: one per line, empty line to finish.")
        for entry in default:
            self.say(f"     [{entry}]")
        values = []
        while True:
            answer = self._prompt("next" if values else "first (Enter keeps the above, - for none)", "")
            if not answer:
                return values if values else list(default)
            if answer == "-" and not values:
                return []
            values.append(answer)

    def outcome(self, question, default, sign=None):
        """A modifier outcome (PUB-09): a whole number or "disqualify"."""
        def check(a):
            if a.lower() in ("disqualify", "d"):
                return None
            if not re.fullmatch(r"[+-]?\d+", a):
                return "a whole number such as -2, or disqualify"
            if sign == "penalty" and int(a) >= 0:
                return "a negative number such as -2, or disqualify"
            return None
        answer = self.text(question, default, check)
        return profile_loader.DISQUALIFY if answer.lower() in ("disqualify", "d") else int(answer)


def _criterion_id(text, taken):
    """A snake_case id from a criterion's first words, unique among taken."""
    words = re.findall(r"[a-z0-9]+", text.lower())[:3] or ["criterion"]
    base = "_".join(words)
    if not base[0].isalpha():
        base = "c_" + base
    cid, n = base, 2
    while cid in taken:
        cid, n = f"{base}_{n}", n + 1
    taken.add(cid)
    return cid


def role_criteria(ui, current):
    """PUB-09: scoring.role_criteria from two lists of one-line descriptions,
    each given a weight from 1 to 5. Enter keeps the current entries."""
    ui.say("   Describe, one line each, what you want the role to have; then what you want it "
           "not to have. Give each a weight from 1 to 5.")
    ui.say("   Judge the role itself: pay and location are asked about separately.")
    taken, out = set(), []
    scored = [c for c in current if not c.get("dealbreaker")]
    for sign, label in ((1, "What you want the role to have"), (-1, "What you want it not to have")):
        mine = [c for c in scored if (c["points"] > 0) == (sign > 0)]
        texts = ui.lines(label, [c["criterion"] for c in mine])
        for text in texts:
            kept = next((c for c in mine if c["criterion"] == text), None)
            if kept:
                taken.add(kept["id"])
                out.append(dict(kept))
                continue
            weight = int(ui.text(f"Weight for \"{text}\" (1-5)", 1 if sign < 0 else 2,
                                 lambda a: None if a.isdigit() and 1 <= int(a) <= 5 else "1 to 5, please"))
            out.append({"id": _criterion_id(text, taken), "points": sign * weight, "criterion": text})
    # PUB-10: dealbreakers are never scored; a posting the scoring call finds
    # one in goes to the Disqualified tab at score 0.
    deals = [c for c in current if c.get("dealbreaker")]
    for text in ui.lines("Dealbreakers: anything that rules a role out, whatever else it offers",
                         [c["criterion"] for c in deals]):
        kept = next((c for c in deals if c["criterion"] == text), None)
        if kept:
            taken.add(kept["id"])
        out.append(dict(kept) if kept else {"id": _criterion_id(text, taken), "dealbreaker": True,
                                             "criterion": text})
    return out


def _pair_states(parts):
    """["Denver", "CO", "Boulder", "CO"] -> ["Denver, CO", "Boulder, CO"]."""
    out = []
    for part in parts:
        if out and len(part) == 2 and part.isalpha() and part.isupper() and "," not in out[-1]:
            out[-1] = f"{out[-1]}, {part}"
        else:
            out.append(part)
    return out


def _place(name):
    # places.centre_for ignores a state code it does not know ("Nowhere, ZZ"
    # finds a Nowhere elsewhere), so the code is checked here first.
    city, _, state = name.rpartition(",")
    if not city.strip() or state.strip() not in places._STATES:
        return f"{name!r}: write it as \"City, ST\" with a two-letter US state code (e.g. \"Chicago, IL\")"
    try:
        places.centre_for(name)
    except places.PlaceError as e:
        return f"{name!r}: {e} — write it as \"City, ST\" (e.g. \"Chicago, IL\") and try again"
    return None


def _zone(name):
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return f"{name!r} is not an IANA time zone, such as America/Chicago or Europe/London"
    return None


def _hhmm(value):
    return None if profile_loader._TIME_RE.fullmatch(value) else "24-hour HH:MM, such as 08:00"


def _weekday(value):
    return None if value in profile_loader.WEEKDAYS else f"{value!r}: a weekday name such as Monday"


def guess_timezone():
    """The system's zone: TZ, else the /etc/localtime link; None if neither names one."""
    for name in (os.environ.get("TZ", "").lstrip(":"), _localtime_link()):
        if name and not _zone(name):
            return name
    return None


def _localtime_link():
    try:
        target = os.path.realpath("/etc/localtime")
    except OSError:
        return ""
    return target.split("zoneinfo/", 1)[1] if "zoneinfo/" in target else ""


def interview(data, ui):
    """Ask every question; `data` (the example profile) is updated in place.
    Returns the dream-company names the user gave (empty: keep the file)."""
    c, r, loc, comp = data["candidate"], data["roles"], data["location"], data["compensation"]
    ind, fit, src, dl = data["industries"], data["company_fit"], data["sources"], data["delivery"]

    ui.say("\n-- Candidate: who the digest is for (the scoring prompt opens with these) --")
    c["display_name"] = ui.text("Your full name", c["display_name"])
    c["short_name"] = ui.text("Name the prompts use on second mention", c["display_name"].split()[0])
    c["pronouns"] = ui.text("Pronouns (she/her, he/him, they/them)", c["pronouns"],
                            lambda a: None if a in profile_loader.PRONOUNS else "one of she/her, he/him, they/them")
    c["descriptor"] = ui.text("Short description after your name (e.g. \"an IT executive\")", c["descriptor"])
    c["headline"] = ui.text("One line: who you are and what you seek", c["headline"])
    points = ui.items("Your background, one point per item", [l.lstrip("- ") for l in c["background"].splitlines()],
                      allow_empty=False)
    c["background"] = "\n".join(f"- {p}" for p in points)

    ui.say("\n-- Roles: the title gate (a cost control; the job description decides fit) --")
    r["target_titles"] = ui.items("Titles that are exactly the role you want", r["target_titles"],
                                  allow_empty=False)
    r["seniority_terms"] = ui.items("Seniority words", r["seniority_terms"], allow_empty=False)
    r["function_terms"] = ui.items("Function words (the field the role leads)", r["function_terms"],
                                   allow_empty=False)
    r["target_department"] = ui.text("The department or function the role must lead, judged from the job "
                                     "description (\"leads ... and owns a team or a budget\")",
                                     r["target_department"])
    while True:
        data["scoring"]["role_criteria"] = role_criteria(ui, data["scoring"]["role_criteria"])
        if data["scoring"]["role_criteria"]:
            break
        ui.say("   at least one criterion, please")

    ui.say("\n-- Location: metros as \"City, ST\" --")
    example_home = loc["home"]
    loc["home"] = ui.text("Home metro", loc["home"], _place)
    # The example's commutable metros only make sense around the example's home.
    nearby = [m for m in loc["commutable"] if m != loc["home"]] if loc["home"] == example_home else []
    commutable = ui.items("Other metros you can commute to (your home metro is included)", nearby, _place,
                          metros=True)
    loc["commutable"] = [loc["home"]] + [m for m in commutable if m != loc["home"]]
    relocate = ui.items("Metros you would relocate to",
                        [m for m in loc["relocate"] if m not in loc["commutable"]], _place, metros=True)
    loc["relocate"] = [m for m in relocate if m not in loc["commutable"]]
    if loc["state_tier_overrides"] and not ui.yes(
            f"Keep the example's state overrides {dict(loc['state_tier_overrides'])}", False):
        loc["state_tier_overrides"] = {}
    ui.say("   Work model: a number added to the score (+1, -2) or \"disqualify\", applied before "
           "distance. With 0 for all three, remote roles are checked only for the country, and hybrid "
           "and onsite roles go on to the distance tiers above.")
    models = loc.setdefault("work_models", {"remote": 0, "hybrid": 0, "onsite": 0})
    for key in profile_loader.WORK_MODELS:
        models[key] = ui.outcome(f"Work model {key}", models.get(key, 0))

    ui.say("\n-- Compensation: judged on the top of a posting's stated range --")
    comp["floor"] = ui.number("Floor, annual USD (a stated top below this costs points)", comp["floor"])
    comp["stretch"] = ui.number("Stretch, annual USD (a stated top at or above this earns a point)",
                                comp["stretch"])
    comp["floor_adjustment"] = ui.outcome("Below the floor: a penalty (e.g. -2) or disqualify",
                                          comp["floor_adjustment"], sign="penalty")

    ui.say("\n-- Industries: how the scoring prompt treats sectors --")
    old_strong = list(ind["strong_yes"])
    ind["strong_yes"] = ui.items("Strong yes (+points)", ind["strong_yes"], allow_empty=False)
    ind["lean_in"] = ui.items("Lean in (+point)", ind["lean_in"])
    ind["neutral"] = ui.items("Neutral (no effect)", ind["neutral"])
    ind["lowest_interest"] = ui.items("Lowest interest (no penalty, named as least interesting)",
                                      ind["lowest_interest"])
    old_depri = list(ind["deprioritize"])
    ind["deprioritize"] = ui.items("Deprioritize (-point)", ind["deprioritize"])
    blocks = ui.items("Hard no (score 0, never emailed)", list(ind["hard_block"]), allow_empty=False)
    if blocks != list(ind["hard_block"]):
        ind["hard_block"] = {b: [b] for b in blocks}
    # Lists that only make sense against the example's own tiers.
    if ind["strong_yes"] != old_strong:
        ind["sector_order"] = []
    if ind["deprioritize"] != old_depri:
        ind["vice_core_business"] = []

    ui.say("\n-- Company appeal and values --")
    fit["enabled"] = ui.yes("Score company appeal (is the company doing something interesting?)",
                            fit.get("enabled", True))
    if fit["enabled"]:
        fit["core_question"] = ui.text("The question asked of every company", fit["core_question"])
        fit["appeal_signals"] = ui.items("What makes the answer yes", fit["appeal_signals"], allow_empty=False)
    mission = data["scoring"]["mission_values"]
    mission["enabled"] = ui.yes("Score mission and values fit", mission.get("enabled", True))
    if mission["enabled"]:
        fit["operating_values"] = ui.items("Operating values you look for", fit["operating_values"],
                                           allow_empty=False)
        fit["mission_flavors"] = ui.items("Missions that count as doing good", fit["mission_flavors"],
                                          allow_empty=False)
        fit["exemplars"] = ui.items("Companies whose fit you admire", fit["exemplars"])

    ui.say("\n-- Dream companies: their boards are fetched directly --")
    dream = ui.items("Dream companies (Enter keeps the current file)", [])

    ui.say("\n-- Sources: which job sources run --")
    for key in src["enabled"]:
        if key == "wellfound":
            continue                                   # parked: no postings from runner IPs
        src["enabled"][key] = ui.yes(f"Enable {key}", src["enabled"][key])
    if src["enabled"].get("linkedin_email"):
        src["linkedin_label"] = ui.text("Gmail label your LinkedIn job alerts are filed under",
                                        src["linkedin_label"])
    if src["enabled"].get("usajobs"):
        ui.say("   USAJOBS series: 2210 is IT Management. Others: "
               "https://www.opm.gov/policy-data-oversight/classification-qualifications/classifying-general-schedule-positions/")
        src["usajobs"]["series"] = ui.text("USAJOBS job series", src["usajobs"]["series"])

    ui.say("\n-- Delivery --")
    dl["email_to"] = ui.text("Email the digest to", dl["email_to"],
                             lambda a: None if profile_loader._EMAIL_RE.fullmatch(a) else "an email address, please")
    dl["timezone"] = ui.text("Time zone (IANA name)", guess_timezone() or dl["timezone"], _zone)
    dl["digest_time_local"] = ui.text("Digest in your inbox by (HH:MM)", dl["digest_time_local"], _hhmm)
    dl["digest_days"] = ui.items("Digest days", dl["digest_days"], _weekday, allow_empty=False)
    dl["weekly_summary_day"] = ui.text("Weekly summary day", dl["weekly_summary_day"], _weekday)
    return dream


def _derived_sources(data, old_location):
    """Adzuna locations and Built In URLs follow the location section; when it
    changed, drop the example's lists so the code derives them (PUB-05)."""
    if data["location"] != old_location:
        data["sources"]["adzuna"].pop("locations", None)
        data["sources"]["builtin"].pop("search_urls", None)


def _from_document(data, path, say):
    """PUB-10: the document's proposals become the interview's defaults; on
    any problem, say why and leave the example's defaults."""
    import setup_document
    if not os.environ.get("ANTHROPIC_API_KEY", "").strip():
        say("ANTHROPIC_API_KEY is not set, so --from-document cannot read the document (it takes one model "
            "call). Running the normal interview.")
        return
    say(f"Reading {os.path.basename(path)} (one model call)...")
    proposal, problem = setup_document.propose(path, data["scoring"]["model"])
    if problem or not proposal:
        say(f"{problem or 'the model proposed nothing usable'}. Running the normal interview.")
        return
    setup_document.apply(data, proposal, _criterion_id)
    say("Proposed from the document (shown as the defaults below; Enter keeps, or type your own):")
    for line in setup_document.describe(proposal):
        say(line)


def write_profile(data, path):
    with open(path, "w") as fh:
        fh.write(HEADER)
        yaml.safe_dump(data, fh, sort_keys=False, allow_unicode=True, width=100)


def main(argv=None, ask=input, say=print, profile_path=PROFILE, root=ROOT, run_after=True):
    parser = argparse.ArgumentParser(prog="python main.py setup",
                                     description="Interview in the terminal and write profile.yaml (PUB-06)")
    parser.add_argument("--force", action="store_true", help="replace an existing profile.yaml")
    parser.add_argument("--from-document", metavar="PATH",
                        help="a resume or job description (.txt, .md, .docx, .pdf): one model call proposes the "
                             "background, target department and titles and role criteria as defaults")
    args = parser.parse_args(argv)
    if os.path.exists(profile_path) and not args.force:
        say(f"{os.path.relpath(profile_path, root)} already exists; nothing written. "
            f"Run python main.py setup --force to replace it (it is in git, so the old one stays recoverable).")
        return 1
    with open(EXAMPLE) as fh:
        data = yaml.safe_load(fh)
    old_location = yaml.safe_load(yaml.safe_dump(data["location"]))
    if args.from_document:
        _from_document(data, args.from_document, say)
    say("Setup: answer each question, or press Enter to keep the [default]. Ctrl-C stops without writing.")
    ui = Interview(ask, say)
    try:
        dream = interview(data, ui)
    except (KeyboardInterrupt, EOFError):
        say("\nStopped; nothing written.")
        return 1
    _derived_sources(data, old_location)
    # The example's five names stay in use only until you give your own.
    if dream or os.path.exists(os.path.join(root, DREAM_FILE)):
        data["dream_companies"]["file"] = DREAM_FILE

    if dream:
        with open(os.path.join(root, DREAM_FILE), "w") as fh:
            fh.write("".join(f"{name}\n" for name in dream))
        say(f"Wrote {DREAM_FILE} ({len(dream)} companies).")
    write_profile(data, profile_path)
    say(f"Wrote {os.path.relpath(profile_path, root)}.")
    errors = profile_loader.validate(data)
    for e in errors:
        say(f"   problem: {e}")
    if not run_after:
        return 1 if errors else 0
    python = sys.executable
    for step in (["main.py", "profile-check"], ["main.py", "cron", "--write"]):
        say(f"\n$ python {' '.join(step)}")
        code = subprocess.run([python, *step], cwd=root).returncode
        if code:
            say(f"That step failed; fix profile.yaml, then run python {' '.join(step)} again.")
            return code
    say(f"\nNext: {NEXT_COMMAND}")
    return 0
