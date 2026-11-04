# Job Digest Agent: Setup Guide

This file is written for an AI assistant. If you are a person reading it, paste the whole file into a chat with Claude, ChatGPT or another assistant and say "help me set this up", or open the repository in Claude Code, Codex or a similar coding agent and say the same. The assistant takes it from there. You can also skip the assistant and run `python main.py setup`, which asks the same questions in the terminal.

## Instructions for the assistant

You are helping one person set up the Job Digest Agent: a program that runs on their own GitHub account and emails them a ranked list of job postings every morning. Your job is to get them from a fresh copy of the repository to a working first digest. Read this whole file before you say anything to them.

**Work out which mode you are in.** If you can run shell commands on their computer (you are inside a coding agent such as Claude Code or Codex), do each step yourself, show them what you did, and ask only when a step needs something only they have (an account login, a decision). If you cannot run commands (you are a chat assistant), either walk them through using a coding agent alongside you, or give them one command at a time, ask them to paste back what it printed, and read that before going on. Everything else in this guide is the same in both modes.

**Rules that hold in both modes.**

- One phase at a time. Finish a phase, run its checkpoint, read the result, then move on. Do not describe later phases in detail until you reach them.
- The checkpoint is `python main.py doctor --phase N`. Its last line reads `DOCTOR phase N: X pass, Y fail, Z skip, W warn`. A phase is done when there are no FAILs for anything the person has enabled. Trust the checkpoint over "yes, I did that".
- Never ask the person to paste a password, API key or service-account file into the chat. Keys go into their `.env` file and their GitHub secrets, and nowhere else. If you have shell access, do not print the contents of `.env` either. `doctor` reports whether a key works without showing it.
- If they say they already started, look for evidence of what phase they finished (the files present, `doctor` output) and confirm with them; only as a last resort ask which phase they finished. Then run that phase's checkpoint before continuing. Do not redo work the checkpoint confirms.
- Use their words. When you write the profile, the criteria and lists should read the way they said them, lightly tidied, not translated into your own vocabulary.
- When a step can go wrong in a known way, the note under that step says what to check. Read it before guessing.

**Time and cost, so you can set expectations.** Setup takes 60 to 90 minutes if they have none of the accounts, 20 if they have most. Running it costs about $7 to $21 a month in model API calls (one call per posting that passes the filters, about 1 cent each, typically 20 to 60 a day) and nothing else: GitHub Actions on a private repository, Gmail, Google Sheets, Adzuna, RapidAPI's free tier and USAJOBS are free at this volume. Conservative high: about $45 in the first month if they load the entire starter company list without filtering it, because every board's existing postings get scored once; a per-run cap spreads that over the first week or two. Say this plainly when the lists come up in phase 3.

## Phase 0: Orientation

Say this in your own words, briefly:

> The goal of this agent is that you no longer have to search for jobs. Relevant, recent postings arrive in your email every day, or whenever you choose. Each morning you get one email listing the open roles that fit you best, each with a score out of 10 and a line explaining why. Every posting the agent scores is also logged to a Google Sheet, so you can track all of them, their scores, where you applied and the status. This is a capable agent, and I will walk you through setup every step of the way. You do not need any coding experience, and you can ask about anything you don't understand at any time.
>
> The agent runs on your GitHub account, reads job boards and company career pages, scores each posting with an AI model using a profile we'll build together, and sends the digest from your Gmail. It never applies to anything and never contacts anyone.
>
> Five phases: (1) GitHub setup, your own copy of the code, (2) connect the accounts it needs, (3) an interview about the roles you want, which becomes your profile file, (4) calibrate the scoring on real postings, (5) a dry run, then switch it on.

Show them what the end result looks like: `docs/assets/sample_digest.png` in the repository is a digest built from sample postings for an example profile, and the repository README links to a page with the same image and a diagram of how the pieces fit. If you can display images, show it; otherwise give the path or link.

Then ask two questions and wait:

1. "Do you want a short walkthrough of how it works first, or go straight to setup?" If they want the walkthrough, explain the components (profile, sources, credentials, fetching, scoring, sending) and then start.
2. "Have you already started? If so, which phase did you finish?"

## Phase 1: GitHub setup

Why this phase matters: the agent runs as GitHub Actions in the person's own repository, with their own secrets. So they need a repository they own, not a clone of someone else's.

1. On the public repository page, click **Use this template**, then **Create a new repository**. Name it (`job-digest-agent` is fine), make it **Private**, create it. Say why it is "Use this template" and not Fork: a template copy has a clean history and no link back to the original; a fork would show as a fork forever and would pull the original's changes into their setup.
2. Turn the schedules off until setup is done. A template copy starts with its four workflows active, so their schedules would start running, and failing, before any key is in place. Repository → Actions tab → open each of `fetch`, `daily`, `weekly_summary` and `resolve_ats` → the `...` menu → **Disable workflow**. With the `gh` tool, from inside the clone (step 3): `gh workflow disable fetch.yml`, then the same for `daily.yml`, `weekly_summary.yml` and `resolve_ats.yml`. Phase 5 turns them back on.
3. Clone their new repository to their computer: `git clone <their repo URL>`, then `cd` into it.
4. Python 3.11 or later: `python3 --version`. If it is older, they need to install a newer Python before anything else.
5. Install the dependencies into a virtual environment: `python3 -m venv .venv && source .venv/bin/activate`, then `python -m pip install -r requirements.txt`. (`requirements.lock` has the same pins with hashes; the workflows and CI install that one, so leave it to them.) (A plain `pip install` outside a virtual environment is refused by Homebrew's Python and most recent Linux systems.) Every command in this guide runs inside it: in a new terminal, `cd` into the repository and `source .venv/bin/activate` first. That is also what makes the `python` in `python main.py ...` work on a Mac, which has no `python` command of its own.
6. Copy `.env.example` to `.env`. It stays empty for now. `.env` is already in `.gitignore`; confirm with `git check-ignore .env`, which should print `.env`.

Checkpoint: `python main.py doctor --phase 1`.

Known trouble: `git remote` reporting that it needs credentials is a WARN, not a failure, for a private repository; it means pushing later will need `gh auth login` or a credential helper. Everything else in phase 1 must pass.

## Phase 2: Accounts, keys, and where postings come from

Start with the shape of the sources, because it changes what they will bother setting up. There are three kinds:

- **Direct company boards, no key needed.** Most companies run their careers page on one of about twenty standard systems, and the agent reads those directly: Greenhouse, Lever, Ashby, SmartRecruiters, Workable, Rippling, Recruitee, BambooHR, Breezy, Teamtailor, Personio, Oracle HCM, ADP, UKG Ready, Gem, Comeet, Manatal, JobScore, and Workday. These are the best sources: fresh, complete, with a direct apply link. What they need is a list of companies to read, which is the subject of the interview's section H.
- **Aggregators and alerts, a key or a label each.** Adzuna and JSearch search across many sites and catch postings from companies not on any list, but they are noisier, their apply links go through an intermediary, and some of what they return is stale. LinkedIn alert emails are a good complement. USAJOBS covers US federal roles. Built In covers tech roles in a handful of metros.
- **The gap, stated plainly.** Some large employers use systems the agent cannot read yet: iCIMS, SuccessFactors, Phenom, ClearCompany, Pinpoint, and careers pages that only render with JavaScript. For those companies the agent tries a watchlist page read and a slower crawler ("bespoke"), which work for some and not others. Roles at those companies may only reach the digest through an aggregator or a LinkedIn alert. This is a known limitation with work planned; do not promise coverage there.

Say plainly what data goes where, before they connect anything. Scoring sends the text of their profile and of each posting to the model provider; the digest also sends text from each emailed company's own website, the bespoke crawler sends careers-page text when a page changes, and the weekly summary sends the notes on rows they marked Declined. The Google Sheet and Gmail are their own accounts. The LinkedIn reader opens one Gmail label, read-only. Status tracking, if they turn it on in section H, reads the last 14 days of their inbox, read-only, and sends the model only messages from someone other than themselves that name a company with a row in their Sheet. Searches go to the job sources they enable, and USAJOBS requires their Gmail address as its User-Agent. Nothing is sent anywhere else.

Then the keys. Every key goes in two places: `.env` on their computer (for running things locally during setup) and **GitHub Actions secrets** on their repository (for the scheduled runs). Same names in both. `.env.example` lists every name with where it comes from and what it unlocks. The commands read `.env` automatically when it is present.

Do the required ones first, then the recommended ones. Ask before each optional one.

**Required**

1. **Gmail app password** (`GMAIL_ADDRESS`, `GMAIL_APP_PASSWORD`). The account the digest is sent from, and the one LinkedIn alerts arrive in if they use that source. Google Account → Security → 2-Step Verification must be on → App passwords → create one named "job digest" → copy the 16-character password. A regular Gmail password will not work.
2. **Google Sheet and a service account** (`GOOGLE_SERVICE_ACCOUNT_JSON`, `GOOGLE_SHEET_ID`). This is the longest step; walk it slowly.
   - console.cloud.google.com → create a project (any name) → APIs & Services → Enable APIs → enable **Google Sheets API**.
   - IAM & Admin → Service Accounts → Create → any name → Done. Open it → Keys → Add key → JSON. A file downloads. That file's entire contents become `GOOGLE_SERVICE_ACCOUNT_JSON`.
   - Create a blank Google Sheet in their Drive. Share it with the service account's email address (it ends in `iam.gserviceaccount.com`; it is in the JSON file as `client_email`) as **Editor**. This is the step everyone misses; without it the agent can see nothing.
   - The Sheet ID is the long string in the Sheet's URL between `/d/` and `/edit`.
   - Put both values in `.env`: `GOOGLE_SHEET_ID=` the ID, and `GOOGLE_SERVICE_ACCOUNT_JSON=` the JSON file's whole contents on one line, wrapped in single quotes.
   - Then `python main.py sheet-init`, which creates the tabs and header row the agent expects. It refuses to touch a Sheet that already has data. It leaves the Sheet's original empty tab (`Sheet1`) alone; they can delete it.
   - Optional: also enable the **Google Drive API** in the same project. The agent does not need it, but with it `doctor` can confirm the service account is an Editor rather than reporting "write not verified".
3. **Model API key** (`ANTHROPIC_API_KEY`). console.anthropic.com → API keys → create. They need a small amount of prepaid credit on the account; $5 lasts weeks at this volume.

**Strongly recommended, one per source**

4. **LinkedIn job alerts** (no key; a Gmail label). Be clear about what this is and is not: the agent never touches linkedin.com. LinkedIn's terms prohibit automated access to their site, so the agent reads only the alert emails LinkedIn sends to the person's own inbox, which is their mail. Steps:
   - On LinkedIn, search for each target title (and "remote" variants), and on the results page turn on **Set alert** with email delivery, daily. Three to six alerts is typical.
   - In Gmail, create a filter: From contains `linkedin.com` and Subject contains `job` (LinkedIn uses several sender addresses, so filter on the domain). Action: apply a new label, for example `li-job-alerts`. Optionally skip the inbox.
   - Write the label name down; the interview asks for it. Unlocks `linkedin_email`.
5. **Adzuna** (`ADZUNA_APP_ID`, `ADZUNA_API_KEY`): developer.adzuna.com → register → both values are on the dashboard. Free. Unlocks `adzuna`.
6. **RapidAPI / JSearch** (`RAPIDAPI_KEY`): rapidapi.com → search "JSearch" → subscribe to the free Basic plan → the key is under your app's settings. Unlocks `jsearch`.
7. **USAJOBS** (`USAJOBS_API_KEY`): developer.usajobs.gov → Request API key, registered to the same email as `GMAIL_ADDRESS` (the agent identifies itself with that email, as USAJOBS requires). Arrives by email. Unlocks `usajobs`. US federal roles only.
8. **Index Doc** (`GOOGLE_DOC_ID`): only if they keep a Google Doc of roles they applied to and want the agent to mark those rows Applied. Enable the Google Docs API in the same Cloud project and share the Doc with the service account. Most people skip this.

**Put the keys in GitHub too.** Repository → Settings → Secrets and variables → Actions → New repository secret, one per name, same values as `.env`. With the `gh` tool: `gh secret set NAME` and paste the value when prompted. For the service account, `gh secret set GOOGLE_SERVICE_ACCOUNT_JSON < path/to/file.json`.

Checkpoint: `python main.py doctor --phase 2`. Every credential they set should PASS. Until phase 3 writes the profile, sources they skipped show FAIL for missing keys; note which ones they intend to skip and move on.

Known trouble: Gmail IMAP failing with the right app password usually means IMAP is off in Gmail settings (Settings → See all settings → Forwarding and POP/IMAP → Enable IMAP). Google Sheet "not found" usually means the Sheet was not shared with the service account email.

## Phase 3: The interview, which builds profile.yaml

This phase produces `profile.yaml`, the only file about them.

If they run `python main.py setup` instead of talking it through with you, it asks sections A to G and I with the example profile's answers as defaults, then writes `profile.yaml`, runs `profile-check` and `cron --write` itself. It does not ask section H: the company lists (`sources.company_lists`) stay as `profile.example.yaml` sets them, which is the example persona's starter-list filter (consumer, food, retail and manufacturing codes). Go through section H with them afterwards and edit `sources.company_lists` to their field. Ask section by section, in order. After each section, read back what you will write in two or three lines and get a yes before moving on. Recommend that they use dictation or voice mode so you get thorough answers, and say that this is the most important phase: the more detail they give, the better the scoring. Keep questions short, but getting a detailed picture comes first. At the end, write the whole file.

Open with an overview of the sections you will walk through, then ask: "If you have your current job description, a job posting or two you'd love to get, or your resume, share them now. I'll take a first pass at draft answers for you to correct." If they do, read them and pre-fill the sections below as proposals; still confirm each one.

**A. About you** → `candidate.*`
Name, pronouns, a one-line descriptor ("a mid-career finance leader", "an early-career data scientist"), a one-line headline of what they seek ("senior account executive at a B2B software company"), and three to five bullet lines of background the model should know when judging fit: years, department, scope, the kind of work they have led. Summarize a shared JD or resume into those lines; do not paste it whole.

**B. The role: how postings are matched** → `roles.*`
Explain first: titles are inconsistent across companies, so the agent uses two different checks. A cheap check on the **title** decides whether a posting is worth a scoring call at all (this keeps costs down; it is deliberately loose). Then the model reads the **description** and judges which department the role actually sits in. Each variable below says which of the two it feeds.

- `target_department` (judged from the description): the department or function the role must sit in, in one line. "Corporate IT", "FP&A", "clinical research operations". A role the model judges to sit outside it is capped at a low score, so this line matters. Ask for it in their words.
- `target_titles` (title): exact titles that always pass, whatever the other words. "CFO", "Head of Design", "Chief of Staff".
- `seniority_terms` (title): the level words. "Director", "VP", "Vice President", "Head of", "Senior Manager", "Principal". A title needs one of these.
- `function_terms` (title): the subject words. "IT", "Information Technology", "Digital Workplace", "Finance", "FP&A", "Accounting". A title needs one of these too. Make this list long: every way their field appears in a title, including abbreviations. Say that a posting only needs one word from each list, so a long list is not a strict filter; it is the net. Propose a list from their titles and department, then ask what is missing.
- `function_terms_never_alone` (title): words that only count inside a longer phrase. These depend on their field. "Enterprise", "digital", "operations", "business" are common ones because they appear in titles from every department: someone looking for "Enterprise Governance Manager" puts "Enterprise Governance" in `function_terms` and "enterprise" here, so that title passes while "Head of Enterprise Sales" does not. Ask which of their function terms are broad enough to need this.
- Mention that a wider net costs more scoring calls and a narrower one risks missing roles, and that phase 4 shows the effect on real titles so they can adjust.

**C. What the role must have, and must not** → `scoring.role_criteria`
This is the heart of the scoring and it is in their words. Say up front: this section is about the role and the job description only; pay, location and remote/hybrid/onsite come next and are handled separately, so leave them out here.

Go theme by theme. For each, ask how important it is on a scale of 1 to 5, then what specifically. Write each answer as one criterion: an `id`, a one-line `criterion` the model can recognize in a posting, and `points` from the importance (5 → +3, 4 → +2, 3 → +1; 1 or 2 means leave it out). For things they want to avoid, the same scale in reverse (3 → −1, 4 → −2, 5 → a dealbreaker, written with `dealbreaker: true` instead of points; a dealbreaker sends the role to the Disqualified tab). For each dealbreaker, also ask for the words a posting would use to say it, and write them as `evidence_terms` (for no team: "individual contributor", "no direct reports", "no team"). The agent applies a dealbreaker only when the model quotes a sentence that is really in the posting and contains one of those words, so a vague or missing phrase means the dealbreaker never fires. Three to eight criteria is typical. Themes:

1. **Scope and authority.** "How important is it that the role owns decisions? Which ones: budget, strategy, vendors, hiring?"
2. **Team and reporting line.** "Do you want to manage people? Is an individual-contributor role acceptable, a step down, or a dealbreaker? Who should the role report to?"
3. **Nature of the work.** "How hands-on do you want to be? Building something new versus running something that exists? Transformation versus steady state?"
4. **Tools, stack or domain alignment.** "Are there systems, tools or domains the role should be centered on? How much overlap do you need: 50 percent, 80, 100?"
5. **Conditions of the role.** "Travel, on-call, client-facing or billable work, shift work: any of these welcome, tolerated, or out?"
6. **Growth and stage.** "Company stage or size preference? A path to the next level?"
7. **Anything else that would make you pass, however good the rest looks.** Write these as dealbreakers.

Prompt with field-appropriate examples only when they stall. Read the list back with the points and confirm.

**D. Where and how** → `location.*`
- Country (`country`, default US; say that the shipped data supports the US and other countries need a rebuilt places file).
- Work models: for remote, hybrid and onsite, is each fine, a plus, a minus, or a dealbreaker? (`work_models`). Explain the default logic: a remote role only has to be in the country; hybrid and onsite roles are then judged by distance from home.
- Home metro as "City, ST" (`home`), and whether they want a bonus for roles close to home (`home_bonus`, radius).
- Metros they can commute to and the radius (`commutable`, `commutable_radius_miles`).
- Metros they would relocate to, and whether relocation is a small penalty, a large one, or a dealbreaker (`relocate`, `relocate_penalty`).
- Everywhere else in the country for hybrid/onsite: penalty or dealbreaker (`elsewhere_penalty`). Outside the country: dealbreaker is the default (`outside_country`).

**E. Pay** → `compensation.*`
Ask for the base salary range they are looking for and any context (total comp expectations, equity, what they earn now if they want to share it). Then set: the salary below which a role is a penalty or a dealbreaker (`floor`, `floor_adjustment`), the number at or above which it earns a bonus (`stretch`), and what to do when a posting states no pay at all (`unstated`, usually 0). Say that only pay stated in the posting counts; the agent never estimates.

**F. Industries** → `industries.*`
Ask for hard passes first, always: "Industries you would never work in, whatever the role." (`hard_block`, each with a name keyword or two so the agent can reject them without a scoring call). Then: industries to avoid (`deprioritize`), ones they like (`lean_in`), ones they love (`strong_yes`), and any that are simply neutral. Any list may be empty; lists have no length limit.

**G. Mission, culture and company appeal** → `scoring.mission_values`, `company_fit.*`
Three questions, each on the 1-to-5 scale, then specifics.

- **Mission.** "How important is it that the company's mission aligns with you? What should it align with?" 1 or 2: `mission_values.enabled: false`. 3: enabled with a cap of 1. 4 or 5: cap of 2. Their answer to "what" goes into `company_fit.mission_flavors` (default suggestions: patients, public goods, access, safety, science, civic life).
- **Culture and values.** "How important is how the company operates? What are you looking for?" Their answer goes into `company_fit.operating_values` (default suggestions: curiosity, transparency, people-first, builds for the long term, benefits a community beyond shareholders); offer the defaults and let them edit or shorten. Someone who rates this 1 or 2 gets an empty list.
- **Company appeal.** "Beyond the role and the industry, do you want the score to reward companies doing something interesting?" 1 or 2: `company_fit.enabled: false`. Otherwise enabled; offer the default signals (innovating in its field, real research, a novel product, mission-driven, doing good, a respected name) and let them edit. Ask for two or three example companies that feel right (`exemplars`).

**H. Companies and lists: where the agent looks** → `data/company_lists/`, `data/dream_companies.txt`, `sources.*`
This section decides how wide the net is. Spend time here. Say plainly:

> The best postings come from reading companies' own career boards, and the agent can only read a board it knows about. So it needs lists of companies. The repository ships one starter list: about 4,700 US public companies from SEC filings plus a few thousand startups and growth-stage technology companies, with their career boards already located, so there is nothing to resolve for them. It is a first pass. It is not kept up to date for you, and it does not know which companies matter in your field. You should filter it to your industries and regions, for two reasons: every existing posting on a board you add is scored once, which is where the first-month cost comes from, and an unfiltered list makes the digest about everyone's field instead of yours. The quality of what you see depends on the lists you give it: if the right companies are not on a list, their roles can only reach you through an aggregator or a LinkedIn alert, and sometimes not at all.

Then have a real conversation about where they want to look:

1. **Dream companies.** "Which companies would you most like to work for?" Any number, one per line → `data/dream_companies.txt`. Their roles reach the digest at a lower score.
2. **The field.** "Where do companies in your field tend to be: startups, large public companies, government contractors, nonprofits, universities, a particular region?" Use the answer to pick lists.
3. **Lists to use.** Offer each and explain what it covers:
   - The shipped starter list, filtered: by segment first (public companies, startups, or both), then by industry from section F. Industry filtering uses SIC codes, which only the public companies carry: startups are never tested by it, so choosing both segments keeps every startup plus the public companies in the chosen codes. Optionally by state. Walk through the filter with them and say roughly how many companies it keeps (the example profile's consumer, food, retail and manufacturing codes keep 528 public companies plus about 6,200 startups). Large public employers are also where the vendor gap from phase 2 bites.
   - Their own list, `data/company_lists/my_companies.csv`, name plus website. Suggest where to get names and say these are suggestions, not endorsements: a Crunchbase export filtered to their sector and stage (startups); Fortune 500 and 1000, Forbes and Inc 5000 lists (growth companies); Y Combinator's company directory; Built In's company directory by metro; Charity Navigator or Candid for nonprofits by rating; USAspending or SAM.gov for federal contractors; industry association member lists; local business journals' lists ("fastest growing", "best places to work"); the "similar companies" shown on LinkedIn company pages for each dream company.
   - Explain that after a list is in place, `python main.py resolve` finds each company's board: most resolve to a readable system, some become watchlist pages, some go to the bespoke crawler, and some cannot be read yet (the gap) and are parked and re-checked monthly. Report the counts to them.
4. **Bespoke.** Explain in three sentences: for a company on their lists whose careers page is not on any standard system, the bespoke crawler re-reads the page itself on a rolling schedule and has the model pull out new postings when the page changes. It only crawls companies from their lists, never the open web. It is slower, the first read of each page is a model call, and it works for plain pages but not for pages that need JavaScript; it is on by default and needs no key.
5. **The aggregators and alerts.** Based on phase 2, set `sources.enabled` for `adzuna`, `jsearch`, `linkedin_email`, `builtin`, `usajobs`. Record the LinkedIn label. For `usajobs`, the job series: 2210 is IT; for anything else, look it up with them on usajobs.gov (the series is the four-digit number in any federal posting's title). For `builtin`, say which metros have a Built In site; if theirs does not, it searches remote only.
6. **Inbox readers, opt-in.** Both default off. Describe each plainly and ask.
   - Application status tracking (`sources.status_tracking.enabled`): once a day the agent opens their inbox read-only and reads each message from the last 14 days, from any sender, skipping mail they sent themselves. A message whose subject or body names a company that has a row on the Sheet's main tab is sent to the model (sender, date, subject, and the body up to 20,000 characters, with up to 30 matching rows) to spot application-lifecycle emails (acknowledgements, interview requests, rejections) and advance the status of matching rows. Other messages are read to check for a company name and go no further. It never sends email and never creates rows; the model's replies are cached in `status_email_cache.json` in their repository. This is wider access than the LinkedIn label reader, which sees only that label. Only turn it on if they are comfortable with that.
   - Index Doc seeding (`sources.applied_doc.enabled`): only if they set `GOOGLE_DOC_ID` in phase 2. Each entry of that Doc goes to the model to read its company, title and date.

Close the section with: "The more that is connected, the better this works. Which of these do you want on today, and which do you want to come back to?" Write the answers.

**I. Delivery** → `delivery.*`
Ask each as its own question:
- Email address to send to (usually the same Gmail).
- Timezone (propose the system's; confirm).
- The local time they want the digest in their inbox (`digest_time_local`). Say the agent starts hours earlier to allow for GitHub's queue.
- Which days (`digest_days`).
- Which day the weekly summary goes out (`weekly_summary_day`, a day with no digest) and at what time.
- Whether a day with nothing new should send nothing (`skip_empty_days`, usually yes).

**Write the file.** Write `profile.yaml` from the answers, following the structure and comments of `profile.example.yaml`, and the company list files from section H. Then `python main.py profile-check`; fix anything it reports. Then `python main.py resolve` if any list was added (it takes a while for a long list; report the counts). Then the checkpoint.

Checkpoint: `python main.py doctor --phase 3`.

## Phase 4: Calibration

The profile is a first draft until they have seen it rank real postings. Two steps, both read-only.

1. `python main.py calibrate --titles`: fetches recent postings from the enabled fast sources (the aggregators and Built In) using the profile's queries and shows which titles passed the gate and which did not. The company boards from their lists (`ats`, `startups`, `watchlist`, `bespoke`) are slow and are read only when named: `python main.py calibrate --titles --sources ats` checks the gate on those too. No model calls. Read the two lists with them: "Is anything in the failed list a role you'd want? Is anything in the passed list obviously not?" Adjust `roles.*` and run it again until the gate looks right.
2. `python main.py calibrate`: scores the postings that passed (about 15, one model call each) and prints a ranked table with each posting's score, the criteria that fired, the modifiers applied and a one-line rationale, then a block naming which profile fields moved each posting. Ask them to react to the order, not the numbers: "Which of these would you open first? Which should not be here?" Translate their reaction into profile edits (a criterion's weight, a missing industry, a location radius), apply them, run again. Two or three passes is normal. Stop when the top five are roles they would actually apply to.

If no source is enabled yet or they are offline, `python main.py calibrate --dir examples/postings` scores twelve built-in sample postings instead; it checks that the mechanics work but says less about their preferences.

## Phase 5: Dry run, then switch on

1. `python main.py digest --dry-run --from-calibrate --send`: builds a real digest from the twelve built-in sample postings in `examples/postings`, scored under their profile (about ten model calls; it does not reuse phase 4's live postings), so they see the format, the sections and the pills, saves the HTML to `out/digest_dry_run.html`, and emails it to them with "[DRY RUN]" in the subject. Nothing is written to the Sheet. Ask them to confirm it arrived and that it looks right on whatever they read mail on.
2. `python main.py cron --write`: writes the schedule lines for their timezone into the two workflow files. Explain that it writes two lines per workflow for timezones with daylight saving and that the agent itself decides which one is the real run, so they never edit these for clock changes.
3. Commit and push: `git add -A && git commit -m "config: my profile, lists and schedule" && git push`. The profile and the company lists belong in their private repository; `.env` is ignored and must not be committed (check that `git status` shows no `.env`).
4. Enable the workflows turned off in phase 1: repository → Actions tab → if GitHub shows a banner asking to enable workflows for the repository, click it; then open each of `fetch`, `daily`, `weekly_summary` and `resolve_ats` and click **Enable workflow** if the button is there (`gh workflow enable fetch.yml` and so on).
5. Start the first fetch by hand so they do not wait a day: Actions → `fetch` → Run workflow. Watch the run. It should end green and the log's summary says how many postings were fetched, gated, scored and queued. A fetch that finds nothing new is normal on a second run the same day.
6. The first real digest arrives the next scheduled morning. Tell them the time to expect it and that GitHub's queue can add a couple of hours to a scheduled run.

Final checkpoint: `python main.py doctor` with no phase argument, all green except sources they disabled.

## After setup: what to tell them before you finish

- The weekly summary arrives on the day they chose and reads better after two or three weeks of data.
- In the Sheet, setting a row's Status to **Declined** teaches the weekly summary what they pass on. Rejected is for when the company passes; the agent sets that itself if status tracking is on.
- To change anything about what they want, they edit `profile.yaml` (every field is commented), run `python main.py profile-check`, commit and push. If they change timezone or digest time, `python main.py cron --write` too. To add companies, add them to a list and run `python main.py resolve`. For a bigger change, they can come back to this guide and redo phases 3 and 4.
- Boards that stop working are parked automatically and re-checked monthly; a monthly pull request shows what changed, and they merge it or not.
- If a digest does not arrive: Actions tab → the latest `daily` run → the log. The digest's banner also reports any fetch run that failed. `python main.py doctor` finds expired keys.
- To stop the agent: Actions tab → each workflow → Disable. Nothing else is needed.
