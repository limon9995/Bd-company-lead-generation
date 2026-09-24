"""Decision-maker extraction with a hallucination guard.

The LLM only proposes candidates. A candidate is kept only if the person's name literally
appears in the fetched source text; if the quoted evidence isn't verbatim, confidence is cut.
"""
import logging
from dataclasses import dataclass, field

from app.services.llm import LLMProvider
from app.services.normalize import name_key, person_key, squash_ws
from app.services.scoring import bucket, prominence, score, title_rank

log = logging.getLogger(__name__)

SYSTEM = (
    "You extract facts from provided text only. Never guess or use outside knowledge. "
    "If the text does not explicitly name a person with a role, return an empty list."
)
MAX_SOURCE_CHARS = 24000


@dataclass
class Source:
    kind: str  # "website" | "search"
    url: str
    text: str


@dataclass
class Candidate:
    name: str
    title: str
    source_url: str
    evidence: str
    on_website: bool = False
    in_search: bool = False
    evidence_exact: bool = True
    linkedin_url: str = ""
    sources: list[dict] = field(default_factory=list)
    confidence: int = 0
    breakdown: list[str] = field(default_factory=list)

    @property
    def rank(self) -> int:
        return title_rank(self.title)

    @property
    def bucket(self) -> str:
        return bucket(self.confidence)


def _norm(s: str) -> str:
    return squash_ws(s).lower()


def build_prompt(company: str, targets: list[str], sources: list[Source]) -> str:
    chunks, used = [], 0
    for i, s in enumerate(sources):
        block = f"### SOURCE {i} ({s.kind}) URL: {s.url}\n{s.text}\n"
        if used + len(block) > MAX_SOURCE_CHARS:
            block = block[: max(0, MAX_SOURCE_CHARS - used)]
        chunks.append(block)
        used += len(block)
        if used >= MAX_SOURCE_CHARS:
            break
    return (
        f"Company: {company}\n"
        f"Roles we care about (most senior first): {', '.join(targets) or 'CEO, Managing Director, Founder, Chairman'}\n\n"
        "From the sources below, list people who hold a leadership / decision-making role AT THIS COMPANY.\n"
        "Return a JSON array; each item: {\"name\": str, \"title\": str, \"source_index\": int, "
        "\"evidence_quote\": str (an exact short quote copied from the source that shows the name and role)}.\n"
        "Rules: only people explicitly named in the text; do not include people from other companies; "
        "copy the quote exactly; return [] if none.\n\n" + "\n".join(chunks)
    )


def _company_mentioned(company: str, text: str) -> bool:
    words = [w for w in name_key(company).split() if len(w) > 2]
    if not words:
        return True
    t = _norm(text)
    hits = sum(1 for w in words if w in t)
    return hits >= max(1, (len(words) + 1) // 2)


def extract_candidates(llm: LLMProvider, company: str, targets: list[str], sources: list[Source]) -> list[Candidate]:
    if not sources:
        return []
    raw = llm.generate_json(build_prompt(company, targets, sources), SYSTEM)
    if isinstance(raw, dict):
        raw = raw.get("people") or raw.get("items") or []
    out: list[Candidate] = []
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, dict):
            continue
        name = squash_ws(str(item.get("name", "")))
        title = squash_ws(str(item.get("title", "")))
        quote = squash_ws(str(item.get("evidence_quote", "")))
        try:
            src = sources[int(item.get("source_index", -1))]
        except (ValueError, TypeError, IndexError):
            continue
        if len(name) < 3 or not title:
            continue
        src_text = _norm(src.text)
        if _norm(name) not in src_text:
            log.info("discarding %r: name not present in source %s", name, src.url)
            continue  # hallucination guard
        if src.kind == "search" and not _company_mentioned(company, src.text):
            continue
        c = Candidate(name=name, title=title, source_url=src.url, evidence=quote,
                      evidence_exact=bool(quote) and _norm(quote) in src_text)
        c.on_website = src.kind == "website"
        c.in_search = src.kind == "search"
        if "linkedin.com/in/" in src.url:
            c.linkedin_url = src.url
        c.sources = [{"kind": src.kind, "url": src.url}]
        out.append(c)
    return out


def merge_and_score(cands: list[Candidate], targets: list[str]) -> list[Candidate]:
    merged: dict[str, Candidate] = {}
    for c in cands:
        k = person_key(c.name)
        if k in merged:
            m = merged[k]
            m.on_website |= c.on_website
            m.in_search |= c.in_search
            m.evidence_exact |= c.evidence_exact
            m.linkedin_url = m.linkedin_url or c.linkedin_url
            if c.rank < m.rank:
                m.title = c.title
            m.sources += [s for s in c.sources if s not in m.sources]
        else:
            merged[k] = c
    for c in merged.values():
        c.confidence, c.breakdown = score(on_website=c.on_website, in_search=c.in_search, title=c.title,
                                          targets=targets, evidence_exact=c.evidence_exact)
    return sorted(merged.values(), key=lambda c: (c.rank, -c.confidence, -prominence(c.sources)))


def search_queries(company: str, city: str, targets: list[str]) -> list[str]:
    roles = " OR ".join(f'"{t}"' for t in (targets or ["CEO", "Managing Director", "Founder", "Chairman"])[:5])
    return [f'"{company}" {city} ({roles})', f'site:linkedin.com/in "{company}"']
