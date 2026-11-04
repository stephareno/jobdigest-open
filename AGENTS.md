# AGENTS.md: brief for any coding agent working on this repository

Read this before changing anything. `CLAUDE.md` imports this file, so Claude Code, Codex and similar tools all read the same brief.

## What this is

An automated job search agent: fetches postings from company career boards, aggregators, LinkedIn alert emails and USAJOBS; filters cheaply; scores each posting with a language model against the owner's `profile.yaml`; applies modifiers in code for work model, location and pay; emails a daily digest and logs everything to a Google Sheet. `docs/PRD.md` is the source of truth for behavior. If the code and the PRD disagree, one of them is wrong; fix one and record it in `ROADMAP.md`.

## If there is no profile.yaml

The repository owner has not finished setup. Do not invent a profile. Open `docs/SETUP_GUIDE.md` and run it with them, phase by phase, as that file instructs. `python main.py doctor` tells you how far they got.

## Where everything lives: three parts, nothing in two places

- `profile.yaml`: identity and preferences. Who the candidate is, what roles, where, how much, which industries, which companies, when to deliver. Commented template: `profile.example.yaml`. Loaded and validated by `profile_loader.py`; every error names its field path.
- Environment variables: secrets and per-user resource IDs. `.env.example` lists them with where each comes from. Locally they come from `.env` (gitignored, loaded automatically); in GitHub Actions from repository secrets. Never anywhere else.
- Code: mechanism. How distance is computed, how duplicates are found, how prompts are assembled, how the schedule works.

The test for any value: would a different job seeker change this? If yes, it belongs in the profile, and code that hardcodes it is a bug. `profile_loader` imports nothing from the pipeline; everything imports from it.

## Commands

```
python main.py fetch [--daily-sources]   one shard of boards plus daily sources if due; score; queue
python main.py digest [--dry-run]        email and Sheet from the queue (dry-run writes HTML, changes nothing)
python main.py doctor [--phase N]        validate profile, test credentials, check schedule; the setup checkpoint
python main.py setup [--from-document f] terminal interview that writes profile.yaml
python main.py calibrate [--titles|--dir] title-gate check on live postings; score ~15; offline samples
python main.py resolve [--dry-run|--max] turn company lists into boards, tenants, careers pages
python main.py cron [--write]            schedule lines for the profile's timezone; --write edits the workflows
python main.py sheet-init                tabs and headers in a blank Sheet
python main.py profile-check             load, validate, summarize
```

## Non-negotiable rules

1. No credentials in code, ever. Environment variables only. Never print a secret; `doctor` reports whether a key works without showing it.
2. Filter location post-fetch on every source. Source APIs' own location filters are unreliable.
3. Strip tracking parameters before hashing URLs. Two dedup keys: URL hash, and normalized company + title + location checked against earlier runs, both before scoring. Two apply links on the same host under different posting ids are never merged.
4. Google Sheet columns are addressed by header name, never by index. Never create a column; write to an optional column only if its header exists.
5. Score 0 goes to the Disqualified tab, never the main index. Score 0 is reserved for hard blocks and dealbreakers (out-of-country postings, with the default `location.outside_country: disqualify`, are dropped before scoring and never logged); penalties never take a score below `scoring.min_score_after_penalties`.
6. Resolve apply links before storing: company site, then LinkedIn, then Indeed or ZipRecruiter, then aggregator (flagged).
7. A slow or failing source never blocks delivery. Every source runs under a time budget; a failed board yields nothing that run and is retried next time.
8. Boards are parked, never deleted. Parking happens through the monthly probe's reviewed pull request or a hand edit of `sourcing/parked_boards.csv`, never by the daily run.
9. Fetch and digest never write the same file. Fetch writes `seen_jobs.json`, `shard_state.json`, `bespoke_state.json`, `bespoke_cursor.json` and adds to `state/pending/`; digest writes `skip_state.json` and deletes the pending batches it sent.
10. State files are written by the workflows and never edited by hand. Do not edit `seen_jobs.json`, `skip_state.json`, `bespoke_state.json`, `shard_state.json` or `state/pending/`.
11. A scoring call that fails for billing or authentication aborts the fetch run before any posting is marked seen. Transient errors defer the posting up to `MAX_SCORING_DEFERRALS` times before recording it at score 0.
12. The title gate is a cost control, not a quality filter. A source is title-gated only when back-testing shows the gate loses no historical high-scoring roles from it. Never trade proven matches for noise reduction.
13. The model scores role fit only. Pay and location are modifiers applied in code afterward; never let the prompt weigh them.
14. The schedule is generated. `python main.py cron --write` writes one cron line per UTC offset the timezone uses, and the `slot` job decides which run is real. Never hand-edit a `schedule:` block.

## How things are built

- Prompts are templates in `prompts/`, rendered from the profile by `prompt_template.py`. Every rendered prompt has a golden file in `tests/golden/`; any wording change shows up as a golden diff, which is how prompt changes are reviewed. Regenerate deliberately with `UPDATE_GOLDEN=1 pytest tests/test_scorer_prompt.py`. A placeholder that cannot be filled stops the run; a prompt never goes out with a gap in it.
- Scoring: the model returns a base score, `criteria_hits` (which `scoring.role_criteria` applied), `function_fit` (`leads_department` / `in_department` / `outside_department`), industry, work model, city and state, and `hard_block_industry`. Code then applies, in order: work-model outcome, location tier, home bonus, compensation, dealbreaker criteria, function cap, floor and cap. Each outcome may be a number or `disqualify`.
- Location: `location.py` resolves "City, ST" strings against `data/us_places.tsv.gz` (GeoNames, offline). Centres, radii and penalties come from `location.*` in the profile. The places file covers one country, recorded in its sidecar; `location.country` must match it.
- Company lists: `data/company_lists/*.csv` (`name,website,notes`), referenced from `sources.company_lists` with a tier and optional filters. `resolve` turns them into boards (startup-tier CSV), Workday tenants and careers pages (`data/companies.yaml` `pinned:` or the bespoke list), or parked rows. `corrections.csv` overrides any result and is applied last; it is the protection against false matches.
- Sources: one module per source in `sources/`. `sources.enabled` in the profile decides what runs; a disabled source is never imported. A new ATS vendor needs a resolver pattern in `tools/resolve_ats.py` and a parser in `sources/ats.py`.
- Tests: `pytest` from the repo root. `conftest.py` forces `tests/fixtures/profile_test.yaml` for every test, so the suite is independent of whatever `profile.yaml` says; a real profile gets one validate-and-render test. Tests never read repo state files; they use `tmp_path` fixtures. New behavior comes with tests; a bug fix comes with the test that would have caught it.
- Logging: write log lines a person can debug from in the Actions log. Fail loudly with a descriptive error rather than swallowing an exception.

## Working conventions

- One task, one branch, one pull request. Branch from `origin/main`; name the branch for the roadmap ID.
- `ROADMAP.md` tracks work: an ID per item, statuses Planned / In flight / Live / Closed. "Live" means an ancestor of `origin/main`, verified with `git merge-base --is-ancestor <sha> origin/main`; a commit on a branch is never reported as shipped. Add the ID before starting; update the status in the same pull request.
- Commit messages: `feat:` new behavior or source; `fix:` what broke and why; `config:` profile or settings; `tune:` prompt or weight change; `chore:` maintenance; `test:` tests only. Titles under 72 characters.
- Stop and ask before anything destructive or irreversible that the task did not name: deleting files, force-pushing, editing state files, sending email, writing to the Sheet outside a normal run, setting or changing secrets.
- Report when done: what changed, what was verified (test counts, what you ran), the pull request, decisions you made that the owner should check, anything left undone.

## Where things live

```
main.py, cli.py           commands
profile_loader.py         load and validate the profile; imports nothing from the pipeline
prompt_template.py        renders prompts/*.txt from the profile
scorer.py                 the scoring call, cap, deferrals
location.py, places.py    tiers; places-file lookup
comp.py                   compensation modifier
pending.py, state.py      queue between fetch and digest; seen-job state; dedup keys
schedule.py               cron lines per UTC offset; slot guard
emailer.py, sheets.py     digest email; Google Sheet by header name
sheet_layout.py           the columns the engine expects
company_lists.py          list parsing, filters, tiers
websites.py, resolve.py   website ladder; resolve command
envfile.py                .env loading; the SECRETS table (drives .env.example and doctor)
sources/                  adzuna, jsearch, linkedin_email, builtin, ats, startups, watchlist, bespoke, usajobs, status_email, applied_doc
tools/                    resolve_ats, probe_parked_boards, build_us_places, build_public_companies, build_starter_companies, build_starter_boards, build_public_boards, export/
data/                     us_places.tsv.gz, company_lists/, companies.yaml, dream_companies.txt
sourcing/                 parked_boards.csv, bespoke lists, startup-tier CSV (written by resolve)
state/pending/            queued batches (workflow-written)
docs/                     PRD.md, SETUP_GUIDE.md; index.html (the Pages site) and assets/ (its images, the sample renders)
examples/postings/        twelve sample postings (calibrate --dir, digest --dry-run --from-calibrate)
tests/                    conftest forces the test profile; golden/ holds rendered prompts
```
