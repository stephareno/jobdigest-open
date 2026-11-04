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

# (tab, the header row sheet-init writes)
TABS = ((SHEET_NAME, SHEET_COLUMNS + OPTIONAL_COLUMNS), (DISQUALIFIED_SHEET_NAME, DISQUALIFIED_COLUMNS))
