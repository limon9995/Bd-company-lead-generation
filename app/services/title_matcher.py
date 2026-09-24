"""Rule-based decision-maker extraction - used when no Gemini key is set.

Crawled page text keeps one HTML text node per line, so leadership blocks look like
"Vice Chancellor / Prof. Dr. Md. Zakir Hossain" (title first) or "Rahim Uddin / Managing Director"
(name first). We split the text into short segments, label each as a person's name or a job title,
and pair neighbours. The layout of each run is decided by which kind comes first, so an alternating
list (title, name, title, name ...) is never paired off by one.
"""
import re

from app.services.extractor import Candidate, Source, _company_mentioned
from app.services.normalize import name_key, squash_ws
from app.services.scoring import TITLE_RANKS, matches_target

MAX_PER_SOURCE = 15

# Segment boundaries: new line, "|", ":", ",", brackets, dashes (a hyphen only when spaced: "Rahim - CEO").
_SEP = re.compile(r"\n|\||:|,|;|\(|\)|–|—| - |•")

_TITLE_WORDS = [w for rank, words in TITLE_RANKS if rank <= 2 for w in words]
_TITLE_RE = re.compile(r"\b(" + "|".join(re.escape(w) for w in _TITLE_WORDS) + r")\b", re.I)
_MD_TITLE = re.compile(r"\bM\.?D\b")  # "MD & CEO"; case-sensitive so the name prefix "Md." is not a title
# Not a current decision maker of this company (or not a job title at all).
_BAD_TITLE = re.compile(r"\b(former|late|ex|republic|minister|guest|alumni|student|students|parent|retired|assistant|"
                        r"associate|lecturer|office|directory|portal|tribute|meeting|minutes|schedule|club)\b|ex-|@",
                        re.I)
_TITLE_NOISE = re.compile(r"^(message|profile|speech)\s+(from|of)\s+|^(the|our)\s+|^hon['’]?ble\s+|^hono(u)?rable\s+|"
                          r"['’]s\s+(message|desk|speech|profile)$", re.I)
# Lower-case words allowed inside a title ("Chairman of the Board of Trustees").
_TITLE_GLUE = {"of", "the", "and", "for", "&", "in", "at", "to"}
# Text right after the title that shows it belongs to another organisation.
_OTHER_ORG = re.compile(r"\b(republic|government|ministry|minister)\b", re.I)
# Pages that list outsiders, department chairs or club officers rather than the company's leaders.
_SKIP_URL = re.compile(r"/(news|notice|notices|event|events|details_event|department|departments|faculty|"
                       r"faculty-member[\w-]*|club|clubs|blog)(/|$|\?)", re.I)

_HONORIFIC = re.compile(r"^(prof(essor)?|dr|mr|mrs|ms|md|mohd|engr|eng|alhaj|al-haj|barrister|adv|advocate|"
                        r"justice|major|col|brig|gen|capt|lt|emeritus|sir)\.?$", re.I)
_DEGREE = re.compile(r"^((ph\.?\s?d|mba|bba|fca|fcma|acca|acma|cpa|mbbs|fcps|frcs|m\.?\s?sc|b\.?\s?sc|m\.?\s?a|b\.?\s?a|"
                     r"llb|llm|dba|ndc|psc|afwc|pe|p\.?\s?eng|retd|\(retd\))\.?\s*)+$", re.I)
_NAME_TOKEN = re.compile(r"^[A-Z][A-Za-z'’.\-]*$")
# Words that make a segment a menu item / organisation, not a person.
_NOT_NAME = {
    "university", "college", "school", "institute", "academy", "office", "message", "profile", "the", "of", "and",
    "for", "in", "at", "to", "our", "bangladesh", "dhaka", "chittagong", "chattogram", "department", "board",
    "trustees", "trustee", "read", "more", "welcome", "about", "contact", "admission", "admissions", "home", "news",
    "limited", "ltd", "group", "company", "international", "center", "centre", "program", "programs", "faculty",
    "campus", "research", "policy", "council", "committee", "team", "management", "leadership", "administration",
    "services", "service", "industries", "foundation", "trust", "hospital", "bank", "plc", "notice", "events",
    "apply", "online", "login", "click", "here", "view", "all", "details", "gallery", "career", "careers",
    "science", "technology", "engineering", "business", "studies", "arts", "education", "library", "hall",
    "vision", "mission", "history", "overview", "information", "tribute", "legacy", "founders", "members",
    "schedule", "directory", "portal", "staff", "ranking", "methodology", "links", "link", "share", "this",
    "facility", "relations", "public", "meeting", "meetings", "dates", "minutes", "academic", "dean", "recipient",
    "padak", "award", "proctor", "bismillahir", "rahmaanir", "raheem", "important", "quick", "useful", "follow",
    "us", "pioneer", "page", "pages", "download", "downloads", "form", "forms", "result", "results", "journal", "office",
}
_MAX_NAME_TOKENS = 6


def _segments(text: str):
    """(start, end, cleaned text) of each short piece of the page text."""
    pos = 0
    for m in [*_SEP.finditer(text), None]:
        end = m.start() if m else len(text)
        raw = text[pos:end]
        seg = squash_ws(raw).strip(" .*\"'“”-")
        if seg:
            lead = len(raw) - len(raw.lstrip())
            yield pos + lead, pos + len(raw.rstrip()), seg
        if m is None:
            break
        pos = m.end()


def _is_name(seg: str, company_key: set[str]) -> bool:
    if len(seg) > 60 or any(ch.isdigit() for ch in seg):
        return False
    tokens = seg.replace(".", ". ").split()
    core = [t for t in tokens if not _HONORIFIC.match(t)]
    if not 2 <= len(core) <= _MAX_NAME_TOKENS:
        return False
    if not all(_NAME_TOKEN.match(t) for t in tokens):
        return False
    low = {t.lower().strip(".'’") for t in core}
    if low & _NOT_NAME or _TITLE_RE.search(" ".join(core)) or _MD_TITLE.search(" ".join(core)):
        return False
    # Pieces of the company's own name ("East West", "Islami Bank") or its acronym ("Go Ahead AUB") are not people.
    if any(t.isupper() and len(t) <= 6 and t.lower() in company_key for t in core):
        return False
    return not (company_key and len(low & company_key) >= max(1, len(low) - 1))


def _clean_title(seg: str) -> str:
    prev = None
    while prev != seg:
        prev, seg = seg, _TITLE_NOISE.sub("", seg).strip()
    return seg


def _is_title(seg: str, targets: list[str]) -> bool:
    if len(seg) > 70 or len(seg.split()) > 8 or any(ch.isdigit() for ch in seg) or _BAD_TITLE.search(seg):
        return False
    if not seg[0].isupper():
        return False
    # A title is capitalised words plus glue ("of the"); other lower-case words mean a sentence
    # ("Professor X was Chairman of ...", "Prior permission from Registrar").
    for w in re.split(r"[\s\-/]+", seg):
        w = w.strip(".,'’")
        if w and w[0].islower() and w not in _TITLE_GLUE and not _TITLE_RE.fullmatch(w):
            return False
    return bool(_TITLE_RE.search(seg) or _MD_TITLE.search(seg)) or any(
        re.search(r"\b" + re.escape(t.strip()) + r"\b", seg, re.I) for t in targets if t.strip())


def _display_name(seg: str) -> str:
    return seg.title() if seg.isupper() else seg


def find_people(company: str, targets: list[str], sources: list[Source]) -> list[Candidate]:
    company_key = {w for w in name_key(company).split() if len(w) > 2}
    out: list[Candidate] = []
    for src in sources:
        if src.kind == "search" and not _company_mentioned(company, src.text):
            continue
        if src.kind == "website" and _SKIP_URL.search(src.url):
            continue
        labelled = []  # (kind, start, end, text); kind: "N" name, "T" title, None = breaks a run
        for start, end, seg in _segments(src.text):
            if _DEGREE.match(seg):
                continue  # "PhD" after "Prof. X, PhD" must not split the name from its title
            if _is_name(seg, company_key):
                labelled.append(("N", start, end, seg))
            elif _clean_title(seg) and _is_title(_clean_title(seg), targets):
                labelled.append(("T", start, end, _clean_title(seg)))
            else:
                labelled.append((None, start, end, seg))
        found = 0
        i = 0
        while i < len(labelled) - 1 and found < MAX_PER_SOURCE:
            a, b = labelled[i], labelled[i + 1]
            if a[0] and b[0] and a[0] != b[0]:
                first = a[0]  # layout of this run: keep pairing in the same direction
                j = i
                while j < len(labelled) - 1 and found < MAX_PER_SOURCE:
                    x, y = labelled[j], labelled[j + 1]
                    if x[0] != first or y[0] not in ("N", "T") or y[0] == first:
                        break
                    name, title = (x, y) if first == "N" else (y, x)
                    after = " ".join(s[3] for s in labelled[j + 2:j + 4])
                    if _OTHER_ORG.search(after):  # "Hon'ble President / People's Republic of Bangladesh"
                        j += 2
                        continue
                    c = Candidate(name=_display_name(name[3]), title=title[3], source_url=src.url,
                                  evidence=squash_ws(src.text[x[1]:y[2]])[:300], evidence_exact=True)
                    c.on_website = src.kind == "website"
                    c.in_search = src.kind == "search"
                    if "linkedin.com/in/" in src.url:
                        c.linkedin_url = src.url
                    c.sources = [{"kind": src.kind, "url": src.url}]
                    out.append(c)
                    found += 1
                    j += 2
                    if j < len(labelled) and labelled[j][0] != first:
                        break
                i = max(j, i + 1)
            else:
                i += 1
    return out
