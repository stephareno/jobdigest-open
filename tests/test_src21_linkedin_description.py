"""SRC-21: the LinkedIn fetcher no longer cuts descriptions at 3,000 characters;
scorer.JD_CHARS is the only limit. No network access."""

import scorer
from sources import linkedin_email


class _Resp:
    def __init__(self, text):
        self.text = text

    def raise_for_status(self):
        pass


def test_description_kept_up_to_scorer_jd_chars(monkeypatch):
    body = "x" * (scorer.JD_CHARS + 500)
    html = ('<h1 class="top-card-layout__title">Director of IT</h1>'
            f'<div class="description__text">{body}</div>')
    monkeypatch.setattr(linkedin_email.requests, "get", lambda *a, **k: _Resp(html))
    job = linkedin_email._fetch_job_details("123")
    assert len(job["description"]) == scorer.JD_CHARS
    assert scorer.JD_CHARS > 3000
