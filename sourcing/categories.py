"""Sector categories for the startup tier: Robotics, Space/Aerospace, Materials (SRC-17).

The Crunchbase harvest (sourcing/process_harvest.py) had no category for these
three sectors, so robotics companies landed in Hardware and space companies in
GovTech/Defense. They are the top of the PRD's sector lean (robotics > energy >
hardware), so they get their own categories, checked before every other rule.

A company is matched on its Crunchbase industries first, then on its
description. Description terms are phrases, never a bare word, because the bare
words are ambiguous:
  - "material" is mostly "course materials", "marketing materials",
    "materials handling", "raw materials", "building materials" distribution;
  - "space" is mostly "office space", "workspace", "in the X space";
  - "robotic" is often "robotic process automation" (software, not robots);
  - "satellite" is often "satellite office" or "satellite clinic".
EXCLUDE removes those senses from the text before the terms are matched, so a
company that says "materials science" and also "marketing materials" still
matches on the first.

Standalone: `python3 sourcing/categories.py` prints per-category counts over
companies_master.csv and the loose-match rows the tight match excludes.
"""

from __future__ import annotations

import re

ROBOTICS = "Robotics"
SPACE = "Space/Aerospace"
MATERIALS = "Materials"

# Checked in this order: a drone maker that also says "aerospace" is Robotics.
SECTOR_ORDER = (ROBOTICS, SPACE, MATERIALS)

# Crunchbase industry values (exact, case-insensitive) that decide on their own.
INDUSTRIES = {
    ROBOTICS: {"robotics", "drones", "drone management", "autonomous vehicles"},
    SPACE: {"aerospace", "space travel", "satellite communication"},
    MATERIALS: {"advanced materials", "nanotechnology"},
}

# Description phrases (after EXCLUDE is applied).
TERMS = {
    ROBOTICS: [
        r"\brobot(s|ic|ics)?\b", r"\bcobots?\b", r"\bhumanoids?\b", r"\bdrones?\b",
        r"\bunmanned (aerial|ground|surface|underwater)\b", r"\buavs?\b",
        r"\bautonomous (vehicle|vehicles|truck|trucks|trucking|system|systems|machine|machines|"
        r"aircraft|vessel|vessels|mobile)\b", r"\bself-driving\b",
    ],
    SPACE: [
        r"\baerospace\b", r"\bspacecraft\b", r"\bspaceflight\b", r"\bspace (industry|economy|"
        r"exploration|launch|station|technology|technologies|systems|domain|situational|"
        r"infrastructure|missions?|vehicles?|debris|based|companies)\b", r"\bouter space\b",
        r"\bin-space\b", r"\bin space\b", r"\bsatellites?\b", r"\brockets?\b",
        r"\blaunch (vehicle|vehicles|services|provider)\b", r"\b(low earth|lunar) orbit\b",
        r"\borbital\b", r"\baircraft\b", r"\bevtol\b", r"\bhypersonic\b",
        r"\bjet engines?\b", r"\bavionics\b", r"\bairspace\b", r"\baeronautic(s|al)\b",
        r"\b(defense|defence|aerospace|security|military)(,| and| &)+ space\b",
        r"\bspace(,| and| &)+ (defense|defence)\b", r"\bfor space\.",
    ],
    MATERIALS: [
        r"\bmaterials? (science|sciences|scientists?|engineering|discovery|innovation|"
        r"informatics|research|development|company|platform|technology|technologies|startup|"
        r"manufacturer|producer|recycling)\b", r"\b[a-z]+-based materials?\b",
        r"\b(advanced|novel|new|engineered|sustainable|bio-based|biobased|smart|functional|"
        r"high-performance|next-generation|next generation|low-carbon|critical|battery|"
        r"composite|structural|quantum|2d|nano|energy|semiconductor|specialty) materials?\b",
        r"\bnanomaterials?\b", r"\bbiomaterials?\b", r"\bmetamaterials?\b", r"\bgraphene\b",
        r"\bcarbon nanotubes?\b", r"\bcarbon fiber\b", r"\bpolymers?\b", r"\bcomposites\b",
        r"\balloys?\b", r"\bceramics\b", r"\bnanotechnology\b", r"\bthin films?\b",
        r"\blow-carbon (cement|concrete|steel)\b", r"\bgreen (cement|concrete|steel)\b",
    ],
}

# Removed from the description before TERMS are matched.
EXCLUDE = [
    # "material" in unrelated senses
    r"\bmaterials? handling\b", r"\b(course|marketing|training|educational|learning|"
    r"teaching|curriculum|sales|promotional|print|printed|reading|study|source|reference|"
    r"packaging|construction|building|raw|hazardous) materials?\b", r"\bmaterials? management\b",
    r"\bmaterial (impact|change|difference|risk|information|breach|weakness)\b",
    r"\bbill of materials\b",
    # "robotic" meaning software bots
    r"\brobotic (data |desktop |business )?(process )?automation\b", r"\brpa\b", r"\brobo-?advis[oe]rs?\b",
    r"\bsoftware robots?\b",
    # "satellite"/"space" in unrelated senses
    r"\bsatellite (office|offices|campus|clinic|clinics|location|locations|radio|tv|television)\b",
    r"\bin space\b(?= (planning|management|utilization|design))",
    # Crunchbase boilerplate for government contractors of every kind
    r"\bis an? (defense|defence) (&|and) space company\b",
]

LEGAL_SUFFIX = re.compile(r"[,.]?\s+(inc|llc|ltd|corp|corporation|co|company)\.?$", re.I)


def _without_name(text: str, company: str) -> str:
    """The description with the company's own name removed.

    Names carry the sector words without the sector: Rocket Lawyer, Rocket
    Software, SpaceCraft (web builder), Alloy (telehealth), Bad Robot Games.
    """
    for name in {company.strip(), LEGAL_SUFFIX.sub("", company.strip())}:
        if len(name) > 2:
            text = re.sub(re.escape(name), " ", text, flags=re.I)
    return text

# A Crunchbase industry tag is not enough when the description says the company
# sells services or business software and names nothing in the sector: Nintex
# (workflow software), MCA Connect (Dynamics integrator), Egen (consulting),
# Ashling Partners (RPA), Digital Divide Data (data labelling) all carry a
# Robotics or Autonomous Vehicles tag.
SERVICES_ONLY = re.compile(
    r"\b(system integrator|systems integrator|consulting|consultancy|consultants?|"
    r"business process(es)?|process automation|rpa|data labell?ing|data annotation|"
    r"it services|it solutions|staffing|recruit(ing|ment)|managed services|"
    r"outsourcing|professional services|advisory)\b", re.I)

# The harvest's own rules (sourcing/process_harvest.py, 26 Aug), for everything
# outside the three sectors. First match on the industry list wins.
HARVEST_RULES = [
 ("GovTech/Defense", ["govtech","government","defense","military","national security","public sector","law enforcement","space","aerospace"]),
 ("Energy", ["energy","solar","renewable","battery","nuclear","oil and gas","cleantech","clean energy","electric vehicle","power","utilities","climate"]),
 ("Hardware", ["hardware","semiconductor","electronics","manufacturing","robotics","internet of things","sensor","3d printing","drone","industrial automation","wearable"]),
 ("Health/Bio", ["health care","biotechnology","medical","pharmaceutical","life science","genetics","mental health","hospital","clinical","therapeutics","biopharma","health diagnostics"]),
 ("Cyber/Security", ["cyber security","network security","information security","identity management","fraud detection","privacy","security"]),
 ("Fintech", ["fintech","financial services","banking","payments","lending","wealth management","accounting","credit","capital markets"]),
 ("AI/ML", ["artificial intelligence","machine learning","generative ai","natural language processing","computer vision","predictive analytics","ai infrastructure"]),
 ("B2B/SaaS", ["saas","enterprise software","b2b","information technology","software","cloud computing","developer tools","productivity tools","enterprise resource planning","crm","analytics","data"]),
]
SECTORS = {"Robotics", "Space/Aerospace", "Materials"}

# Checked by hand (SRC-17): the rules cannot tell these from robotics companies.
CATEGORY_OVERRIDES = {
    "nintex": "B2B/SaaS",               # workflow and process-automation software
    "mca connect": "B2B/SaaS",          # Microsoft Dynamics integrator
    "egen solutions": "B2B/SaaS",       # software and data consulting
    "ashling partners": "B2B/SaaS",     # RPA consultancy
    "digital divide data": "AI/ML",     # ML data labelling services
}

INDUSTRY_SPLIT = re.compile(r"\s*;\s*")
_TERMS = {cat: re.compile("|".join(TERMS[cat]), re.I) for cat in SECTOR_ORDER}
_EXCLUDE = re.compile("|".join(EXCLUDE), re.I)


def industries_of(value: str) -> set[str]:
    return {i.strip().lower() for i in INDUSTRY_SPLIT.split(value or "") if i.strip()}


def sector_of(industries: str, description: str, company: str = "") -> str | None:
    """Robotics, Space/Aerospace or Materials, or None when the company is none of them.

    A company can hit more than one sector (a drone maker says "aerospace",
    NASA lists Robotics). Each matching industry counts 2 and each distinct
    description phrase 1; the highest total wins, ties go to SECTOR_ORDER.
    The company name counts only for Materials ("Blue Whale Materials").
    """
    inds = industries_of(industries)
    text = _EXCLUDE.sub(" ", _without_name(description or "", company or ""))
    services_only = bool(SERVICES_ONLY.search(text))
    best, best_score = None, 0
    for cat in SECTOR_ORDER:
        phrases = len({m.group(0).lower() for m in _TERMS[cat].finditer(text)})
        tags = 0 if (services_only and not phrases) else len(inds & INDUSTRIES[cat])
        score = 2 * tags + phrases
        if cat == MATERIALS and re.search(r"\bmaterials\b", _EXCLUDE.sub(" ", company or ""), re.I):
            score += 1
        if score > best_score:
            best, best_score = cat, score
    return best


def harvest_category(industries: str, drop_sector_tags: bool = False) -> str:
    """The pre-SRC-17 category from the harvest's industry rules.

    drop_sector_tags ignores the sector industry tags, for a company the
    services rule has already ruled out of the sectors -- otherwise its
    Robotics tag sends it straight to Hardware.
    """
    inds = industries_of(industries)
    if drop_sector_tags:
        inds -= set().union(*INDUSTRIES.values()) | {"industrial automation"}
    low = " | ".join(sorted(inds))
    for name, terms in HARVEST_RULES:
        if any(t in low for t in terms):
            return name
    return "Other"


def category_of(industries: str, description: str, company: str = "") -> str:
    """A hand override, else a sector when one matches, else the harvest's category."""
    override = CATEGORY_OVERRIDES.get((company or "").strip().lower())
    if override:
        return override
    return sector_of(industries, description, company) or harvest_category(industries, True)


def recategorize(current: str, industries: str, description: str, company: str = "") -> str:
    """The row's category with the three sectors taking precedence over the rest.

    A row that no longer matches a sector goes back to the harvest's rule; any
    other category is kept as it is.
    """
    override = CATEGORY_OVERRIDES.get((company or "").strip().lower())
    if override:
        return override
    sector = sector_of(industries, description, company)
    if sector:
        return sector
    return harvest_category(industries, True) if current in SECTORS else current


# The loose match the 21 Sep count used; kept only to report what tightening excludes.
LOOSE = {ROBOTICS: re.compile(r"robot", re.I),
         SPACE: re.compile(r"space|aerospace|satellite|rocket", re.I),
         MATERIALS: re.compile(r"material", re.I)}


def _report() -> None:
    import collections

    from harvest import read_harvest

    rows = read_harvest(("company", "industries", "description"))
    tight = collections.Counter()
    excluded: dict[str, list[dict[str, str]]] = collections.defaultdict(list)
    for r in rows:
        cat = sector_of(r["industries"], r["description"], r["company"])
        if cat:
            tight[cat] += 1
        for c, pat in LOOSE.items():
            if pat.search(r["industries"] + " " + r["description"]) and not sector_of(
                    r["industries"], r["description"], r["company"]) == c:
                excluded[c].append(r)
    for cat in SECTOR_ORDER:
        print(f"{cat:16s} tight {tight[cat]:5d}   loose-only (excluded or other sector) "
              f"{len(excluded[cat]):5d}")
    for cat in SECTOR_ORDER:
        print(f"\n--- {cat}: loose matches the tight rule drops (first 25) ---")
        for r in excluded[cat][:25]:
            got = sector_of(r["industries"], r["description"], r["company"]) or "-"
            print(f"  [{got}] {r['company']}: {r['description'][:110]}")


if __name__ == "__main__":
    _report()
