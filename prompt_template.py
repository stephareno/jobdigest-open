# prompt_template.py — renders the prompts in prompts/ from the profile (PUB-02)
#
# A deliberately tiny renderer, no Jinja and no new dependency. The prompts
# contain literal JSON, so placeholders use double braces and single braces
# stay plain text.
#
#   {{candidate.display_name}}                   a value, by dotted path
#   {{scoring.max_score|signed}}                 an integer with its sign: +3, -2, 0
#   {{industries.strong_yes|join:" ·\n"}}        a list joined by the separator (\n = newline)
#   {{industries.neutral|join:", "|capfirst}}    filters apply left to right
#   {{company_fit.appeal_signals|or_list}}       "A", "A or B", "A, B, or C"
#   {{industries.hard_block|keys|join:", "}}     a mapping's keys, in order
#   {{#if company_fit.exemplars}}...{{/if}}      kept only when the value is true / non-empty
#   {{#if not company_fit.size_matters}}...{{/if}}   kept only when it is false / empty
#   blocks nest: the innermost is resolved first
#
# The context is the loaded profile plus `pronoun` (the forms of
# candidate.pronouns: subject, object, possessive, verb_s), `mission_scale`
# (the mission and values scale spelled out from scoring.mission_values),
# `rubric` (scoring.role_criteria as the SCORING lines: positive, negative,
# ids; PUB-09), `overlaps` (which profile-specific sentences apply; PUB-07b) and
# any extra values the caller passes (the weekly summary's `listing`). An unknown path,
# a list without a join, an unknown filter or a placeholder left unrendered
# raises TemplateError: a prompt is never sent with a hole in it.

import os
import re
from collections.abc import Mapping

from profile_loader import PRONOUNS, with_defaults

PROMPTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "prompts")

# An #if block holding no other #if: replaced repeatedly, so nested blocks
# resolve from the inside out.
_IF_RE = re.compile(r"\{\{#if (not )?([\w.]+)\}\}((?:(?!\{\{#if ).)*?)\{\{/if\}\}", re.S)
_ARG = r'"((?:[^"\\]|\\.)*)"'
_FILTER_RE = re.compile(r"\|(\w+)(?::" + _ARG + r")?")
_VALUE_RE = re.compile(r"\{\{([a-z_][\w.]*)((?:\|\w+(?::" + _ARG + r")?)*)\}\}")


class TemplateError(ValueError):
    pass


def _signed(value):
    return f"{value:+d}" if value else "0"


def mission_scale(values):
    """scoring.mission_values as the prompt's two scale lines, every step from
    max down to min: {"fit": "+2 strong fit · +1 partial fit · 0 not
    determinable from the posting", "misfit": "-1 slight misfit · -2 strong
    misfit"}. A wider range gets more partial / slight steps."""
    top, bottom = values["max"], values["min"]
    fit = [f"{_signed(v)} {'strong fit' if v == top else 'partial fit'}" for v in range(top, 0, -1)]
    misfit = [f"{_signed(v)} {'strong misfit' if v == bottom else 'slight misfit'}" for v in range(-1, bottom - 1, -1)]
    return {"fit": " · ".join(fit + ["0 not determinable from the posting"]),
            "misfit": " · ".join(misfit)}


def rubric(criteria):
    """scoring.role_criteria as the prompt's SCORING lines (PUB-09):
    {"positive": "+3: <criterion>\n...", "negative": "-2: ...", "ids":
    "id_a, id_b"}. Positives first, then negatives, each in profile order;
    ids lists the criteria's ids in that same order, for the reply's
    criteria_hits.

    PUB-10: a dealbreaker criterion is never scored. It is listed under
    "dealbreakers" ("- <id>: <criterion>") for the model to report in
    criteria_hits, and its id follows the scored ones; the code turns a hit
    into score 0 (location.apply_location_tier)."""
    scored = [c for c in criteria if not c.get("dealbreaker")]
    positive = [c for c in scored if c["points"] > 0]
    negative = [c for c in scored if c["points"] < 0]
    deals = [c for c in criteria if c.get("dealbreaker")]

    def lines(items):
        return "\n".join(f"{_signed(c['points'])}: {c['criterion'].strip()}" for c in items)
    return {"positive": lines(positive), "negative": lines(negative),
            "dealbreakers": "\n".join(f"- {c['id']}: {c['criterion'].strip()}" for c in deals),
            "ids": ", ".join(c["id"] for c in positive + negative + deals)}


_WORD_RE = re.compile(r"[a-z0-9&]+")


def _words(text):
    return set(_WORD_RE.findall(str(text).lower()))


def overlaps(profile):
    """Which of the prompt's profile-specific sentences apply (PUB-07b).

    scored_twice: the deprioritize entries that are also a negative SCORING
    criterion (every word of the entry appears in that criterion's text, as
    "client-facing consulting delivery" does in "Client-facing/consulting
    delivery (external billable work)"); the prompt then says the penalty is
    applied once. staffing: a deprioritize entry or a criterion names
    staffing; the prompt then says that penalty follows the hiring company."""
    industries = profile.get("industries") or {}
    deprioritize = industries.get("deprioritize") or []
    criteria = profile["scoring"]["role_criteria"]
    negative = [_words(c["criterion"]) for c in criteria if not c.get("dealbreaker") and c["points"] < 0]
    twice = [e for e in deprioritize if _words(e) and any(_words(e) <= n for n in negative)]
    staffing = any("staffing" in str(t).lower() for t in list(deprioritize) + [c["criterion"] for c in criteria])
    return {"scored_twice": twice, "staffing": staffing}


def context(profile, **extra):
    """The values a prompt template can name. The profile's optional fields
    take their defaults, so a profile built in code renders like a loaded one."""
    profile = with_defaults(profile)
    return {**profile, "pronoun": PRONOUNS[profile["candidate"]["pronouns"]],
            "mission_scale": mission_scale(profile["scoring"]["mission_values"]),
            "rubric": rubric(profile["scoring"]["role_criteria"]),
            "overlaps": overlaps(profile), **extra}


def _lookup(ctx, path, name):
    value = ctx
    for part in path.split("."):
        if not isinstance(value, Mapping) or part not in value:
            raise TemplateError(f"{name}: no value for {{{{{path}}}}}")
        value = value[part]
    return value


def _apply(value, filters, path, name):
    for filt, arg in filters:
        arg = None if arg is None else arg.replace("\\n", "\n").replace('\\"', '"')
        if filt == "join":
            if not isinstance(value, (list, tuple)) or arg is None:
                raise TemplateError(f"{name}: join needs a list and a separator ({{{{{path}}}}})")
            value = arg.join(str(v) for v in value)
        elif filt == "signed":
            if not isinstance(value, int) or isinstance(value, bool):
                raise TemplateError(f"{name}: signed needs an integer ({{{{{path}}}}})")
            value = _signed(value)
        elif filt == "keys":
            if not isinstance(value, Mapping):
                raise TemplateError(f"{name}: keys needs a mapping ({{{{{path}}}}})")
            value = tuple(value)
        elif filt == "or_list":
            if not isinstance(value, (list, tuple)) or not value:
                raise TemplateError(f"{name}: or_list needs a non-empty list ({{{{{path}}}}})")
            items = [str(v) for v in value]
            value = items[0] if len(items) == 1 else (
                f"{items[0]} or {items[1]}" if len(items) == 2 else ", ".join(items[:-1]) + f", or {items[-1]}")
        elif filt == "capfirst":
            value = str(value)
            value = value[:1].upper() + value[1:]
        else:
            raise TemplateError(f"{name}: unknown filter {filt!r} ({{{{{path}}}}})")
    if isinstance(value, (list, tuple, Mapping)):
        raise TemplateError(f"{name}: {{{{{path}}}}} is a list or mapping; render it with a join")
    return str(value)


def render(text, ctx, name="template"):
    """`text` with every block and placeholder filled from `ctx`."""
    def block(m):
        negate, path, body = m.group(1), m.group(2), m.group(3)
        keep = bool(_lookup(ctx, path, name))
        return body if keep != bool(negate) else ""

    def value(m):
        path = m.group(1)
        filters = [(f.group(1), f.group(2)) for f in _FILTER_RE.finditer(m.group(2))]
        return _apply(_lookup(ctx, path, name), filters, path, name)

    while True:
        resolved = _IF_RE.sub(block, text)
        if resolved == text:
            break
        text = resolved
    out = _VALUE_RE.sub(value, text)
    left = re.search(r"\{\{.*?\}\}", out, re.S)
    if left:
        raise TemplateError(f"{name}: placeholder not rendered: {left.group(0)[:60]!r}")
    return out


def render_file(filename, profile, **extra):
    """prompts/<filename> rendered from the profile."""
    with open(os.path.join(PROMPTS_DIR, filename)) as fh:
        text = fh.read()
    return render(text, context(profile, **extra), name=filename)
