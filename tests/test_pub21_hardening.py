"""PUB-21: security hardening.

Covers: the digest escapes every posting and model value and links only
http(s) URLs (a <script> title and a javascript: apply link render escaped
and with no href; a normal posting renders as before); url_resolver allows
only http(s), refuses loopback/private/link-local/multicast/reserved
addresses before every request and every redirect hop, and caps redirects;
every workflow pins its actions to a full commit SHA; dependabot and the
tests workflow are configured; the lock file pins every package with hashes.
"""

import re
from pathlib import Path

import pytest

import emailer

ROOT = Path(__file__).resolve().parent.parent


def _job(**kw):
    base = {"title": "Director of FP&A", "company": "Wonka Industries", "location": "Chicago, IL",
            "work_model": "Hybrid", "comp": "$175,000 - $195,000 a year", "score": 9,
            "rationale": "Chocolate maker. Top FP&A seat. Culture: candid.", "apply_link": "https://example.com/jobs/1",
            "source": "ats"}
    return {**base, **kw}


# --- step 1: the digest escapes and links safely ------------------------------------------

def test_a_script_title_and_a_javascript_link_render_inert():
    card = emailer._job_card(_job(title="<script>alert(1)</script>", company='Evil "Co" <b>',
                                  rationale="<img src=x onerror=alert(1)>", apply_link="javascript:alert(1)",
                                  linkedin_url="javascript:alert(2)"))
    print(card)
    assert "<script>" not in card and "&lt;script&gt;alert(1)&lt;/script&gt;" in card
    assert "<img" not in card and "&lt;img src=x onerror=alert(1)&gt;" in card
    assert "Evil &quot;Co&quot; &lt;b&gt;" in card
    assert "javascript:" not in card and "alert(2)" not in card
    assert 'href=' not in card                                     # no link at all on this card
    assert ">Apply (ATS)</span>" in card                          # the label, unlinked


@pytest.mark.parametrize("url,ok", [
    ("https://example.com/a?b=1&c=2", True), ("http://example.com", True), ("HTTPS://Example.com/x", True),
    ("javascript:alert(1)", False), ("data:text/html,<b>x</b>", False), ("/relative/path", False),
    ("//example.com/x", False), ("mailto:a@example.com", False), ("", False), (None, False), ("https://", False),
])
def test_only_http_and_https_become_links(url, ok):
    href = emailer._safe_href(url)
    assert (href is not None) == ok
    if ok:
        import html
        assert href == html.escape(url.strip(), quote=True)


def test_a_quote_in_a_url_cannot_break_out_of_the_attribute():
    html = emailer._link('https://example.com/x" onmouseover="alert(1)', "Go", "color:red;")
    assert html == '<a href="https://example.com/x&quot; onmouseover=&quot;alert(1)" style="color:red;">Go</a>'


def test_a_normal_posting_renders_as_before_but_for_escaping():
    card = emailer._job_card(_job())
    assert '<a href="https://example.com/jobs/1" style="display:inline-block;padding:6px 16px;background:#1a1a1a;' \
           'color:#ffffff;border-radius:6px;font-size:13px;text-decoration:none;font-weight:500;">Apply (ATS)</a>' in card
    assert ">Director of FP&amp;A</div>" in card and ">Wonka Industries</div>" in card
    assert ">Chicago, IL · Hybrid · $175,000 - $195,000 a year</div>" in card
    assert ">Chocolate maker. Top FP&amp;A seat. Culture: candid.</div>" in card
    assert ">ATS</span>" in card


def test_the_cool_company_card_and_collapsed_links_are_escaped_too():
    job = _job(title="<i>t</i>", company="<u>c</u>", apply_link="javascript:x", is_cool_company_wrong_role=True,
               other_links=[{"url": "javascript:y", "source": "jsearch", "company": "<u>c</u>"},
                            {"url": "https://example.com/2", "source": "<b>src</b>", "company": "<u>c</u>"}])
    html = emailer._build_html([], [], [job], [], False, False)
    assert "<i>t</i>" not in html and "<u>c</u>" not in html and "javascript:" not in html
    assert '<a href="https://example.com/2"' in html and "&lt;b&gt;src&lt;/b&gt;" in html


def test_the_fetch_failure_banner_escapes_and_checks_its_link():
    runs = [{"started_at": "2026-11-04T04:17:00Z", "url": "javascript:z", "error_title": "<b>boom</b>"}]
    html = emailer._fetch_failure_banner(runs)
    assert "&lt;b&gt;boom&lt;/b&gt;" in html and "javascript:" not in html and "href" not in html


def test_no_html_is_built_from_an_unescaped_job_field():
    """Every {job...} / {title} etc. interpolation in emailer.py goes through _text or _link."""
    src = (ROOT / "emailer.py").read_text()
    raw = re.findall(r'>\{(?!_text\(|_link\(|_score_badge|_dream_badge|_fresh_badge|_warm_badge|_relo_badge|'
                     r'_startup_badge|_age_html|status_html|_function_cap_html|_linkedin_link|_other_links|'
                     r'_source_badge|apply_html|label_html|count_str|hidden|_today|lines|content)[^}]*\}', src)
    assert raw == []


# --- step 2: url_resolver -------------------------------------------------------------------

import url_resolver  # noqa: E402

DNS = {"jobs.example.com": ["93.184.216.34"], "redirect.example.com": ["93.184.215.14"],
       "intranet.example.com": ["10.0.0.5"], "meta.example.com": ["169.254.169.254"],
       "v6local.example.com": ["::1"], "mixed.example.com": ["93.184.216.34", "192.168.1.9"]}


class _Resp:
    def __init__(self, status, location=None):
        self.status_code, self.headers = status, ({"Location": location} if location else {})


@pytest.fixture
def net(monkeypatch):
    """A stubbed DNS and HTTP: `routes` maps a URL to (status, Location); every
    requested URL is recorded in `asked`."""
    asked, routes = [], {}

    def resolve(host):
        if host not in DNS:
            raise url_resolver.socket.gaierror("unknown")
        return DNS[host]

    def request(url):
        asked.append(url)
        status, location = routes.get(url, (200, None))
        return _Resp(status, location)

    monkeypatch.setattr(url_resolver, "_resolve_host", resolve)
    monkeypatch.setattr(url_resolver, "_request", request)
    return asked, routes


def test_a_normal_link_resolves_through_its_redirect(net, capsys):
    asked, routes = net
    routes["https://redirect.example.com/r/1"] = (302, "https://jobs.example.com/apply/1?utm_source=x")
    final, resolved, agg = url_resolver.resolve_apply_link("https://redirect.example.com/r/1")
    assert (final, resolved, agg) == ("https://jobs.example.com/apply/1", True, False)
    assert asked == ["https://redirect.example.com/r/1", "https://jobs.example.com/apply/1?utm_source=x"]
    assert "not followed" not in capsys.readouterr().out


@pytest.mark.parametrize("url,reason", [
    ("https://intranet.example.com/job", "intranet.example.com resolves to 10.0.0.5 (private)"),
    ("http://meta.example.com/latest", "meta.example.com resolves to 169.254.169.254"),
    ("https://v6local.example.com/x", "v6local.example.com resolves to ::1 (loopback)"),
    ("https://mixed.example.com/x", "mixed.example.com resolves to 192.168.1.9 (private)"),
    ("http://127.0.0.1:8080/admin", "127.0.0.1 resolves to 127.0.0.1 (loopback)"),
    ("file:///etc/passwd", "scheme file not allowed"),
    ("ftp://jobs.example.com/x", "scheme ftp not allowed"),
])
def test_a_refused_link_is_never_requested_and_stored_unresolved(net, capsys, monkeypatch, url, reason):
    asked, _ = net
    DNS.setdefault("127.0.0.1", ["127.0.0.1"])
    final, resolved, _agg = url_resolver.resolve_apply_link(url)
    assert asked == [] and resolved is False and final == url_resolver.normalize_url(url)
    out = capsys.readouterr().out
    assert out.startswith("[URLResolver] not followed: ") and reason in out and url in out


def test_a_redirect_into_a_private_address_is_not_followed(net, capsys):
    asked, routes = net
    routes["https://redirect.example.com/r/2"] = (301, "http://intranet.example.com/secret")
    final, resolved, _ = url_resolver.resolve_apply_link("https://redirect.example.com/r/2")
    assert (final, resolved) == ("https://redirect.example.com/r/2", False)
    assert asked == ["https://redirect.example.com/r/2"]                  # the private hop is never requested
    assert ("not followed: intranet.example.com resolves to 10.0.0.5 (private) (redirect 1 to "
            "http://intranet.example.com/secret)") in capsys.readouterr().out


def test_a_relative_redirect_and_the_redirect_cap(net, capsys):
    asked, routes = net
    for i in range(7):
        routes[f"https://jobs.example.com/{i}"] = (302, f"/{i + 1}")
    final, resolved, _ = url_resolver.resolve_apply_link("https://jobs.example.com/0")
    assert (final, resolved) == ("https://jobs.example.com/0", False)
    assert len(asked) == url_resolver.MAX_REDIRECTS + 1 == 6
    assert "more than 5 redirects" in capsys.readouterr().out


def test_an_unknown_host_is_refused():
    assert url_resolver.refuse_reason("https://no-such-host.invalid/x").startswith("host does not resolve")


def test_redirects_are_never_followed_by_requests_itself():
    src = (ROOT / "url_resolver.py").read_text()
    assert "allow_redirects=True" not in src and src.count("allow_redirects=False") == 2


# --- steps 3 and 4: workflows, Dependabot, lock files ----------------------------------------

import yaml  # noqa: E402

WORKFLOWS = sorted((ROOT / ".github" / "workflows").glob("*.yml"))


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name)
def test_every_action_is_pinned_to_a_full_commit_sha(path):
    uses = re.findall(r"^\s*(?:-\s*)?uses:\s*(\S+)(.*)$", path.read_text(), re.M)
    assert uses, path.name
    for ref, rest in uses:
        assert re.fullmatch(r"[\w.-]+/[\w.-]+@[0-9a-f]{40}", ref), ref
        assert re.fullmatch(r"\s+#\s+v\d+\.\d+\.\d+", rest), f"{ref}: version tag comment missing"


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name)
def test_every_install_is_hash_checked(path):
    installs = re.findall(r"pip install[^\n]*", path.read_text())
    assert all("--require-hashes" in line for line in installs), installs


def test_dependabot_updates_actions_monthly_in_one_group_and_never_pip():
    """PUB-21 follow-up: a pip PR would change a pin without its lock (AGENTS.md, Dependencies)."""
    cfg = yaml.safe_load((ROOT / ".github" / "dependabot.yml").read_text())
    assert [(u["package-ecosystem"], u["schedule"]["interval"]) for u in cfg["updates"]] == [("github-actions", "monthly")]
    assert cfg["updates"][0]["groups"] == {"actions": {"patterns": ["*"]}}


def test_lock_sh_recompiles_both_locks_with_hashes():
    script = (ROOT / "tools" / "lock.sh").read_text()
    assert script.startswith("#!/usr/bin/env bash") and (ROOT / "tools" / "lock.sh").stat().st_mode & 0o111
    assert "--generate-hashes" in script
    assert "-o requirements.lock requirements.txt" in script
    assert "-o tools/requirements.lock requirements.txt tools/requirements.txt" in script


def test_the_tests_workflow_needs_no_secrets():
    text = (ROOT / ".github" / "workflows" / "tests.yml").read_text()
    wf = yaml.safe_load(text)
    on = wf.get("on") or wf.get(True)
    assert set(on) == {"push", "pull_request"}
    job = wf["jobs"]["pytest"]
    assert job["strategy"]["matrix"]["python-version"] == ["3.11", "3.12"]
    assert "${{ secrets" not in text and wf["permissions"] == {"contents": "read"}
    run = next(s for s in job["steps"] if "pytest" in s.get("run", ""))
    assert run["env"]["JOB_DIGEST_PROFILE"] == "profile.example.yaml"


@pytest.mark.parametrize("lock,sources", [("requirements.lock", ["requirements.txt"]),
                                          ("tools/requirements.lock", ["requirements.txt", "tools/requirements.txt"])])
def test_the_lock_files_pin_every_requirement_with_hashes(lock, sources):
    text = (ROOT / lock).read_text()
    entries = re.findall(r"^([A-Za-z0-9_.-]+)==(\S+) \\\n((?:\s+--hash=sha256:[0-9a-f]{64}(?: \\)?\n)+)", text, re.M)
    names = {n.lower().replace("_", "-"): v for n, v, _ in entries}
    assert len(entries) == len(re.findall(r"^[A-Za-z0-9]", text, re.M))      # every package carries hashes
    for src in sources:
        for line in (ROOT / src).read_text().splitlines():
            m = re.match(r"^([A-Za-z0-9_.-]+)==(\S+)", line.strip())
            if m:
                assert names.get(m.group(1).lower().replace("_", "-")) == m.group(2), (src, line)


def test_the_slot_jobs_install_pyyaml_from_the_lock(tmp_path):
    import subprocess
    for name in ("daily.yml", "weekly_summary.yml"):
        text = (ROOT / ".github" / "workflows" / name).read_text()
        assert "            requirements.lock\n" in text and "pip install PyYAML" not in text
    awk = "/^pyyaml==/{p=1;print;next} p&&/^ /{print;next} {p=0}"
    out = subprocess.run(["awk", awk, str(ROOT / "requirements.lock")], capture_output=True, text=True).stdout
    assert out.startswith("pyyaml==6.0.3 \\\n") and out.count("--hash=sha256:") >= 2
    assert "==" not in out.split("\n", 1)[1]                          # PyYAML alone


# --- step 5: the privacy statement matches the code ----------------------------------------

def test_the_privacy_wording_matches_status_tracking():
    import config
    from sources import status_email
    assert status_email.LOOKBACK_DAYS == 14
    assert config.STATUS_EMAIL_MAX_BODY_CHARS == 20_000 and config.STATUS_EMAIL_MAX_ROWS == 30
    readme = (ROOT / "docs" / "public" / "README.md").read_text() if (ROOT / "docs" / "public").exists() \
        else (ROOT / "README.md").read_text()
    guide = (ROOT / "docs" / "SETUP_GUIDE.md").read_text()
    page = (ROOT / "docs" / "index.html").read_text()
    assert "## Privacy and data" in readme and "last 14 days" in readme and "20,000 characters" in readme
    assert "<h4>Privacy and data</h4>" in page and "last 14 days of your inbox" in page
    assert "reads every message in their inbox" not in guide and "reads the last 14 days of their inbox" in guide
    assert "reads every message in the inbox" not in (ROOT / "docs" / "PRD.md").read_text()
    for text in (readme, guide, page):
        assert "Nothing is sent anywhere else" in text


def test_every_python_file_compiles_on_this_interpreter():
    """The tests workflow runs 3.11 and 3.12: a 3.12-only form (a backslash in an
    f-string expression, PEP 701) in a module no test imports must still fail here."""
    import subprocess
    files = [ROOT / f for f in subprocess.run(["git", "ls-files", "*.py"], cwd=ROOT, capture_output=True,
                                              text=True).stdout.split()] or sorted(ROOT.rglob("*.py"))
    bad = []
    for path in files:
        if ".venv" in path.parts or not path.exists():
            continue
        try:
            compile(path.read_text(), str(path), "exec")
        except SyntaxError as e:
            bad.append(f"{path.relative_to(ROOT)}:{e.lineno}: {e.msg}")
    assert bad == []
