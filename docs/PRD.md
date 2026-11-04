# Job Digest Agent: Product Requirements

> **Who this is for.** This document is written to be read by an AI assistant or coding agent working on or setting up the Job Digest Agent. It is the guiding description of what the agent does and the rules it follows, in enough detail that an assistant can set it up, change it, or explain it without guessing. A person can read it too; the first two sections are the quick version, and the rest is reference.

**Get an email with jobs worth your time, so you never have to look for openings.**

Version 3.1 (public). This document is the source of truth for behavior: if the code and this document disagree, one of them is wrong, and whoever owns the repository decides which and then updates the other.

The agent is an engine. Everything about the person it works for lives in one file, `profile.yaml`. This document uses the word *candidate* for that person and refers to profile fields by their dotted path, for example `location.home`.

## 1. What it does

Every morning, on the days the candidate chooses, the agent emails a digest that lists open job postings that fit the role they are looking for. Each job is scored by fit, and every posting the agent saw is logged to a Google Sheet. To do that, the agent:

1. Fetches postings from company career boards, aggregators, LinkedIn alert emails and USAJOBS, three times a day.
2. Drops postings that cannot fit before spending anything on them: wrong kind of title, already seen, outside the country, in a blocked industry.
3. Scores each remaining posting from 1 to 10 with a language model, using a prompt rendered from the candidate's profile. The model judges role fit only.
4. Applies modifiers in code for the things the model does not weigh: work model, distance from home, and pay. Each can be a bonus, a penalty or a dealbreaker.
5. Queues the scored postings as they come through and, once a day, sends the digest and stores record of everything into a Google Sheet.
6. Sends a weekly summary with status and statistics: what was applied to, what is in progress, what scored well, what the candidate declined, which sources produced.

The candidate reads one email. Everything else is bookkeeping the agent does for itself.

## 2. Who it is for

Anyone running a deliberate job search who wants to see fewer, better postings without checking ten sites a day. The engine is indifferent to field or seniority: the profile decides whether it looks for a VP of Finance in Charlotte, an event coordinator in Denver or a research scientist who will work anywhere remote. It assumes the candidate:

- has a Gmail account (used for sending the digest and, optionally, reading LinkedIn alerts),
- has a Google account for the Sheet,
- can create a few free API keys, or has an assistant that can walk them through it,
- can run a handful of terminal commands, or has an AI assistant that can walk them through it.

It does not apply to jobs, write cover letters or contact anyone. It finds and ranks.

## 3. Principles

These shape every design decision and are not negotiable in the engine.

**Three parts, nothing in two places.** `profile.yaml` holds identity and preferences. Environment variables hold secrets and per-user resource IDs. Code holds mechanism. The test for any value: would a different job seeker change this? If yes, it belongs in the profile.

**The model scores role fit only.** Pay and place never enter the model's score. They are modifiers applied in code afterward, where they are deterministic, free and visible in the Sheet.

**The title gate is a cost control, not a quality filter.** It exists to avoid paying for scoring calls on postings that cannot be the target role. The job description decides fit, never the title alone. Proven matches are never traded for noise reduction.

**A slow or failing source never blocks delivery.** Every source runs under a time budget. A board that fails is recorded as failed for that run and yields nothing. The digest goes out with whatever is in the queue.

**Boards are parked, never deleted.** When a board stops working it is parked with a reason and a date, re-probed monthly, and un-parked if it recovers.

**No credentials in code, ever.** Secrets come from environment variables locally and from GitHub Actions Secrets in production.

**Nothing is written twice.** Fetch runs and the digest run write different files, so they can overlap without conflicts.

**Fail loudly.** Errors are descriptive. A bad profile stops the run at startup, before anything is fetched or marked seen. Billing and authentication failures on the scoring API abort the run rather than recording postings at score 0.

**The candidate can always see why.** Every score in the digest and the Sheet carries the criteria that fired and the modifiers that applied, in words.

## 4. Architecture

Two scheduled workflows and a queue between them.

```
sources ──► filters ──► scoring ──► modifiers ──► queue ──► digest ──► email + Sheet
 (fetch.yml, 3x/day)                              (state/pending/)  (daily.yml, 1x/day)
```

**fetch.yml** runs three times a day. Each run fetches one shard of the board set (so every board is visited at least every two days, most daily), plus the once-a-day sources on the first run. It filters, scores and writes one immutable batch file to `state/pending/`. It also updates the seen-job state so nothing is scored twice.

**daily.yml** runs once a day, on the candidate's digest days, timed so the email arrives by the hour they chose. It reads the queue, builds the email, writes the Sheet, deletes the batch files it sent, and runs the optional inbox passes. It never writes the files fetch writes.

**weekly_summary.yml** runs once a week on the no-digest day.

**resolve_ats.yml** runs monthly: re-resolves company career sites to their applicant tracking systems, re-probes parked boards, retries a batch of companies whose boards could not be checked, and opens a pull request if anything changed. Nothing is changed without review.

State files (`seen_jobs.json`, `shard_state.json`, `bespoke_state.json`, `skip_state.json`, `state/pending/`) are written by the workflows and committed back to the repository. They are never edited by hand.

## 5. Sources

Each source is a module. `sources.enabled` in the profile turns each on or off; a disabled source is never imported. API keys live in environment variables.

| Source | What it is | Needs | Title-gated |
|---|---|---|---|
| `ats` | The career boards of the candidate's dream companies, read directly from their applicant tracking systems: Greenhouse, Lever, Ashby, SmartRecruiters, Workable, Rippling, Recruitee, BambooHR, Breezy, Teamtailor, Personio, Oracle HCM, ADP, UKG Ready, Gem, Comeet, Manatal, JobScore | `data/dream_companies.txt` | yes |
| `startups` | The same kind of direct reads for the larger tier of companies from the candidate's lists (§13) | company lists | yes |
| `watchlist` | Companies with no fetchable board: Workday tenants and plain career pages, pinned in `data/companies.yaml` | nothing | wider gate |
| `bespoke` | Career pages with no board at all, for companies on the candidate's lists. A rolling crawler re-reads each page on its own schedule, notices when it changed, and has the model extract the new postings. Slower, and the first read of each page is a model call; works for plain pages, not for pages that only render with JavaScript | company lists | yes |
| `linkedin_email` | Parses LinkedIn job-alert emails from a Gmail label. The agent never touches linkedin.com; it reads only the alert emails LinkedIn chose to send to the candidate's own inbox | Gmail app password; the candidate sets up alerts and a filter | no |
| `adzuna` | Aggregator API, searched by the profile's queries and locations | free Adzuna app id and key | yes |
| `jsearch` | Aggregator API on RapidAPI, searched by the profile's queries | RapidAPI key (free tier) | yes |
| `usajobs` | US federal postings by job series and title keywords | free USAJOBS key | no (bounded by series, hiring path and pay floor) |
| `builtin` | Built In's search pages for the home metro and remote | nothing | yes |

Direct board reads are the best source: fresh, complete, with a direct apply link. Aggregators supplement them and catch companies not on any list; they are noisier, their apply links go through an intermediary, and the engine never leans on them to compensate for lost boards.

**Known gap.** Some large employers use systems the engine cannot read yet: iCIMS, SuccessFactors, Phenom, ClearCompany, Pinpoint, and JavaScript-only career pages. In a sample of US public companies, about 9 percent used one of these; roles there reach the digest only through an aggregator or a LinkedIn alert. Companies found on an unsupported vendor are recorded as such so the gap is measurable.

Which sources are gated is a measured decision: a source is gated only when back-testing the gate against the Sheet loses no historical high-scoring roles from it. The shipped defaults reflect that measurement for one candidate; a new candidate can change `roles.title_gated_sources` after their own calibration.

## 6. Filtering

All filters run after fetching and before scoring, in this order.

**Title gate** (`roles.*`), matched against the title only, whole word, case-insensitive. A title passes if it is one of `roles.target_titles`, or contains one word from `roles.seniority_terms` and one from `roles.function_terms`. Words in `roles.function_terms_never_alone` count only inside a longer phrase ("Enterprise Governance" counts; "enterprise" alone does not). The lists are meant to be long; one word from each is enough, so they cast a net rather than filter. Sources not in `roles.title_gated_sources` skip the gate.

**Deduplication.** Two keys, both kept in `seen_jobs.json` and aged out together:

- URL hash, after stripping tracking parameters.
- Content key: normalized company + title + location, checked against earlier runs as well as the current one. Two apply links on the same host under different posting ids are never merged.

A repeat is dropped before scoring, so it costs nothing.

**Country filter** (`location.country`, `location.outside_country`). Postings pinned outside the candidate's country take the `outside_country` outcome, by default a dealbreaker, for every work model. With the dealbreaker (`disqualify`), the location filter drops them right after fetching, before scoring: no model call, no Sheet row, only the count in the run log (`Total unique jobs after location filter: N (dropped M)`). A number instead is applied after scoring like any other penalty, and the role is logged. A country signal in the location string (state abbreviation, the country's name or abbreviation) always beats a foreign term, so Paris TX and Dublin OH stay in.

**Hard-block pre-filter** (`industries.hard_block`). Each blocked industry has name keywords. A whole-word hit in the company name or title scores 0 without a scoring call. The job description is never searched here; the scoring call judges industry from the body.

## 7. Scoring: role fit

The scoring call returns a base score from 1 to 10 for role fit, the criteria that applied, a rationale, the industry, the work model, the city and state it read from the posting, which department the role sits in, and a flag for any hard-block industry. The prompt is rendered from `prompts/scoring_system.txt` and the profile; every wording change is reviewed as a diff of the rendered prompt.

The model is told, in the prompt, to score the role and the company only and never to adjust for pay or location.

**Role criteria** (`scoring.role_criteria`). The heart of the score, in the candidate's words. Each entry has an `id`, a one-line `criterion` the model can recognize in a posting, and either `points` (−5 to +5) or `dealbreaker: true`. The interview builds the list from questions about scope and authority, team and reporting line, nature of the work, tools and domain alignment, conditions such as travel or on-call, growth and stage, and anything that would make the candidate pass regardless. Three to eight entries is typical. The model reports which criteria applied in `criteria_hits`; points are applied by the model, dealbreakers by code (§8), so the model never does disqualifying arithmetic. A dealbreaker applies only when the posting states it plainly; silence or ambiguity means it does not apply.

**Industry tier** (`industries.*`, `scoring.sector_adjustments`). Five lists the prompt renders and the model applies: `hard_block` (score 0, with name keywords for the pre-filter), `deprioritize` (−1), `lowest_interest` and `neutral` (0), `lean_in` (+1), `strong_yes` (+2). Lists have no length limit and may be empty. `sector_order` breaks ties inside strong yes. `vice_core_business` names businesses that take a larger penalty when they are the company's core. Each penalty applies once. When a staffing or recruiting firm posts a role, industry, sector and values are judged on the hiring client named in the description; a staffing penalty applies only when the client is itself a staffing firm.

**Company appeal** (`company_fit.*`, optional). When `company_fit.enabled` is true, the model also answers `company_fit.core_question` ("Is this company doing something interesting?") using `company_fit.appeal_signals`, by default: innovating in its field, doing real research, a novel product or approach, mission-driven, doing good for the world, a brand people know and respect. These signals, with the industry tier, earn the sector bonus. `company_fit.exemplars` names companies the candidate points to as the fit they mean. Company size is not a factor unless `size_matters` is set. `b2b_over_b2c` records whether consumer businesses are off-target.

**Mission and values** (`scoring.mission_values`, optional). When enabled, a weighted factor from `min` to `max` (default −2 to +2), the same in every sector. A mission that does good earns `mission_alone` (+1) on its own; its flavors are `company_fit.mission_flavors`. Evidence of how the company operates (`company_fit.operating_values`) adds the rest, up to the cap. The factor is 0 only when the posting and company give no signal of either. It is never disqualifying. The interview sets the cap from how much the candidate says mission and culture matter, and disables the factor for someone who says they do not.

**Function fit** (`roles.target_department`, `scoring.function_cap`). The model judges from the description, not the title, whether the role leads the target department (`leads_department`), sits in it without leading (`in_department`), or is outside it (`outside_department`). A role outside the department is capped at `function_cap` (default 4) after every other adjustment, so it is logged but never emailed in the main sections.

**Reliability.** A transient API error (rate limit, server error, timeout) defers the posting to a later run, up to three times, before recording it at score 0. A billing, authentication or permission error aborts the fetch run before any posting is marked seen; postings are never recorded at 0 for those causes. A per-run cap, `scoring.max_calls_per_run`, holds further gate-passing postings unseen for the next run, dream companies first, so a large new company list spreads its cost over days instead of one bill.

## 8. Modifiers: work model, location, pay

Applied in code after the call, in this order. Every outcome is an integer (bonus or penalty) or the word `disqualify`, which means score 0, the Disqualified tab, a recorded reason, and no email, exactly like a hard block. A penalty never takes a score below `scoring.min_score_after_penalties` (default 1); only `disqualify` and hard blocks produce 0. The final score never exceeds `scoring.max_score` (default 10).

1. **Work model** (`location.work_models`): remote, hybrid and onsite each have an outcome, default 0. A dealbreaker here short-circuits everything after it. Default logic: a remote role only has to be in the country; hybrid and onsite roles go on to the distance tier.
2. **Location tier** (`location.*`), hybrid and onsite only, by straight-line distance from the posting's resolved location:

| Where | Outcome |
|---|---|
| Within `home_bonus_radius_miles` (30) of `home` | `home_bonus` (+1) |
| Within `commutable_radius_miles` (60) of any `commutable` metro | no change |
| Within `relocate_radius_miles` (40) of a `relocate` metro | `relocate_penalty` (−2), tagged "relo" |
| From the commutable radius out to `outer_band_miles` (100) of a commutable metro | `outer_band_penalty` (−2), tagged "relo" |
| Anywhere else in the country | `elsewhere_penalty` (−5), tagged "relo" |

   Every place is written "City, ST" and resolved against an offline places file (GeoNames, no network at run time); a precise centre can be pinned with `lat` and `lon`. The home bonus goes only to roles within the radius of `home`; the other commutable metros get nothing, and remote roles get nothing however local the company is. When the posting's location string cannot be placed, fallbacks run in order: the city and state the scoring call read from the description; for a bare town name, the most favorable tier among same-named places of `sizeable_place_population` or more (else the most populous); for a state-only string, the tier most of that state's population falls in, with `state_tier_overrides` winning; "United States" or blank: no change and no tag. The run log counts hybrid and onsite locations by resolution method.
3. **Compensation** (`compensation.*`), judged on the top of the stated range: the source's comp field or a pay figure in the description, never an estimate or an aggregator's prediction. A minimum with no top has no stated top. Below `floor`: `floor_adjustment` (default −2, may be `disqualify`). At or above `stretch`: `stretch_adjustment` (+1). No stated pay: `unstated` (default 0). The digest shows the full stated range and names the adjustment.
4. **Dealbreaker criteria**: any `role_criteria` entry marked `dealbreaker: true` that the model reported in `criteria_hits` sets the score to 0 with the criterion's name as the reason.
5. **Function cap**, then the floor and the cap.

Digest thresholds (`scoring.thresholds`): main digest at 7 and above, "worth a look" at 5 and above, and dream-company roles reach the main section from 3 and above.

## 9. Delivery

**The digest** is an HTML email to `delivery.email_to`, sent from the Gmail account in `GMAIL_ADDRESS`, on `delivery.digest_days`, timed to arrive by `delivery.digest_time_local` in `delivery.timezone`. Sections, in order: main digest, worth a look, and "cool company, wrong role" (capped at `scoring.cool_company_wrong_role_cap` entries by company fit; all are logged). Each entry shows score, title, company, location and work model, the "relo" tag where it applies, stated compensation, posting age with a fresh flag, the criteria and modifiers that produced the score, the rationale, and a resolved apply link. Any failed or cancelled fetch run since the last digest shows as a banner line with a link to the run.

A digest day with no new postings sends no email when `delivery.skip_empty_days` is true, and counts as a skipped day in the weekly summary.

**The weekly summary** goes out on `delivery.weekly_summary_day` at `delivery.weekly_summary_time_local`: what scored well, what the candidate declined and why that matters for scoring, which sources produced, skipped days, and boards parked or recovered.

**Schedule.** GitHub Actions cron is UTC and has no notion of daylight saving. `python main.py cron --write` writes one cron line per UTC offset the candidate's timezone uses across the year (two for most US zones, one for Arizona or UTC), and every line fires. A small first job in each workflow checks whether its line is the intended local slot and skips the run otherwise, so the real run never waits on the twin and the candidate never edits a schedule at a clock change. Fire times are set hours before the delivery time to absorb GitHub's queue delay, which on a free-tier repository is routinely two to four hours.

## 10. The Google Sheet

Every posting the agent scores is logged. Score 1 and above go to the main index; score 0 goes to a Disqualified tab with the reason (hard block, dealbreaker). Postings dropped before scoring never reach the Sheet: wrong title, already seen, and, with the default `outside_country: disqualify`, outside the country (§6). Columns are always addressed by header name, never by position, so the candidate can reorder them; optional columns (criteria, hiring company) are written only if their header exists and are never created. `python main.py sheet-init` creates the tabs and headers in a blank Sheet.

**Status** moves forward only: New → Interested → Applied → Interviewing → Rejected. `Declined` means the candidate passed; `Rejected` means the company did. The weekly summary's feedback reads Declined rows only, so the two are never confused. Declined and Closed are set by hand and never overwritten.

**Inbox passes, opt-in** (`sources.status_tracking.enabled`, `sources.applied_doc.enabled`, both off by default). Status tracking reads every message in the inbox, from any sender, and uses the model to spot application-lifecycle emails and advance the status of matching rows by company and title; it logs ambiguous matches and never guesses or creates rows. Index Doc seeding reads a Google Doc of applied roles and marks matching rows Applied. The setup interview explains what each one reads and asks before turning either on; the LinkedIn reader, by contrast, sees only its label.

## 11. The profile

`profile.yaml` is the only file a new candidate must write. Every field is commented in `profile.example.yaml`. The loader validates the whole file at startup and reports every problem at once with its field path. Sections:

| Section | Holds |
|---|---|
| `candidate` | name, pronouns, one-line descriptor, headline, the "about" lines the prompt uses |
| `roles` | `target_department` (judged from the description); `target_titles`, `seniority_terms`, `function_terms`, `function_terms_never_alone` (matched against the title); which sources are gated; the watchlist's wider gate; optional department examples and a function-fit note |
| `scoring` | `role_criteria`; `max_calls_per_run`; point values, thresholds, function cap, digest caps, model id; `mission_values` with its switch |
| `company_fit` | switch, core question, appeal signals, operating values, mission flavors, exemplars |
| `industries` | the five tiers with name keywords for hard blocks, vice list, sector order |
| `location` | country and outside-country outcome; work-model outcomes; home, commutable and relocate metros, radii, penalties, home bonus, state overrides |
| `compensation` | floor and stretch with their outcomes; the unstated-pay outcome |
| `dream_companies` | path to the canonical list |
| `sources` | enabled map, LinkedIn label, per-source queries, USAJOBS series and paths, `company_lists` (§13), the two inbox-pass switches |
| `delivery` | recipient, timezone, digest time and days, weekly day and time, skip-empty rule |

Fields a stranger rarely needs are derived when unset: Adzuna locations and Built In URLs come from `location.home` and `commutable`; the USAJOBS pay floor reuses `compensation.floor`.

## 12. Setup and operations

Setup is a guided conversation in `docs/SETUP_GUIDE.md`, written to work pasted into a chat assistant (which gives the candidate commands and reads what they paste back) or run inside a coding agent with a shell (which does the steps itself). Phases:

0. Orientation: what the agent does, how it is built, the phases, ballpark cost, a sample digest to look at, and a resume point if returning after a pause.
1. GitHub setup: **Use this template** on the public repository (a template copy has a clean history and no link back; a fork would be the wrong starting point because GitHub Actions runs in the candidate's own repository with their own secrets), clone, Python check, install. Checkpoint: `python main.py doctor --phase 1`.
2. Accounts, keys, and where postings come from: the three kinds of sources and the known gap explained first; then Gmail app password, Google Cloud project and service account, the Sheet shared with the service account and initialised with `sheet-init`, model API key; then the recommended optional ones: LinkedIn alerts and label, Adzuna, RapidAPI, USAJOBS. Each key goes in `.env` locally and GitHub Actions Secrets. Checkpoint: `doctor --phase 2`, which tests each live.
3. The interview, which builds `profile.yaml`, in sections with a readback after each: about you; how postings are matched (title words versus department); what the role must have and must not, as a questionnaire with importance scales that map to points and dealbreakers; where and how; pay; industries, hard passes first; mission, culture and company appeal on importance scales; companies and lists (§13), including the conversation about where to look and the opt-ins; delivery, with the preferred arrival time as its own question. The candidate can share a job description or resume up front to pre-fill proposals. Output: `profile.yaml`. Checkpoint: `doctor --phase 3`.
4. Calibration, two read-only steps: `calibrate --titles` fetches live postings and shows which titles passed the gate and which did not, with no model calls, so the lists can be tuned first; then `calibrate` scores about fifteen and prints a ranked table with the criteria and modifiers that produced each score, and the candidate reacts to the order. Two or three passes is normal. Offline sample postings in `docs/samples/` exercise the mechanics when no source is connected yet.
5. Dry run: `digest --dry-run --from-calibrate --send` emails one digest to the candidate; then `cron --write`, commit, push, enable the workflows, start the first fetch by hand.

`doctor` is the checkpoint at every phase: it validates the profile, tests every credential it can reach, checks that the workflow schedules match the profile, and prints PASS, FAIL, SKIP or WARN per component with a one-line reason the candidate can act on. It never prints a secret.

**Environment variables:** `ANTHROPIC_API_KEY`, `GMAIL_ADDRESS`, `GMAIL_APP_PASSWORD`, `GOOGLE_SERVICE_ACCOUNT_JSON`, `GOOGLE_SHEET_ID`; optional `ADZUNA_APP_ID`, `ADZUNA_API_KEY`, `RAPIDAPI_KEY`, `USAJOBS_API_KEY`, `GOOGLE_DOC_ID`. `.env.example` is generated from one table in the code and says where each comes from and what it unlocks.

## 13. Company lists

The agent can only read a board it knows about, so the width of the net is the list of companies. Lists are CSV files in `data/company_lists/` with the columns `name,website,notes`, referenced from `sources.company_lists` with a tier (`dream` or `watch`) and optional filters (industry codes, states, names). The repository ships one starter list, `starter_companies.csv`, a snapshot with a build date, rebuilt by the maintainer and not kept current for users. It holds about 4,700 US companies listed on NYSE, Nasdaq or CBOE, built from SEC EDGAR filings (name, ticker, industry code, business-address state) with websites from Wikidata and verified lookups, plus a few thousand startups and growth-stage technology companies. Each row carries a segment (`public` or `startup`) and, where known, the industry code and state, so the profile can filter it: by segment first, then by industry, where SIC codes narrow the public companies only (startups carry none and are never tested by them), and optionally by state. The boards already located for these companies ship alongside it in `starter_boards.csv`, so no resolving is needed for them. Candidates should filter the starter list to their industries and regions: a board costs nothing to read, but every existing posting on it is scored once, and a plain careers page costs a model call on first read.

The candidate's own list, `my_companies.csv`, is where the net is widened for their field. The setup guide suggests sources for names: a Crunchbase export filtered to sector and stage, Fortune and Inc lists, Y Combinator's directory, Built In's company directory, Charity Navigator or Candid for nonprofits, USAspending or SAM.gov for federal contractors, industry association member lists, business journals' lists, and the similar-companies panel on LinkedIn company pages.

`python main.py resolve` turns a list into boards. It previews first (how many companies, how long, how many pages will need a paid first read in bespoke), then, per company: reads the careers page when a website is known and detects the vendor from its links; otherwise finds a website through a ladder (the list's own column, Wikidata by name, domain guesses, and, only when asked, a verified model guess) and probes vendors by slug. A company settles as soon as any vendor confirms. Results go to the files the engine already reads; companies on unsupported vendors are recorded as such; `corrections.csv` overrides any result and is the protection against false matches. The monthly workflow re-runs resolve and retries a batch of companies whose boards could not be checked.

## 14. Cost

- Model API: one scoring call per posting that passes the filters, about 1 cent each, plus bespoke's first read of each plain careers page (well under a cent), plus a few calls for the weekly summary. About $7 to $21 a month at a typical 20 to 60 scored postings a day. Conservative high: about $45 in the first month if the entire starter list is loaded unfiltered, because every board's existing postings are scored once; `scoring.max_calls_per_run` spreads that over the first week or two.
- GitHub Actions: three fetch runs and one digest run a day, each a few minutes, plus a monthly resolve run of up to a few hours. Within the free tier for a private repository.
- Adzuna, RapidAPI (JSearch) and USAJOBS have free tiers that cover this volume. Google Sheets and Gmail are free.

`calibrate` prints the measured cost per call for the candidate's model and profile, and `scoring.max_calls_per_run` caps a run so a large new list spreads its cost over days.

## 15. Limits

- Built and tested for the United States. The places file, the state logic and the USAJOBS source are US-specific. Another country needs the places file rebuilt from that country's GeoNames data (`tools/build_us_places.py` is the starting point) and `location.country` set to match; this has not been tried and the agent says so when asked.
- Delivery time depends on GitHub's scheduled-run queue. The schedule is sized so most digests arrive on time, not all.
- No source that requires a login (LinkedIn's site, Indeed, Wellfound from runner IPs). LinkedIn is covered through its alert emails.
- Unsupported applicant tracking systems (§5) are a known gap with work planned.

## 16. Glossary

- **Board**: a company's career listing on an applicant tracking system, fetchable without a browser.
- **Bespoke**: the crawler for careers pages with no board; it reads the page itself and extracts postings when the page changes.
- **Dealbreaker**: a profile setting that sends a posting to the Disqualified tab outright: a work model, a location tier, a pay rule, or a role criterion the posting plainly states.
- **Dream companies**: the candidate's named list; their boards are fetched directly and their roles reach the digest at a lower threshold.
- **Modifier**: an adjustment applied in code after the model's role-fit score: work model, location tier, compensation.
- **Parked**: a board or page that stopped working, kept with a reason and re-probed monthly.
- **Relo**: a hybrid or onsite role outside the commutable band; tagged in the digest and the Sheet.
- **Shard**: the portion of the board set one fetch run visits.
- **Title gate**: the cheap title test that decides whether a posting is worth a scoring call.
