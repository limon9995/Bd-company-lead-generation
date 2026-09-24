"""Explainable, rule-based confidence scoring for decision-maker candidates."""
import re

TITLE_RANKS: list[tuple[int, tuple[str, ...]]] = [
    (1, ("chairman", "chairperson", "managing director", "chief executive", "ceo", "founder", "owner",
         "proprietor", "president", "vice chancellor", "vice-chancellor", "principal", "country manager")),
    (2, ("executive director", "director", "cto", "coo", "cfo", "cmo", "chief", "general manager", "head of",
         "vice president", "partner", "pro-vice chancellor", "rector", "registrar", "superintendent")),
    (3, ("manager", "head", "lead", "coordinator", "administrator", "officer")),
]
_MD = re.compile(r"\bM\.?D\b")
_MEDICAL_DEGREE = re.compile(r"mbbs|fcps|frcs|mrcp|md\s*\(", re.I)


def title_rank(title: str) -> int:
    t = (title or "").lower()
    # "MD" in BD corporate usage = Managing Director; "MBBS, MD (Medicine)" is a medical degree.
    if _MD.search(title or "") and not _MEDICAL_DEGREE.search(title or ""):
        return 1
    for rank, words in TITLE_RANKS:
        if any(w in t for w in words):
            return rank
    return 9


def matches_target(title: str, targets: list[str]) -> bool:
    t = (title or "").lower()
    return any(tt.strip().lower() and tt.strip().lower() in t for tt in targets)


def score(*, on_website: bool, in_search: bool, title: str, targets: list[str], evidence_exact: bool) -> tuple[int, list[str]]:
    points = 0
    why: list[str] = []
    if on_website:
        points += 40
        why.append("+40 named on the company's own website")
    if in_search:
        points += 25
        why.append("+25 found in a search result that mentions the company")
    if matches_target(title, targets):
        points += 15
        why.append("+15 title matches campaign target titles")
    if on_website and in_search:
        points += 10
        why.append("+10 two independent sources agree")
    if title_rank(title) == 1:
        points += 10
        why.append("+10 top-level title")
    if not evidence_exact:
        points -= 30
        why.append("-30 AI evidence quote not found verbatim in the source")
    return max(0, min(100, points)), why


def bucket(confidence: int) -> str:
    return "high" if confidence >= 70 else "medium" if confidence >= 40 else "low"
