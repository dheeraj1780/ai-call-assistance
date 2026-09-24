"""Deterministic, low-latency detectors run on every final transcript segment.

They are intentionally conservative (moderate confidence, specific phrasings) and only
produce *suggestions*: humans can confirm, edit or reject everything, and the LLM pass can
add richer extraction. Tuned for Indian English sales conversations; a language parameter
can select Hinglish/regional rule sets later.
"""

import re
from dataclasses import dataclass

from app.intel.models import NoteKind, ObjectionCategory


@dataclass(frozen=True)
class Detection:
    kind: NoteKind
    text: str
    confidence: float
    category: ObjectionCategory | None = None
    key: str = ""  # dedupe key suffix


def _rx(*patterns: str) -> re.Pattern[str]:
    return re.compile("|".join(f"(?:{p})" for p in patterns), re.IGNORECASE)


OBJECTION_PATTERNS: dict[ObjectionCategory, re.Pattern[str]] = {
    ObjectionCategory.PRICE: _rx(
        r"\btoo (?:expensive|costly|high)\b",
        r"\b(?:price|cost|pricing) is (?:too |very |a bit )?(?:high|much|steep)\b",
        r"\bout of (?:our|my) budget\b",
        r"\bcan(?:'t|not) afford\b",
        r"\bcheaper (?:option|alternative)\b",
    ),
    ObjectionCategory.EXISTING_SOLUTION: _rx(
        r"\b(?:we are|we're|i am|i'm) (?:happy|fine|okay|ok) with (?:our|the) (?:current|existing)\b",
        r"\bwe already (?:have|use)\b",
        r"\b(?:excel|tally|our system) (?:is )?(?:working|works) (?:fine|well|okay)\b",
    ),
    ObjectionCategory.TIMING: _rx(
        r"\bnot (?:the )?right time\b",
        r"\bmaybe (?:later|next (?:month|quarter|year))\b",
        r"\b(?:too )?busy (?:right now|this (?:month|season))\b",
        r"\bafter (?:diwali|the season|this quarter)\b",
    ),
    ObjectionCategory.AUTHORITY: _rx(
        r"\b(?:check|discuss|talk|speak) (?:it |this )?with (?:my |our )?"
        r"(?:boss|partner|owner|director|md|manager|team|father|brother)\b",
        r"\bnot (?:my|only my) decision\b",
        r"\bneed(?:s)? (?:approval|sign[- ]off)\b",
    ),
    ObjectionCategory.TRUST: _rx(
        r"\bnot sure (?:if |whether )?(?:it|this) (?:will )?work",
        r"\bhow (?:can|do) (?:i|we) (?:trust|know)\b",
        r"\bheard (?:bad|negative)\b",
        r"\bis (?:it|our data) (?:safe|secure)\b",
    ),
    ObjectionCategory.FEATURE_GAP: _rx(
        r"\b(?:doesn't|doesnt|does not) (?:have|support)\b",
        r"\bmissing (?:a |the )?feature\b",
        r"\bwithout (?:gst|tally|whatsapp) (?:support|integration)\b",
    ),
    ObjectionCategory.IMPLEMENTATION: _rx(
        r"\b(?:too )?(?:complicated|complex) to (?:set ?up|use|learn)\b",
        r"\b(?:training|migration|setup) (?:will )?(?:take|takes) (?:too )?(?:long|much time)\b",
        r"\bmy (?:staff|team) (?:won't|will not|cannot|can't) (?:use|learn)\b",
    ),
    ObjectionCategory.COMPETITOR: _rx(
        r"\b(?:also )?(?:talking to|evaluating|comparing with|got a quote from) "
        r"(?:another|other|a different) (?:vendor|company|provider)\b",
    ),
}

KNOWN_TOOLS = (
    r"excel|spreadsheets?|google sheets|tally|zoho|busy|marg|sap|quickbooks|vyapar|khatabook"
    r"|registers?|paper|notebook|whatsapp"
)
_SUBJECT = r"\b(?:we|i)\s+(?:currently\s+|still\s+)?"
CURRENT_SOLUTION = re.compile(
    _SUBJECT
    + r"(?:use|are using|am using|manage|track|maintain|keep|record|handle|do)\b"
    + r"(?:\s+[a-z]+){0,3}?\s+(?:in|on|with|using|through)\s+(?:an? |the |our |my )?"
    + rf"(?P<tool>{KNOWN_TOOLS})\b"
    + "|"
    + _SUBJECT
    + rf"(?:use|are using|am using)\s+(?:an? |the |our )?(?P<tool2>{KNOWN_TOOLS})\b",
    re.IGNORECASE,
)
SCALE = re.compile(
    r"\b(?P<n>\d{1,6})\s+(?P<unit>branches|locations|stores|shops|outlets|warehouses|godowns"
    r"|factories|users|employees|staff|salespeople|orders|invoices|skus|products|trucks)\b",
    re.IGNORECASE,
)
NEED = re.compile(
    r"\b(?:we|i) (?:need|want|are looking for|am looking for|require|must have)\b"
    r"(?P<rest>[^.?!]{3,160})",
    re.IGNORECASE,
)
PAIN = re.compile(
    r"\b(?:problem|issue|pain|headache|difficult|takes (?:a lot of|too much) time|mistakes"
    r"|errors|mismatch|manual(?:ly)?)\b",
    re.IGNORECASE,
)
BUDGET = re.compile(
    r"(?:₹\s?\d|\brs\.?\s?\d|\b\d[\d,.]*\s?(?:rupees|lakh|lakhs|lac|crore|k)\b"
    r"|\bbudget (?:is|of|around|about)\b)",
    re.IGNORECASE,
)
TIMELINE = re.compile(
    r"\b(?:by|before|within|in) (?:the )?(?:next |this |end of (?:the )?)?(?:\d+\s+)?"
    r"(?:week|weeks|month|months|quarter|year|diwali|march|april|q[1-4])\b",
    re.IGNORECASE,
)
DECISION_MAKER = re.compile(
    r"\b(?:(?:my|our) (?:boss|owner|partner|director|md|ceo|father|brother) "
    r"(?:decides|will decide|takes the (?:final )?decision|has the final say)"
    r"|i (?:take|make) the (?:final )?decision|final decision is (?:mine|with me))\b",
    re.IGNORECASE,
)
NEXT_STEP = re.compile(
    r"\b(?:(?:send|share|email|whatsapp) (?:me|us) (?:the |a |your )?"
    r"(?:quote|quotation|pricing|price list|proposal|brochure|details|demo link)"
    r"|(?:let's|let us|can we) (?:schedule|set up|book|have) (?:a )?(?:demo|meeting|call|visit)"
    r"|call (?:me|us) (?:back|again) (?:on|next|tomorrow))\b",
    re.IGNORECASE,
)
QUESTION_START = re.compile(
    r"^(?:do|does|can|could|is|are|will|would|what|how|which|when|where|why|who)\b", re.IGNORECASE
)


def is_question(text: str) -> bool:
    t = text.strip()
    return t.endswith("?") or bool(QUESTION_START.match(t))


def _clip(s: str, n: int = 200) -> str:
    s = " ".join(s.split())
    return s if len(s) <= n else s[: n - 1] + "…"


def _key(s: str) -> str:
    return _clip(s, 40).lower()


def detect(text: str, *, speaker_is_customer: bool) -> list[Detection]:
    """Extract structured facts from one utterance. Customer speech is the main source;
    the salesperson's own statements are only scanned for agreed next steps."""
    found: list[Detection] = []
    if speaker_is_customer:
        for category, pattern in OBJECTION_PATTERNS.items():
            m = pattern.search(text)
            if m:
                key = f"{category}:{m.group(0).lower()}"
                found.append(Detection(NoteKind.OBJECTION, _clip(text), 0.6, category, key))
        m = CURRENT_SOLUTION.search(text)
        if m:
            tool = m.group("tool") or m.group("tool2")
            found.append(
                Detection(NoteKind.CURRENT_SOLUTION, f"Uses {tool}", 0.7, key=tool.lower())
            )
        for m in SCALE.finditer(text):
            unit = m.group("unit").lower()
            found.append(
                Detection(NoteKind.REQUIREMENT, f"{m.group('n')} {unit}", 0.7, key=f"scale:{unit}")
            )
        m = NEED.search(text)
        if m:
            key = f"need:{_key(m.group('rest'))}"
            found.append(Detection(NoteKind.REQUIREMENT, _clip(text), 0.55, key=key))
        if PAIN.search(text) and not any(d.kind == NoteKind.OBJECTION for d in found):
            found.append(Detection(NoteKind.PAIN_POINT, _clip(text), 0.5, key=_key(text)))
        if BUDGET.search(text):
            found.append(Detection(NoteKind.BUDGET, _clip(text), 0.6, key=_key(text)))
        if TIMELINE.search(text):
            found.append(Detection(NoteKind.TIMELINE, _clip(text), 0.55, key=_key(text)))
        if DECISION_MAKER.search(text):
            found.append(Detection(NoteKind.DECISION_MAKER, _clip(text), 0.65, key=_key(text)))
    m = NEXT_STEP.search(text)
    if m:
        found.append(Detection(NoteKind.NEXT_STEP, _clip(text), 0.6, key=m.group(0).lower()))
    return found


# ---- Agenda topic matching -------------------------------------------------------------------

TOPIC_SYNONYMS: dict[str, set[str]] = {
    "budget": set(
        "budget cost costs price pricing spend afford investment rupees lakh lakhs crore".split()
    ),
    "timeline": set(
        "timeline when deadline month months quarter week weeks date start launch soon".split()
    ),
    "decision": set(
        "decision decide decides approve approval boss owner director partner final".split()
    ),
    "current": set(
        "currently today use using process manage track excel tally system handle".split()
    ),
    "pain": set(
        "problem problems issue issues pain difficult challenge slow errors mistakes headache".split()
    ),
    "scale": set(
        "locations branches stores shops users employees orders volume outlets warehouses many".split()
    ),
    "next": set("next follow demo meeting send share proposal quote quotation trial".split()),
    "requirement": set(
        "need needs require requirement requirements looking want features feature".split()
    ),
    "competitor": set(
        "competitor competitors alternative alternatives vendor vendors compare comparing".split()
    ),
}
_KEYWORD_TO_GROUP = {word: group for group, words in TOPIC_SYNONYMS.items() for word in words}
# Very generic words would match almost every utterance.
_TOO_GENERIC = {"use", "using", "want", "many", "today", "start", "when", "process"}
_WORDS = re.compile(r"[a-z0-9]+")


def expand_keywords(keywords: str | None) -> set[str]:
    words = set((keywords or "").split())
    expanded = set(words)
    for w in words:
        group = _KEYWORD_TO_GROUP.get(w)
        if group:
            expanded |= TOPIC_SYNONYMS[group]
    return (expanded - _TOO_GENERIC) | (words & _TOO_GENERIC)


def topic_matches(expanded_keywords: set[str], text: str) -> bool:
    tokens = set(_WORDS.findall(text.lower()))
    return bool(tokens & expanded_keywords)


def word_count(text: str) -> int:
    return len(_WORDS.findall(text))
