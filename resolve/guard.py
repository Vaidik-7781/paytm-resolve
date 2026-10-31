"""Deterministic input guard. Runs BEFORE any model sees text and does not depend on an LLM.

- Prompt-injection detection and stripping (untrusted text is data, never instructions)
- Scam / social-engineering detection
- Legal / escalation keywords
- PII tokenisation (reversible only inside this process)
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

INJECTION = [
    r"ignore (all |any |the )?(previous|prior|above|earlier) (instructions?|rules?|prompts?|polic(y|ies))",
    r"disregard (the |all |any )?(previous|above|system|prior)",
    r"(reveal|show|print|repeat).{0,25}(system prompt|your instructions|hidden (rules|prompt))",
    r"\byou are now\b", r"developer mode", r"\bjailbreak\b",
    r"act as (an? |the )?(admin|administrator|root|supervisor|manager|agent)",
    r"\bas (the )?(admin|administrator|system|supervisor)\b",
    r"override (the )?(policy|limits?|rules?|checks?|safeguards?)",
    r"(skip|bypass|disable) (the )?(verification|policy|checks?|approval)",
    r"(refund|transfer|send|pay|credit)\b.{0,40}\b(to|into)\b.{0,30}(account|a/c|acct|upi|vpa|wallet)",
    r"पिछले (सभी )?निर्देश", r"निर्देशों? को अनदेखा", r"सभी नियम.{0,10}अनदेखा",
    r"निर्देश(ों)?\s*(को)?\s*(भूल|अनदेखा|नज़रअंदाज़|नजरअंदाज)", r"(नियम|पॉलिसी|नीति)\s*(को)?\s*(तोड़|अनदेखा|भूल|हटा)",
    r"(इस|इसी)\s*खाते\s*(में|मे)\s*(रिफंड|पैसे|पैसा|राशि).{0,20}(भेज|डाल|ट्रांसफर)",
    r"pichle (sabhi )?(instructions?|nirdesh)", r"instructions? ignore kar",
    r"(pichle|purane|sabhi|saare)\s+(instructions?|nirdesh|niyam|rules?).{0,20}(bhool|ignore|hata|bhulo)",
    r"(instructions?|rules?|niyam|policy)\s*(ko\s*)?(ignore|bhool|bhulo|hata|tod)\w*",
    r"(is|iss|isi)\s+account\s*(mein|me)\s*(refund|paise|paisa)?.{0,20}(bhej|transfer|daal)",
]
SCAM = [
    r"\botp\b.{0,40}(shar|bata|diya|de diya|dena|told|gave|given)", r"(shar|bata|gave|told|diya).{0,40}\botp\b",
    r"anydesk|teamviewer|quick ?support|screen ?share",
    r"kyc.{0,40}(expire|update|block|suspend).{0,60}(call|caller|link|message)",
    r"(call|caller|phone|rang).{0,50}(kyc|otp|block|suspend|pin)",
    r"lottery|lucky draw|prize money|\bkbc\b", r"ओटीपी", r"केवाईसी.{0,30}(कॉल|फोन|लिंक)", r"फोन.{0,20}केवाईसी",
]
LEGAL = [r"\b(consumer court|legal notice|lawyer|advocate|ombudsman|police complaint|fir)\b", r"कानूनी|वकील"]


# ---- fuzzy layer: survives typos/transpositions that defeat exact regexes ------------------------------------
VERBS = {"ignore", "disregard", "forget", "override", "bypass", "skip", "disable", "bhool", "bhulo", "hatao", "hata", "ignor"}
OBJECTS = {"previous", "prior", "instructions", "instruction", "rules", "policy", "policies", "checks", "verification", "safeguards", "system", "prompt", "above", "nirdesh", "niyam", "pichle", "purane"}
MONEY = {"refund", "transfer", "send", "credit", "pay"}
DEST = {"account", "acct", "upi", "vpa", "wallet"}


def _osa(a: str, b: str) -> int:
    """Optimal string alignment distance (Damerau-Levenshtein with adjacent transpositions)."""
    d = [[0] * (len(b) + 1) for _ in range(len(a) + 1)]
    for i in range(len(a) + 1):
        d[i][0] = i
    for j in range(len(b) + 1):
        d[0][j] = j
    for i in range(1, len(a) + 1):
        for j in range(1, len(b) + 1):
            c = 0 if a[i - 1] == b[j - 1] else 1
            d[i][j] = min(d[i - 1][j] + 1, d[i][j - 1] + 1, d[i - 1][j - 1] + c)
            if i > 1 and j > 1 and a[i - 1] == b[j - 2] and a[i - 2] == b[j - 1]:
                d[i][j] = min(d[i][j], d[i - 2][j - 2] + 1)
    return d[-1][-1]


def _fz(tok: str, vocab: set[str]) -> bool:
    if tok in vocab:
        return True
    return len(tok) >= 5 and any(abs(len(tok) - len(v)) <= 1 and _osa(tok, v) <= 1 for v in vocab if len(v) >= 5)


def fuzzy_injection(sentence: str) -> bool:
    toks = re.findall(r"[a-z]+", sentence.lower())
    verbs = any(_fz(t, VERBS) for t in toks)
    objs = any(_fz(t, OBJECTS) for t in toks)
    money = any(_fz(t, MONEY) for t in toks)
    dest = any(_fz(t, DEST) for t in toks)
    has_acct_number = bool(re.search(r"\b\d{9,18}\b|[\w.\-]{2,}@[a-z]{2,}", sentence))
    return (verbs and objs) or (money and dest and has_acct_number) or (money and has_acct_number)


def _any(patterns: list[str], text: str) -> list[str]:
    return [p for p in patterns if re.search(p, text, re.IGNORECASE | re.UNICODE)]


@dataclass
class GuardResult:
    injection: bool = False
    scam: bool = False
    legal: bool = False
    clean_text: str = ""
    removed: list[str] = field(default_factory=list)
    masked_text: str = ""
    pii_found: dict[str, int] = field(default_factory=dict)
    injected_targets: list[str] = field(default_factory=list)  # account numbers / VPAs smuggled in the message

    @property
    def flags(self) -> list[str]:
        out = []
        if self.injection:
            out.append("injection_attempt")
        if self.scam:
            out.append("scam_suspected")
        if self.legal:
            out.append("legal_threat")
        return out


PII_PATTERNS = {
    "PAN": r"\b[A-Z]{5}\d{4}[A-Z]\b",
    "AADHAAR": r"\b\d{4}\s?\d{4}\s?\d{4}\b",
    "CARD": r"\b(?:\d[ -]?){13,19}\b",
    "ACCOUNT": r"\b\d{9,18}\b",
    "PHONE": r"(?<!\d)(?:\+91[\s-]?)?[6-9]\d{9}(?!\d)",
    "VPA": r"\b[\w.\-]{2,}@[a-z]{2,}\b",
}


def mask_pii(text: str) -> tuple[str, dict[str, int]]:
    counts: dict[str, int] = {}
    out = text
    for name, pat in PII_PATTERNS.items():
        def repl(m, name=name):
            counts[name] = counts.get(name, 0) + 1
            return f"[{name}_{counts[name]}]"
        out = re.sub(pat, repl, out)
    return out, counts


def _sentences(text: str) -> list[str]:
    parts = re.split(r"(?<=[.!?।\n])\s+", text.strip())
    return [p for p in parts if p]


def inspect(text: str) -> GuardResult:
    res = GuardResult()
    kept: list[str] = []
    for s in _sentences(text):
        if _any(INJECTION, s) or fuzzy_injection(s):
            res.injection = True
            res.removed.append(s.strip())
            res.injected_targets += re.findall(r"\b\d{9,18}\b|[\w.\-]{2,}@[a-z]{2,}", s)
        else:
            kept.append(s)
    res.clean_text = " ".join(kept).strip()
    res.scam = bool(_any(SCAM, text))
    res.legal = bool(_any(LEGAL, text))
    res.masked_text, res.pii_found = mask_pii(res.clean_text)
    return res
