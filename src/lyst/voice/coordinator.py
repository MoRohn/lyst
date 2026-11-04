# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""The Lyst Voice Coordinator: what a spoken turn means, and what Lyst may do about it.

Speech is never authorisation and never authentication. A voice turn runs as the person whose signed-in session
opened the voice session, through the same calls (and so the same permission, policy and budget checks) as a typed
request. On top of that, anything that would change, send, approve or remove something is not done by voice at all:
the coordinator answers that it needs confirming on screen, and records that it was asked. A spoken "yes" or
"approve" never decides an approval.

Stopping speech and stopping work are separate, explicit operations. "Stop", "quiet", "never mind" stop Lyst talking
and leave the analysis running; only "cancel the analysis" (or "stop the task", "cancel that request") cancels it.

The classification is deliberately plain (word rules, no model): it must be predictable, it runs on every turn, and
a misheard question costs a re-ask while a misheard action would cost far more. Anything that is not clearly one of
the control intents is treated as an analytical question and handed to the analyst.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# politeness and filler a person says before the verb
_LEAD = re.compile(r"^(?:(?:ok(?:ay)?|so|um+|uh+|hey|hi|lyst|please|now|and|then|alright|right|well|could you|can you|"
                   r"would you|will you|i want you to|i'd like you to|i need you to|go ahead and|let's|lets)[,.!\s]+)+",
                   re.IGNORECASE)
_WORK = r"(?:analysis|task|request|job|query|investigation|run|work|forecast|question)"
_CANCEL_WORK = re.compile(rf"^(?:cancel|stop|abort|kill|halt|end)\s+(?:the|that|this|my)?\s*(?:running\s+|current\s+|last\s+)?{_WORK}\b",
                          re.IGNORECASE)
_STOP_SPEAKING = re.compile(r"^(?:stop(?: talking| speaking| it| that)?|quiet|be quiet|shh+|hush|silence|pause|hold on|wait|"
                            r"never ?mind|forget (?:it|that)|cancel(?: that| it)?|enough|ok(?:ay)? stop)\b[.!]?$", re.IGNORECASE)
_REPEAT = re.compile(r"^(?:repeat(?: that)?|say (?:that|it) again|what did you (?:just )?say|come again|pardon)\b", re.IGNORECASE)
_STATUS = re.compile(r"^(?:status|(?:is|are) (?:it|you) (?:done|finished|ready)|how(?:'s| is) it going|still working|"
                     r"what(?:'s| is) (?:the )?status|any (?:news|update))\b", re.IGNORECASE)
_CATCH_UP = re.compile(r"^(?:what did (?:you|lyst) (?:find|do|finish)(?: while i was (?:away|out|gone))?|"
                       r"what(?:'s| has) (?:happened|changed|finished)(?: since)?|catch me up|bring me up to (?:date|speed)|"
                       r"what did i miss|anything new)\b", re.IGNORECASE)
# a bare assent: speech cannot confirm anything
_ASSENT = re.compile(r"^(?:yes|yeah|yep|sure|confirm(?:ed)?|approved?|do it|go ahead|ship it|proceed|okay do it|"
                     r"sounds good|that's fine)\b[.!]?$", re.IGNORECASE)
# verbs that change, send, decide or remove something
ACTION_VERBS = ("approve", "reject", "deny", "decline", "delete", "remove", "drop", "purge", "erase", "wipe", "publish",
                "send", "email", "share", "post", "export", "download", "schedule", "grant", "revoke", "invite", "transfer",
                "pay", "deploy", "merge", "archive", "rename", "disable", "enable", "rotate", "reset", "change", "modify",
                "set", "connect", "disconnect", "sign", "submit", "retire", "promote", "classify", "define", "overwrite")
_ACTION = re.compile(r"^(?:" + "|".join(ACTION_VERBS) + r")\b", re.IGNORECASE)

INTENTS = ("question", "stop_speaking", "cancel_work", "repeat", "status", "catch_up", "action", "assent", "silence")


@dataclass(frozen=True)
class Intent:
    kind: str            # one of INTENTS
    text: str            # the utterance with leading filler removed
    verb: str | None = None


def classify(utterance: str) -> Intent:
    raw = " ".join((utterance or "").split())
    text = _LEAD.sub("", raw + " ").strip()
    if not text or not re.search(r"\w", text):
        return Intent("silence", "")
    if _CANCEL_WORK.match(text):
        return Intent("cancel_work", text)
    if _STOP_SPEAKING.match(text):
        return Intent("stop_speaking", text)
    if _REPEAT.match(text):
        return Intent("repeat", text)
    if _STATUS.match(text):
        return Intent("status", text)
    if _CATCH_UP.match(text):
        return Intent("catch_up", text)
    if _ASSENT.match(text):
        return Intent("assent", text)
    if m := _ACTION.match(text):
        return Intent("action", text, m.group(0).lower())
    return Intent("question", text)


# what Lyst says back, by intent; analytical answers are never composed here, only read from the verified result
REPLIES = {
    "silence": "I didn't catch anything.",
    "question": "Checking that now.",
    "stop_speaking": "",
    "stop_speaking_running": "Okay. The analysis is still running; say 'cancel the analysis' to stop it.",
    "cancel_running": "Cancelled the analysis.",
    "cancel_idle": "Nothing is running.",
    "repeat_none": "There's nothing to repeat yet.",
    "status_none": "Nothing is running.",
    "catch_up_none": "Nothing new has finished since you were last here.",
    "action": "I can't {verb} things by voice. I've put it on screen so you can check and confirm it there.",
    "assent": "Voice can't confirm or approve anything. Please use the button on screen.",
}

# speaking styles: how long a read-back may run, and the line given to the duplex persona. The model's own
# behaviour is not tuned beyond its published prompt conditioning, so a style only changes length and register.
STYLES: dict[str, dict] = {
    "professional": {"limit": 600, "persona": "Speak clearly and concisely, in plain business English."},
    "conversational": {"limit": 600, "persona": "Speak naturally, as a colleague would, without filler."},
    "executive": {"limit": 280, "persona": "Be brief and decision-oriented."},
    "technical": {"limit": 900, "persona": "Be precise with technical and statistical terms."},
    "briefing": {"limit": 900, "persona": "Narrate prepared findings in a calm, structured way."},
}


# -- speaking numbers -----------------------------------------------------------------------------------------------
_SCALE = {"k": "thousand", "m": "million", "bn": "billion", "b": "billion", "t": "trillion"}
_CURRENCY = {"$": "dollars", "€": "euros", "£": "pounds", "¥": "yen"}
_MONTHS = {"jan": "January", "feb": "February", "mar": "March", "apr": "April", "jun": "June", "jul": "July",
           "aug": "August", "sep": "September", "sept": "September", "oct": "October", "nov": "November", "dec": "December"}
_MONEY = re.compile(r"(?P<sign>[-−+])?(?P<cur>[$€£¥])(?P<num>\d[\d,]*(?:\.\d+)?)\s?(?P<scale>bn|[kKmMbBtT])?\b")
_PCT = re.compile(r"(?P<sign>[-−+])?(?P<num>\d+(?:\.\d+)?)\s?(?P<unit>%|pp|bps)(?!\w)")
_NEG = re.compile(r"(?<![\w.])[-−](?P<num>\d[\d,]*(?:\.\d+)?)")
_MONTH = re.compile(r"\b(?P<m>Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sept?|Oct|Nov|Dec)\.?\s+(?P<y>\d{4})\b")
_QUARTER = re.compile(r"\bQ(?P<q>[1-4])\s+(?:FY\s?)?(?P<y>\d{2,4})\b")
_ORDINAL = {"1": "first", "2": "second", "3": "third", "4": "fourth"}


def _num(text: str) -> str:
    return text.replace(",", "")


def speak_numbers(text: str) -> str:
    """Rewrite figures for the ear without changing them: "-$292.1K (-3.4%)" becomes "minus 292.1 thousand dollars
    (minus 3.4 percent)". The exact text stays on screen; this is only what is read aloud."""
    def money(m: re.Match) -> str:
        scale = (m["scale"] or "").lower()
        words = [_num(m["num"])]
        if scale:
            words.append(_SCALE[scale])
        words.append(_CURRENCY[m["cur"]])
        sign = {"-": "minus ", "−": "minus ", "+": "plus "}.get(m["sign"] or "", "")
        return sign + " ".join(words)

    def pct(m: re.Match) -> str:
        unit = {"%": "percent", "pp": "percentage points", "bps": "basis points"}[m["unit"]]
        sign = {"-": "minus ", "−": "minus ", "+": "plus "}.get(m["sign"] or "", "")
        return f"{sign}{m['num']} {unit}"

    def quarter(m: re.Match) -> str:
        y = m["y"] if len(m["y"]) == 4 else "20" + m["y"]
        return f"the {_ORDINAL[m['q']]} quarter of {y}"

    out = _MONEY.sub(money, text)
    out = _PCT.sub(pct, out)
    out = _NEG.sub(lambda m: "minus " + _num(m["num"]), out)
    out = _MONTH.sub(lambda m: f"{_MONTHS[m['m'].lower()]} {m['y']}", out)
    out = _QUARTER.sub(quarter, out)
    out = re.sub(r"\bvs\.?(?=\s)", "versus", out)
    out = re.sub(r"\bYoY\b", "year over year", out)
    out = re.sub(r"\bMoM\b", "month over month", out)
    return out


def speakable(answer: str | None, limit: int = 600) -> str:
    """A verified answer prepared for reading aloud: markdown removed, figures spoken, whole sentences up to
    ``limit`` characters, with a pointer to the screen when it was shortened."""
    if not answer:
        return ""
    text = re.sub(r"[*_`#>|]+", "", answer)
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
    text = " ".join(text.split())
    if len(text) > limit:
        cut = text[:limit]
        end = max(cut.rfind(". "), cut.rfind("? "), cut.rfind("! "))
        text = (cut[: end + 1] if end > limit // 3 else cut.rsplit(" ", 1)[0] + "…") + " The full answer is on screen."
    return speak_numbers(text)
