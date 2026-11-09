# sheet_layout.py — the Sheet's tab names and header rows (PUB-10)
#
# A leaf module (no profile, no environment) so `python main.py sheet-init`
# can lay out a new Sheet before profile.yaml exists. config.py re-exports
# these names; columns are always looked up by header name, never by position.

SHEET_NAME = "Job Digest Index Sheet"
DISQUALIFIED_SHEET_NAME = "Disqualified"

SHEET_COLUMNS = [
    "Job Title", "Company", "Location", "Work Model", "Comp",
    "Score", "Claude Rationale", "Source", "Date First Seen",
    "Apply Link", "Status", "Applied Date", "Notes", "Warm Contact", "Relo",
    "Tier", "Posted Date", "Status Date",
]
# Written only when the column exists (SCO-09, PUB-09, PUB-16); the pipeline
# never adds them to an existing Sheet, but sheet-init lays them out on a new one.
OPTIONAL_COLUMNS = ["Function Fit", "Criteria", "Dream"]

DISQUALIFIED_COLUMNS = [
    "Job Title", "Company", "Location", "Work Model", "Comp",
    "Score", "Disqualify Reason", "Source", "Date First Seen", "Apply Link", "Relo",
    "Tier", "Posted Date",
]

# REV-04: the headers each tab cannot do without. Row 1 must carry every one,
# in any order (exact text after trimming), or a write stops before it writes
# anything. Every other column is optional: written when present, reported
# when absent ("optional column missing: <name>"), never created. Only
# sheet-init, on an empty tab, lays out a header row.
REQUIRED_HEADERS = {
    SHEET_NAME: ("Job Title", "Company", "Apply Link", "Status", "Date First Seen", "Score"),
    DISQUALIFIED_SHEET_NAME: ("Job Title", "Company", "Apply Link", "Date First Seen", "Score",
                              "Disqualify Reason"),
}


def header_problems(title, row1):
    """(missing required headers, repeated headers) for a tab's row 1, compared
    by exact text after trimming. Pure: doctor and sheets.py share it."""
    names = [str(h).strip() for h in row1]
    seen, repeated = set(), []
    for name in names:
        if name and name in seen and name not in repeated:
            repeated.append(name)
        seen.add(name)
    missing = [h for h in REQUIRED_HEADERS.get(title, ()) if h not in seen]
    return missing, repeated


def optional_missing(title, row1):
    """The layout's optional columns for this tab that row 1 lacks."""
    names = {str(h).strip() for h in row1}
    layout = dict(TABS)[title]
    return [h for h in layout if h not in REQUIRED_HEADERS[title] and h not in names]


# (tab, the header row sheet-init writes)
TABS = ((SHEET_NAME, SHEET_COLUMNS + OPTIONAL_COLUMNS), (DISQUALIFIED_SHEET_NAME, DISQUALIFIED_COLUMNS))
