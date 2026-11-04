# Company lists

Each list is a UTF-8 CSV with a header row: `name,website,notes`. Only `name` is required; `website` and `notes` may be empty.
Lines starting with `#` are comments. Extra columns are kept: `segment`, `sic` and `state` feed the profile's filters, and a row with `ats` and `url` is a board the engine fetches as it is.
Name the lists in `profile.yaml` under `sources.company_lists`, in order, each with a `tier` (`dream` or `watch`) and an optional `filter` (`segment`, `sic`, `states`, `names`, `exclude_names`). Filter by segment first (`public`, `startup` or both), then by industry: `sic` narrows public companies only, because a startup row has no SIC code and is not tested by it, so `segment: [public, startup]` with `sic` keeps every startup and the public companies in those codes. A row without the column another filter reads (`segment`, `states`) fails that filter. A watch entry may also name `boards:`, a file of boards already found for that list; they are read first.
A company may appear in several lists; the first file in profile order sets its tier.
`python main.py resolve` prints a preview (companies to try per list, time, cost), asks before it starts, then finds a board, Workday tenant or careers page for each listed company that has none yet, and parks the rest with a reason. A company without a website goes through the website ladder first (Wikidata, domain guesses; `--guess-websites` adds one model call each). `--max N` works in batches: run it again to continue.

**Add your own lists.** `my_companies.csv` is yours to fill in (shipped empty), and any other CSV in this format works the same way.

## The shipped lists are snapshots

The lists below are built by the maintainer on the date in each file's first line (or, for the board files, the commit that changed them). They are a starting point, not a feed: they are not kept current for users, and companies, websites and boards drift after the build date. Rebuild them with the tools named, or add your own lists.

- `starter_companies.csv`: the one starter list, `name,website,notes,segment,ticker,sic,sic_description,state,outcome`, public companies first. `segment` is `public` (US companies listed on NYSE, Nasdaq or CBOE, from SEC EDGAR and Wikidata, both public domain / CC0; `state` is the business-address state) or `startup` (the companies behind the maintainer's resolved startup-tier boards; no ticker, SIC or state). Every website was checked to name the company before it was kept. `outcome` is the maintainer's resolve result per company (`board`, `workday`, `careers_page`, `unsupported:<vendor>`, `none`, `none/workable_unchecked` = no website and Workable not probed, because it blocked the maintainer's address; empty or `untried` = not settled yet, so `resolve` tries it). `resolve` skips any row with a settled outcome; `resolve --retry-unchecked --list data/company_lists/starter_companies.csv` probes Workable for the `none/workable_unchecked` rows only and writes the answers back here. Four companies are in both segments, one row each.
- `starter_boards.csv`: the boards, Workday tenants and careers pages already found for every company in `starter_companies.csv` (`name,ats,url,website`): the startup rows first, then the public rows (careers pages carry ats `careers_page` and are kept for coverage, not fetched as boards). A board two rows share is written once. Name it as the starter list's `boards:` file.
- `corrections.csv`: hand corrections, `name,field,value,reason`, where `field` is `website`, `board` (value `<ats> <url>`, empty to remove it) or `outcome`. Applied last by the build tools and `resolve`, so a correction survives every rebuild.

Rebuilding (maintainer):

- Startup segment: `python tools/build_starter_boards.py` rewrites the startup rows of both files from `sourcing/startups_ats_resolved.csv`.
- Public segment: `GMAIL_ADDRESS=... python tools/build_public_companies.py --out /tmp/public.csv`, then `python tools/build_starter_companies.py --public /tmp/public.csv` replaces the public rows of `starter_companies.csv`, keeping each company's `outcome`.
- Public boards: `python tools/build_public_boards.py --workdir DIR` after a `resolve --workdir DIR` run over the public segment rewrites the public rows of `starter_boards.csv` and the public `outcome` column. `--check-guessed --list /tmp/public.csv` reports shipped boards on a guessed domain that fail the stricter name check (the shipped list does not carry `website_source`, the builder's output does).
