"""PUB-07a: cut the public template from this repo.

    python tools/export/export_public.py --out <dir> [--check] [--deny VALUE ...] [--repo OWNER/NAME]

Copies the files tools/export/allowlist.txt names from the committed tree
(git HEAD, so nothing untracked or gitignored can ship) into an empty <dir>,
then rewrites what must differ in the public version:

  - header-only skeletons for the sourcing CSVs and data/companies.yaml, so a
    fresh install starts with no companies;
  - docs/public/ (where the root README.md and AGENTS.md are staged) never
    ships; --check fails on any file under it and on any two shipped files
    with identical content (PUB-20);
  - ROADMAP.md reduced to its framework (IDs, statuses, chat names, the Live
    rule) with an empty index; LICENSE (MIT); CLAUDE.md as "@AGENTS.md";
    AGENTS.md and README.md from docs/public/, or placeholders;
  - tests/: every test file that imports a module the export does not ship is
    dropped; the suite and its golden files run on tests/fixtures/profile_test.yaml;
  - workflow schedule blocks regenerated for the example profile's timezone
    (python main.py cron --write).

It writes the manifest (every file, its size, the source commit) next to the
export, as <dir>.EXPORT_MANIFEST.txt beside <dir>, never inside it: the manifest
is a local record and does not ship (PUB-07c).

PUB-15: the public docs link to the repository as OWNER/REPO. --repo OWNER/NAME
(default stephareno/jobdigest-open, the public repository: PUB-07b) replaces
every OWNER/REPO in the export's non-Python files with it, and the scrub does
not count that address as a hit. No file names the repository any other way.

--check then proves the result: a fresh venv in <dir> runs the full suite with
JOB_DIGEST_PROFILE=profile.example.yaml and nothing else in the environment
(tests/conftest.py then puts the suite on its test profile), no test is skipped
for a reason naming a profile (a skip is not a pass), a denylist scan finds none
of the maintainer's values, and gitleaks finds no secrets. Any failure exits 1
with the file and line.

The denylist is read at run time, never written here: the candidate's name,
short name, email addresses, employer and LinkedIn label from the committed
profile.yaml, the repo owner and name from the origin remote, GOOGLE_DOC_ID and
GOOGLE_SHEET_ID if set (a WARN line says when one is not, so it was not scanned
for), and any --deny values.
"""

from __future__ import annotations

import argparse
import ast
import datetime
import fnmatch
import io
import os
import re
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
ALLOWLIST = Path(__file__).resolve().parent / "allowlist.txt"
EXAMPLE_PROFILE = "profile.example.yaml"
MANIFEST = "EXPORT_MANIFEST.txt"   # suffix of the file written beside <dir>, never inside it

# A safety net, not the selection: if the allowlist ever matches one of these,
# the export stops rather than ship it.
NEVER = ["profile.yaml", "*state*.json", "state/*", "seen_jobs.json", "bespoke_cursor.json",
         "service-account.json", ".env", ".env.*", "connections.csv", "connections.py", "*.docx",
         "ROADMAP.md", ".claude/*", "chat-export-*", "Claude outputs/*", "*__pycache__*", ".venv/*",
         "out/*", "sourcing/*.csv", "sourcing/*.csv.gz", "data/dream_companies.txt",
         "data/companies.yaml", "company_profiles.json", "status_email_cache.json",
         "docs/public/*"]   # PUB-20: staged copies of the root README.md and AGENTS.md

# Header-only CSVs written in place of the maintainer's, header read from the
# committed file so it always matches what the code expects.
SKELETON_CSVS = ["sourcing/parked_boards.csv", "sourcing/bespoke_dropped.csv",
                 "sourcing/startups_unresolved.csv"]   # the last is the bespoke source list

# Exactly what tools/resolve_ats.py writes for an empty set, so a rewrite is a no-op.
COMPANIES_YAML = """companies: []
unresolved: []
pinned: {}
"""

MIT = """MIT License

Copyright (c) {year} {holder}

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
"""

# Incidental personal values in shipped files, rewritten in the export only:
# (path glob, text with {owner}/{short} filled in at run time, replacement).
# Each must match at least once in a file that ships, or the export stops, so
# a rewrite that no longer applies is noticed rather than silently dropped.
# None at present: PUB-07b took the repository URL out of resolve_ats.py's
# User-Agent at the source.
REWRITES = []

ROADMAP_EXAMPLE_ROW = ("| `SRC-01` | Example: one line on what the feature does; once merged, "
                       "the PR and merge commit | Planned | `SRC-01 Short description` |")

PLACEHOLDER = "# {name}\n\nTo be written (PUB-07: the public docs item owes this file).\n"

# PUB-15: the public repository's address in the docs until it exists.
REPO_PLACEHOLDER = "OWNER/REPO"
PAGES_PLACEHOLDER = "OWNER.github.io/REPO"   # PUB-22: the project page address
GUIDE = "docs/SETUP_GUIDE.md"      # the setup guide (PUB-15), with docs/public/ the docs a label example may appear in
REPO_RE = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?/[A-Za-z0-9._-]+")


def log(msg: str) -> None:
    print(f"[Export] {msg}", flush=True)


def git(*args: str, binary: bool = False):
    out = subprocess.run(["git", "-C", str(ROOT), *args], check=True, capture_output=True)
    return out.stdout if binary else out.stdout.decode()


def show(path: str) -> bytes:
    """A file as committed at HEAD."""
    return git("show", f"HEAD:{path}", binary=True)


# --- The allowlist -------------------------------------------------------------


def read_allowlist(text: str) -> list[str]:
    lines = []
    for raw in text.splitlines():
        line = raw.strip()
        if line and not line.startswith("#"):
            lines.append(line)
    return lines


def matches(pattern: str, path: str) -> bool:
    """`dir/` matches everything under dir; otherwise a glob whose * never
    crosses a "/"."""
    if pattern.endswith("/"):
        return path.startswith(pattern)
    parts, pat = path.split("/"), pattern.split("/")
    return len(parts) == len(pat) and all(fnmatch.fnmatchcase(p, q) for p, q in zip(parts, pat))


def select(patterns: list[str], tracked: list[str]) -> tuple[list[str], list[str]]:
    """(files to ship, allowlist lines that matched nothing). Lines apply in
    order; a !line removes what earlier lines added."""
    chosen: set[str] = set()
    unmatched = []
    for pattern in patterns:
        negate = pattern.startswith("!")
        pat = pattern[1:] if negate else pattern
        hits = {p for p in tracked if matches(pat, p)}
        if not hits:
            unmatched.append(pattern)
        chosen = chosen - hits if negate else chosen | hits
    return sorted(chosen), unmatched


def never_violations(files: list[str]) -> list[str]:
    # .env.example is the one .env* file that ships: names and help text, no values.
    return [f for f in files if f != ".env.example" and any(fnmatch.fnmatchcase(f, n) for n in NEVER)]


# --- Tests that cannot run in the export -------------------------------------


def _module_files(name: str) -> list[str]:
    """Candidate repo paths for a dotted module name, from the root and from the
    directories tests put on sys.path."""
    rel = name.replace(".", "/")
    out = []
    for base in ("", "tools/", "sourcing/", "tests/"):
        out += [f"{base}{rel}.py", f"{base}{rel}/__init__.py"]
    return out


def _is_package_dir(name: str, files: set[str]) -> bool:
    prefix = name.replace(".", "/") + "/"
    return any(f.startswith(prefix) for f in files)


def imports_of(source: str) -> tuple[set[str], set[str]]:
    """(module names, .py files) a test file imports or loads: import
    statements, importlib.import_module("name"), a "dir/x.py" string (a repo
    path, matched exactly) and ROOT / ... / "x.py" (matched by file name). A
    bare "x.py" string anywhere else is data, not a load."""
    modules, pyfiles = set(), set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            modules |= {a.name for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            modules.add(node.module)
            modules |= {f"{node.module}.{a.name}" for a in node.names}
        elif (isinstance(node, ast.Call) and getattr(node.func, "attr", None) == "import_module"
              and node.args and isinstance(node.args[0], ast.Constant)
              and isinstance(node.args[0].value, str)):
            modules.add(node.args[0].value)
        elif isinstance(node, ast.Constant) and isinstance(node.value, str) \
                and re.fullmatch(r"[\w-]+(/[\w-]+)+\.py", node.value):
            pyfiles.add(node.value)                     # "sourcing/x.py": a repo path
        elif isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div) \
                and isinstance(node.right, ast.Constant) and isinstance(node.right.value, str) \
                and node.right.value.endswith(".py"):
            pyfiles.add(node.right.value)              # ROOT / "tools" / "x.py": matched by name
    return modules, pyfiles


def missing_imports(source: str, source_files: set[str], export_files: set[str]) -> list[str]:
    """Repo modules (or .py files) the test uses that exist in the source tree
    but not in the export. Third-party and stdlib names resolve to neither and
    are ignored; so is `from pkg import name` where name is not a module."""
    modules, pyfiles = imports_of(source)
    missing = []
    for mod in sorted(modules):
        in_source = any(p in source_files for p in _module_files(mod))
        in_export = any(p in export_files for p in _module_files(mod)) or _is_package_dir(mod, export_files)
        if in_source and not in_export:
            missing.append(mod)
    source_names = {f.rsplit("/", 1)[-1] for f in source_files}
    export_names = {f.rsplit("/", 1)[-1] for f in export_files}
    for f in sorted(pyfiles):
        if "/" in f and f in source_files and f not in export_files:
            missing.append(f)
        elif "/" not in f and f in source_names and f not in export_names:
            missing.append(f)
    return missing


def drop_unrunnable_tests(out: Path, source_files: set[str]) -> dict[str, list[str]]:
    """Remove test files that import a module the export lacks; repeat until
    nothing changes (a test may import another test module)."""
    excluded: dict[str, list[str]] = {}
    while True:
        export_files = set(list_files(out))
        newly = {}
        for rel in sorted(export_files):
            if rel.startswith("tests/") and rel.endswith(".py"):
                gone = missing_imports((out / rel).read_text(), source_files, export_files)
                if gone:
                    newly[rel] = gone
        if not newly:
            return excluded
        for rel, gone in newly.items():
            (out / rel).unlink()
            excluded[rel] = gone


# --- Generated files -------------------------------------------------------------


def roadmap_framework(source: str, repo_name: str) -> str:
    """The header of the source roadmap from "## How IDs work" up to the
    index, plus an empty index with one example row."""
    start, index = source.find("## How IDs work"), source.find("## Index")
    if start < 0 or index < start:
        raise SystemExit("ROADMAP.md: cannot find '## How IDs work' ... '## Index'; update export_public.py")
    body = source[start:index]
    # The repo name belongs to this repo; the prefix row stays, generic.
    body = re.sub(r"\s*\(" + re.escape(repo_name) + r" only\)", "", body)
    # The Live rule stays; the story of why it exists is history.
    body = re.sub(r"\nA branch commit changes nothing.*?\n\n", "\n\n", body, flags=re.S)
    return ("# Roadmap\n\n"
            "Source of truth for feature status and chat naming: this file owns **what\n"
            "state each piece of work is in** and **what the chat about it is called**.\n\n"
            + body
            + "## Index\n\n| ID | Feature | Status | Chat name |\n|---|---|---|---|\n"
            + ROADMAP_EXAMPLE_ROW + "\n")


def apply_rewrites(out: Path, values: dict[str, str]) -> list[str]:
    notes = []
    for glob, old, new in REWRITES:
        old = old.format(**values)
        paths = [p for p in list_files(out) if matches(glob, p)]
        hits = 0
        for rel in paths:
            text = (out / rel).read_text()
            if old in text:
                hits += text.count(old)
                (out / rel).write_text(text.replace(old, new))
        if paths and not hits:
            raise SystemExit(f"rewrite for {glob} matched nothing; update REWRITES in export_public.py")
        if hits:
            notes.append(f"rewrote {hits} occurrence(s) in {glob}")
    return notes


def write_generated(out: Path, tracked: set[str], repo_name: str, holder: str) -> list[str]:
    """Write every file the export generates; returns notes for the report."""
    notes = []
    for rel in SKELETON_CSVS:
        header = show(rel).decode().splitlines()[0]
        (out / rel).parent.mkdir(parents=True, exist_ok=True)
        (out / rel).write_text(header + "\n")
    (out / "data" / "companies.yaml").write_text(COMPANIES_YAML)
    (out / "ROADMAP.md").write_text(roadmap_framework(show("ROADMAP.md").decode(), repo_name))
    if "LICENSE" in tracked:
        (out / "LICENSE").write_bytes(show("LICENSE"))
        notes.append("LICENSE copied from the repo")
    else:
        (out / "LICENSE").write_text(MIT.format(year=datetime.date.today().year, holder=holder))
        notes.append(f"LICENSE: MIT, holder '{holder}' (the one personal value that ships, by design)")
    (out / "CLAUDE.md").write_text("@AGENTS.md\n")
    for name in ("AGENTS.md", "README.md"):
        src = f"docs/public/{name}"
        if src in tracked:
            (out / name).write_bytes(show(src))
            notes.append(f"{name} copied from {src}")
        else:
            (out / name).write_text(PLACEHOLDER.format(name=name))
            notes.append(f"{name}: placeholder written; the docs item still owes docs/public/{name}")
    return notes


def _placeholder_files(out: Path) -> list[str]:
    """Every shipped file but Python source: the code that handles the
    placeholder names it as a literal, and must keep it."""
    return [rel for rel in list_files(out) if not rel.endswith(".py")]


def fill_repo(out: Path, repo: str) -> int:
    """Replace OWNER/REPO with the public repository, and OWNER.github.io/REPO with
    its project page, in every non-Python file; returns the count."""
    owner, name = repo.split("/")
    pairs = [(PAGES_PLACEHOLDER, f"{owner}.github.io/{name}"), (REPO_PLACEHOLDER, repo)]
    n = 0
    for rel in _placeholder_files(out):
        data = (out / rel).read_bytes()
        new = data
        for old, value in pairs:
            n += new.count(old.encode())
            new = new.replace(old.encode(), value.encode())
        if new != data:
            (out / rel).write_bytes(new)
    return n


def repo_addresses(repo: str | None) -> tuple[str, ...]:
    """The text fill_repo writes, which the scrub does not count: the repository
    and its project page."""
    if not repo:
        return ()
    owner, name = repo.split("/")
    return (f"{owner}.github.io/{name}", repo)


def check_placeholders(out: Path, repo: str | None) -> list[str]:
    """The check's failures for OWNER/REPO: with --repo set, none may be left."""
    if not repo:
        return []
    left = placeholder_hits(out)
    for hit in left:
        print(f"  {hit}: {REPO_PLACEHOLDER} survived --repo")
    log(f"check: placeholders: {len(left)} {REPO_PLACEHOLDER} left")
    return [f"{len(left)} {REPO_PLACEHOLDER} placeholder(s) survived --repo {repo}"] if left else []


def placeholder_hits(out: Path) -> list[str]:
    """file:line for every OWNER/REPO or OWNER.github.io/REPO left in the export's non-Python files."""
    hits = []
    for rel in _placeholder_files(out):
        text = (out / rel).read_bytes().decode("utf-8", errors="replace")
        for n, line in enumerate(text.splitlines(), 1):
            if REPO_PLACEHOLDER in line or PAGES_PLACEHOLDER in line:
                hits.append(f"{rel}:{n}")
    return hits


# --- Steps run inside the export ------------------------------------------------


def export_env(out: Path, **extra: str) -> dict[str, str]:
    """Only what the step needs: the example profile, a PATH, a HOME inside
    the export's scratch space, and no bytecode files left behind."""
    home = out / ".export_home"
    home.mkdir(exist_ok=True)
    return {"JOB_DIGEST_PROFILE": str(out / EXAMPLE_PROFILE), "PATH": "/usr/bin:/bin",
            "HOME": str(home), "PYTHONDONTWRITEBYTECODE": "1", **extra}


def run(cmd: list[str], cwd: Path, env: dict[str, str], label: str) -> subprocess.CompletedProcess:
    result = subprocess.run(cmd, cwd=cwd, env=env, capture_output=True, text=True)
    if result.returncode != 0:
        raise SystemExit(f"{label} failed (exit {result.returncode}):\n{result.stdout}\n{result.stderr}")
    return result


def regenerate(out: Path) -> None:
    env = export_env(out)
    log("schedule: python main.py cron --write against the example profile")
    print(run([sys.executable, "main.py", "cron", "--write"], out, env, "cron --write").stdout.rstrip())


def clean_scratch(out: Path) -> None:
    for name in (".export_home", ".venv", ".pytest_cache", ".git"):
        shutil.rmtree(out / name, ignore_errors=True)
    for cache in out.rglob("__pycache__"):
        shutil.rmtree(cache, ignore_errors=True)


def list_files(out: Path) -> list[str]:
    skip = {".export_home", ".venv", ".pytest_cache", ".git"}
    files = []
    for p in out.rglob("*"):
        rel = p.relative_to(out)
        if p.is_file() and rel.parts[0] not in skip and "__pycache__" not in rel.parts:
            files.append(rel.as_posix())
    return sorted(files)


def manifest_path(out: Path) -> Path:
    """The manifest's place: beside the export directory, so it never ships."""
    return out.parent / f"{out.name}.{MANIFEST}"


def write_manifest(out: Path, sha: str) -> tuple[int, int]:
    files = list_files(out)
    sizes = [(out / f).stat().st_size for f in files]
    lines = [f"# Public export of commit {sha}",
             f"# {len(files)} files, {sum(sizes)} bytes. Columns: size in bytes, path.", ""]
    lines += [f"{size:>10}  {f}" for f, size in zip(files, sizes)]
    manifest_path(out).write_text("\n".join(lines) + "\n")
    return len(files), sum(sizes)


# --- --check -------------------------------------------------------------------------


def layout_violations(files: list[str]) -> list[str]:
    """PUB-20: shipped paths that break the public layout (docs/public/ is
    v2-only staging; its two files ship at the root)."""
    return [f for f in files if f.startswith("docs/public/")]


def duplicate_files(out: Path, files: list[str]) -> list[tuple[str, str]]:
    """PUB-20: every pair of shipped files with identical content, each pair
    (first path, later path) in path order."""
    import hashlib
    first: dict[str, str] = {}
    pairs = []
    for rel in sorted(files):
        digest = hashlib.sha256((out / rel).read_bytes()).hexdigest()
        if digest in first:
            pairs.append((first[digest], rel))
        else:
            first[digest] = rel
    return pairs



def pytest_pin() -> str:
    for line in show("tools/requirements.txt").decode().splitlines():
        if line.strip().startswith("pytest=="):
            return line.strip()
    return "pytest"


def profile_skips(output: str) -> list[str]:
    """pytest -rs "SKIPPED [n] file:line: reason" lines whose reason names a profile."""
    return [line for line in output.splitlines()
            if line.startswith("SKIPPED") and "profile" in line.split(": ", 1)[-1].casefold()]


def run_suite(out: Path) -> tuple[bool, str, list[str]]:
    """Fresh venv, requirements plus pytest, full suite, example profile only."""
    venv = out / ".venv"
    log(f"check: fresh venv at {venv}")
    run([sys.executable, "-m", "venv", str(venv)], out, export_env(out), "venv")
    py = str(venv / "bin" / "python")
    run([py, "-m", "pip", "install", "-q", "--disable-pip-version-check", "-r", "requirements.txt",
         pytest_pin()], out, export_env(out), "pip install")
    # Published, this tree is a git repo, and tests ask git about it.
    run(["git", "init", "-q"], out, export_env(out), "git init")
    log("check: full suite with JOB_DIGEST_PROFILE=profile.example.yaml")
    result = subprocess.run([py, "-m", "pytest", "-q", "-rfEs", "-p", "no:cacheprovider"], cwd=out,
                            env=export_env(out), capture_output=True, text=True)
    summary = (result.stdout.strip().splitlines() or ["(no output)"])[-1]
    if result.returncode != 0:
        print(result.stdout[-20000:], result.stderr[-5000:], sep="\n")
    return result.returncode == 0, summary, profile_skips(result.stdout)


def denylist(extra: list[str]) -> list[tuple[str, str, frozenset[str]]]:
    """[(label, value, files exempt)]."""
    profile = yaml.safe_load(show("profile.yaml"))
    cand = profile.get("candidate") or {}
    raw = show("profile.yaml").decode()
    licence = frozenset({"LICENSE"})
    example_ok = frozenset({EXAMPLE_PROFILE})
    items = [("candidate name", cand.get("display_name", ""), licence),
             ("short name", cand.get("short_name", ""), licence)]
    for email in sorted(set(re.findall(r"[\w.+-]+@[\w-]+\.[\w.-]+", raw))):
        items.append(("email", email, frozenset()))
    for employer in sorted(set(re.findall(r"\bat ([A-Z][\w&-]*(?:\.[a-z]{2,})?)", cand.get("background", "")))):
        items.append(("employer", employer, frozenset()))
        if "." in employer:
            items.append(("employer", employer.split(".")[0], frozenset()))
    for label in re.findall(r"linkedin_label:\s*([\w-]+)", raw):
        items.append(("LinkedIn label", label, example_ok))
    owner, repo = origin_owner_repo()
    items += [("repo owner", owner, licence), ("repo name", repo, licence)]
    for var in ("GOOGLE_DOC_ID", "GOOGLE_SHEET_ID"):
        if os.environ.get(var):
            items.append((var, os.environ[var], frozenset()))
    items += [("--deny", v, frozenset()) for v in extra]
    return [(l, v, e) for l, v, e in items if v]


def unscanned(environ) -> list[str]:
    """The resource IDs the scrub cannot look for, because they are not set."""
    return [var for var in ("GOOGLE_DOC_ID", "GOOGLE_SHEET_ID") if not environ.get(var)]


# PUB-22: the maintainer's name may appear in LICENSE and in exactly two blocks:
# the README's "## About" section (up to, not including, "## License") and the
# page's "From the author" card. The exemption matches the blocks, not the files,
# so the name anywhere else in README.md or docs/index.html is still a hit.
AUTHOR_LABELS = frozenset({"candidate name", "short name"})
AUTHOR_BLOCKS = {
    # path: (start line, end line, end line is part of the block)
    "README.md": (re.compile(r"^## About\s*$"), re.compile(r"^## License\b"), False),
    "docs/index.html": (re.compile(r'^<section id="from-the-author">\s*$'), re.compile(r"^</section>\s*$"), True),
}


def author_block(rel: str, lines: list[str]) -> set[int]:
    """1-based line numbers of rel's About block; empty when rel has none or the
    block is not closed (then the name in it is a hit, as it should be)."""
    if rel not in AUTHOR_BLOCKS:
        return set()
    start_rx, end_rx, inclusive = AUTHOR_BLOCKS[rel]
    start = next((n for n, line in enumerate(lines, 1) if start_rx.search(line)), None)
    if start is None:
        return set()
    end = next((n for n, line in enumerate(lines, 1) if n > start and end_rx.search(line)), None)
    if end is None:
        return set()
    return set(range(start, end + 1 if inclusive else end))


def scrub(out: Path, items, allowed: tuple[str, ...] = ()) -> list[str]:
    """file:line: label 'value' for every hit. Whole-word, any case. The guide
    (docs/public/ and docs/SETUP_GUIDE.md) may name the LinkedIn label as an example. Text in
    `allowed` (the --repo address) is removed from a line before it is scanned. The
    maintainer's name is allowed inside author_block() (PUB-22)."""
    hits = []
    regexes = [(label, value, exempt,
                re.compile(rf"(?<![A-Za-z0-9]){re.escape(value)}(?![A-Za-z0-9])", re.I))
               for label, value, exempt in items]
    for rel in list_files(out):
        text = (out / rel).read_bytes().decode("utf-8", errors="replace")
        lines = text.splitlines()
        for allow in allowed:
            lines = [line.replace(allow, "") for line in lines]
        about = author_block(rel, lines)
        for label, value, exempt, rx in regexes:
            if rel in exempt or (label == "LinkedIn label" and (rel.startswith("docs/public/") or rel == GUIDE)):
                continue
            if rx.search(rel):
                hits.append(f"{rel}: {label} '{value}' in the path")
            for n, line in enumerate(lines, 1):
                if label in AUTHOR_LABELS and n in about:
                    continue
                if rx.search(line):
                    hits.append(f"{rel}:{n}: {label} '{value}'")
    return hits


def gitleaks(out: Path) -> tuple[bool, str]:
    exe = shutil.which("gitleaks") or next((p for p in ("/opt/homebrew/bin/gitleaks", "/usr/local/bin/gitleaks")
                                            if os.path.exists(p)), None)
    if not exe:
        return False, "gitleaks is not installed (brew install gitleaks)"
    report = out.parent / f".{out.name}-gitleaks.json"
    help_text = subprocess.run([exe, "--help"], capture_output=True, text=True).stdout
    if re.search(r"^\s+dir\s", help_text, re.M):
        cmd = [exe, "dir", str(out)]
    else:
        cmd = [exe, "detect", "--no-git", "--source", str(out)]
    cmd += ["--no-banner", "--redact", "--report-format", "json", "--report-path", str(report)]
    result = subprocess.run(cmd, capture_output=True, text=True)
    version = subprocess.run([exe, "version"], capture_output=True, text=True).stdout.strip()
    detail = f"gitleaks {version}: " + (result.stderr.strip().splitlines() or ["(no output)"])[-1]
    if result.returncode != 0:
        detail += "\n" + report.read_text() if report.exists() else ""
    report.unlink(missing_ok=True)
    return result.returncode == 0, detail


def origin_owner_repo() -> tuple[str, str]:
    url = git("remote", "get-url", "origin").strip()
    return re.search(r"[:/]([^/:]+)/([^/]+?)(?:\.git)?$", url).groups()


# --- Main ------------------------------------------------------------------------------


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--out", required=True, type=Path, help="an empty or absent directory")
    ap.add_argument("--check", action="store_true", help="run the suite, the scrub and gitleaks")
    ap.add_argument("--deny", action="append", default=[], help="another value that must not ship")
    ap.add_argument("--repo", default="stephareno/jobdigest-open",
                    help="the public repository as OWNER/NAME: replaces OWNER/REPO in the docs "
                         "(default %(default)s)")
    args = ap.parse_args(argv)
    if args.repo is not None and not REPO_RE.fullmatch(args.repo):
        ap.error(f"--repo must be OWNER/NAME, got {args.repo!r}")

    out = args.out.resolve()
    if out.exists() and any(out.iterdir()):
        raise SystemExit(f"{out} is not empty; the export only writes into an empty directory")
    if out == ROOT or ROOT in out.parents:
        raise SystemExit(f"{out} is inside the repo; export somewhere else")
    out.mkdir(parents=True, exist_ok=True)

    sha = git("rev-parse", "HEAD").strip()
    dirty = git("status", "--porcelain").strip()
    log(f"source {ROOT} at {sha}" + (" (working tree has uncommitted changes; they do not ship)" if dirty else ""))
    tracked = git("ls-tree", "-r", "-z", "--name-only", "HEAD").split("\0")
    tracked = [t for t in tracked if t]
    patterns = read_allowlist(ALLOWLIST.read_text())
    files, unmatched = select(patterns, tracked)
    for pattern in unmatched:
        log(f"allowlist line matched nothing (yet): {pattern}")
    bad = never_violations(files)
    if bad:
        raise SystemExit(f"the allowlist matches files that must never ship: {bad}")

    archive = git("archive", "--format=tar", "HEAD", "--", *files, binary=True)
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        tar.extractall(out, filter="data")
    log(f"copied {len(files)} allowlisted files")

    excluded = drop_unrunnable_tests(out, set(tracked))
    for rel, gone in excluded.items():
        log(f"excluded {rel}: imports {', '.join(gone)}, which does not ship")

    owner, repo = origin_owner_repo()
    candidate = yaml.safe_load(show("profile.yaml")).get("candidate") or {}
    # The licence holder is the committed profile's display name (by design).
    for note in write_generated(out, set(tracked), repo, candidate.get("display_name") or owner):
        log(note)
    for note in apply_rewrites(out, {"owner": owner, "short": candidate.get("short_name") or owner}):
        log(note)
    if args.repo:
        log(f"{REPO_PLACEHOLDER}: {fill_repo(out, args.repo)} replaced with {args.repo}")
    else:
        log(f"{REPO_PLACEHOLDER}: {len(placeholder_hits(out))} left as placeholders (no --repo)")
    regenerate(out)
    clean_scratch(out)
    count, size = write_manifest(out, sha)
    log(f"{count} files, {size} bytes; manifest at {manifest_path(out)} (not shipped)")

    if not args.check:
        return 0

    failures = []
    shipped = set(list_files(out))
    bad = layout_violations(sorted(shipped))
    log(f"check: layout: {len(bad)} file(s) under docs/public/")
    if bad:
        failures.append(f"docs/public/ shipped: {', '.join(bad)}")
    dupes = duplicate_files(out, sorted(shipped))
    for a, b in dupes:
        print(f"  duplicate content: {a} == {b}")
    log(f"check: duplicates: {len(dupes)} pair(s) of identical files")
    if dupes:
        failures.append(f"{len(dupes)} pair(s) of shipped files with identical content")
    passed, summary, skips = run_suite(out)
    log(f"check: tests: {summary}")
    if not passed:
        failures.append(f"tests failed: {summary}")
    for line in skips:
        print(f"  {line}")
    if skips:
        failures.append(f"{len(skips)} skip(s) naming a profile")
    clean_scratch(out)
    for rel in set(list_files(out)) - shipped:   # caches the suite left behind
        (out / rel).unlink()
    for folder in sorted((p for p in out.rglob("*") if p.is_dir()), reverse=True):
        if not any(folder.iterdir()):
            folder.rmdir()

    for var in unscanned(os.environ):
        log(f"WARN ***** {var} is not set: the scrub did NOT scan for it *****")
    items = denylist(args.deny)
    log(f"check: scrub for {len(items)} values ({', '.join(sorted({l for l, _, _ in items}))})")
    failures += check_placeholders(out, args.repo)
    hits = scrub(out, items, repo_addresses(args.repo))
    for hit in hits:
        print(f"  {hit}")
    log(f"check: scrub: {len(hits)} hit(s)")
    if hits:
        failures.append(f"scrub found {len(hits)} hit(s)")

    ok, detail = gitleaks(out)
    log(f"check: {detail}")
    if not ok:
        failures.append("gitleaks reported findings")

    if failures:
        log("CHECK FAILED: " + "; ".join(failures))
        return 1
    log("check passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
