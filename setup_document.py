# setup_document.py — `python main.py setup --from-document path` (PUB-10)
#
# Reads a resume or a job description and, with ANTHROPIC_API_KEY set, makes
# one model call that proposes the interview's defaults for:
#   candidate.background   (a resume only: a few one-line points)
#   roles.target_department
#   roles.target_titles
#   scoring.role_criteria  (three to five, each with points)
# The interview then asks every question as usual, showing these as the
# [defaults] to keep or edit. Only the proposed fields reach profile.yaml:
# the document's text is never written anywhere.
#
# Formats: .txt and .md (read as text), .docx (its text, read with the
# standard library's zipfile), .pdf (sent to the model as a PDF document, so
# no PDF library is needed). Anything else is skipped with a message and the
# normal interview runs, as it does without a key.

import json
import os
import zipfile
from xml.etree import ElementTree

TEXT_FORMATS = (".txt", ".md")
FORMATS = TEXT_FORMATS + (".docx", ".pdf")
MAX_CHARS = 60_000
MAX_TOKENS = 1500
_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"

PROMPT = """You help someone set up a job-search agent. The attached document is \
either their resume or a job description they would want. Propose starting \
values for their profile, as JSON only, no prose:

{
  "kind": "resume" or "job_description",
  "background": ["<3 to 6 short one-line points about the person: years, scope, \
domains, notable results>"] -- only for a resume; [] for a job description,
  "target_department": "<the department or function the target role leads, one \
phrase that finishes 'leads ... and owns a team or a budget', e.g. 'an internal \
IT, infrastructure or business systems function'>",
  "target_titles": ["<2 to 6 job titles that are exactly the role they want>"],
  "role_criteria": [{"points": <integer -5 to 5, not 0>, "criterion": "<one line \
a scorer can recognise in a posting>"}]
}

role_criteria: three to five. Positive points for what makes a role a strong \
fit (+1 to +3, the most important +3), negative for what makes it weaker (-1 \
or -2). Judge the role itself only: never pay, location, remote/hybrid or \
company size. Write every value in plain words; never copy personal details \
such as names, emails, phone numbers or addresses."""


class UnreadableDocument(Exception):
    """The file cannot be read as a document; the message says why."""


def read_document(path):
    """("text", str) or ("pdf", bytes)."""
    ext = os.path.splitext(path)[1].lower()
    if ext not in FORMATS:
        raise UnreadableDocument(f"{os.path.basename(path)}: {ext or 'no extension'} is not a format setup reads "
                                 f"({', '.join(FORMATS)}); save it as one of those to use it")
    if not os.path.isfile(path):
        raise UnreadableDocument(f"{path}: no such file")
    if ext == ".pdf":
        with open(path, "rb") as fh:
            return "pdf", fh.read()
    if ext == ".docx":
        try:
            with zipfile.ZipFile(path) as z:
                root = ElementTree.fromstring(z.read("word/document.xml"))
        except (zipfile.BadZipFile, KeyError, ElementTree.ParseError) as e:
            raise UnreadableDocument(f"{os.path.basename(path)}: not a readable .docx ({type(e).__name__})") from None
        paragraphs = ["".join(t.text or "" for t in p.iter(f"{_W}t")) for p in root.iter(f"{_W}p")]
        text = "\n".join(p for p in paragraphs if p.strip())
    else:
        with open(path, encoding="utf-8", errors="replace") as fh:
            text = fh.read()
    if not text.strip():
        raise UnreadableDocument(f"{os.path.basename(path)}: no text found")
    return "text", text[:MAX_CHARS]


def _request(kind, body):
    """(messages, attachments): a PDF goes as an attachment, text inline."""
    if kind == "pdf":
        return ([{"role": "user", "content": [{"type": "text", "text": PROMPT}]}],
                [{"media_type": "application/pdf", "data": body}])
    doc = {"type": "text", "text": f"<document>\n{body}\n</document>"}
    return [{"role": "user", "content": [doc, {"type": "text", "text": PROMPT}]}], None


def _strings(value, limit):
    if not isinstance(value, list):
        return []
    out = [" ".join(str(v).split()) for v in value if isinstance(v, str) and v.strip()]
    return out[:limit]


def parse_proposal(text):
    """The reply as {background, target_department, target_titles,
    role_criteria}, each cleaned; a field the reply gets wrong is left out."""
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < start:
        raise ValueError("no JSON object in the reply")
    raw = json.loads(text[start:end + 1])
    out = {}
    if raw.get("kind") == "resume":
        lines = _strings(raw.get("background"), 6)
        if lines:
            out["background"] = lines
    dept = " ".join(str(raw.get("target_department") or "").split())
    if dept and "\n" not in dept:
        out["target_department"] = dept
    titles = _strings(raw.get("target_titles"), 8)
    if titles:
        out["target_titles"] = titles
    criteria = []
    for c in raw.get("role_criteria") or []:
        if not isinstance(c, dict):
            continue
        points, text_ = c.get("points"), " ".join(str(c.get("criterion") or "").split())
        if isinstance(points, int) and not isinstance(points, bool) and points and -5 <= points <= 5 and text_:
            criteria.append({"points": points, "criterion": text_})
    if len(criteria) >= 1:
        out["role_criteria"] = criteria[:5]
    return out


def propose(path, model):
    """(proposal, None) or (None, why not). One model call. A billing/auth
    error raises llm.ApiAccessError (OPS-12); any other failure is a reason."""
    import llm
    try:
        kind, body = read_document(path)
    except UnreadableDocument as e:
        return None, str(e)
    messages, attachments = _request(kind, body)
    try:
        text, _ = llm.complete(model, None, messages, MAX_TOKENS, json_mode=True, attachments=attachments,
                               purpose="setup --from-document call")
        return parse_proposal(text), None
    except llm.ApiAccessError:
        raise
    except Exception as e:                             # noqa: BLE001 -- the interview runs either way
        return None, f"the model call failed ({type(e).__name__}: {' '.join(str(e).split())[:160]})"


def apply(data, proposal, criterion_id):
    """Put the proposal into the profile data as the interview's defaults.
    criterion_id(text, taken) makes each new criterion's snake_case id."""
    if "background" in proposal:
        data["candidate"]["background"] = "\n".join(f"- {line.lstrip('- ')}" for line in proposal["background"])
    roles = data["roles"]
    if "target_department" in proposal:
        roles["target_department"] = proposal["target_department"]
    if "target_titles" in proposal:
        roles["target_titles"] = list(proposal["target_titles"])
    if "role_criteria" in proposal:
        taken = set()
        deals = [c for c in data["scoring"]["role_criteria"] if c.get("dealbreaker")]
        for c in deals:
            taken.add(c["id"])
        data["scoring"]["role_criteria"] = [
            {"id": criterion_id(c["criterion"], taken), "points": c["points"], "criterion": c["criterion"]}
            for c in proposal["role_criteria"]] + deals
    return data


def describe(proposal):
    """What was proposed, for the terminal."""
    lines = []
    if "background" in proposal:
        lines.append(f"   background: {len(proposal['background'])} points")
    if "target_department" in proposal:
        lines.append(f"   target_department: {proposal['target_department']}")
    if "target_titles" in proposal:
        lines.append(f"   target_titles: {', '.join(proposal['target_titles'])}")
    for c in proposal.get("role_criteria", []):
        lines.append(f"   criterion {c['points']:+d}: {c['criterion']}")
    return lines

