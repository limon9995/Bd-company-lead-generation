"""AI browser agent: Gemini looks at the page and decides what a person would do next.

Loop: snapshot (visible text + numbered buttons/links/inputs) → Gemini picks ONE action →
we check it against guardrails → do it in the real browser → repeat.

Actions: type (into a search box, then Enter) · click · scroll · extract (list businesses on this page) ·
back · done.

Guardrails (enforced in code, not left to the AI):
  * stays on the starting website (links to other sites are refused);
  * never types into password / email fields or any form that has a password field (no logins, no sign-ups);
  * never clicks login / sign-up / buy / pay / delete style controls;
  * step limit, and it stops if it repeats the same action 3 times;
  * CAPTCHA / block pages stop it (SourceBlocked), like every browser source.
"""
import json
import re
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlparse

from app.services.directory import extract_entries
from app.services.llm import LLMProvider

SNAPSHOT_JS = """
() => {
  const sel = 'a[href], button, input, textarea, select, [role=button], [role=link]';
  const out = []; let id = 0;
  document.querySelectorAll('[data-agent-id]').forEach(e => e.removeAttribute('data-agent-id'));
  for (const el of document.querySelectorAll(sel)) {
    const r = el.getBoundingClientRect(), st = getComputedStyle(el);
    if (r.width === 0 || r.height === 0 || st.visibility === 'hidden' || st.display === 'none') continue;
    if ((el.getAttribute('type') || '').toLowerCase() === 'hidden') continue;
    id += 1; el.setAttribute('data-agent-id', String(id));
    const label = (el.innerText || el.value || el.getAttribute('aria-label') || el.getAttribute('placeholder')
                   || el.getAttribute('title') || el.getAttribute('name') || '').trim().replace(/\\s+/g, ' ').slice(0, 80);
    out.push({id, tag: el.tagName.toLowerCase(), type: (el.getAttribute('type') || '').toLowerCase(), label,
              href: el.getAttribute('href') || '',
              in_login_form: !!(el.form && el.form.querySelector('input[type=password]'))});
    if (out.length >= 150) break;
  }
  return out;
}
"""
RISKY_LABEL = re.compile(r"\b(log ?in|sign ?in|sign ?up|register|log ?out|password|checkout|buy|pay|cart|delete|"
                         r"subscribe|download app|লগইন|নিবন্ধন)\b", re.I)
SYSTEM = ("You control a web browser to collect a list of businesses from one website. Reply with ONE JSON object only. "
          "Never try to log in, sign up, buy, or leave the website.")


@dataclass
class AgentReport:
    steps: int = 0
    extracted: int = 0
    pages_extracted: int = 0
    stop_reason: str = ""
    log: list[str] = field(default_factory=list)


def same_site(url: str, start_host: str) -> bool:
    host = (urlparse(url).hostname or "").lower().removeprefix("www.")
    base = start_host.lower().removeprefix("www.")
    return host == base or host.endswith("." + base)


def build_prompt(goal: str, url: str, text: str, elements: list[dict], history: list[str], remaining: int) -> str:
    el_lines = "\n".join(
        f"[{e['id']}] {e['tag']}{'(' + e['type'] + ')' if e['type'] else ''} \"{e['label']}\"" + (f" -> {e['href'][:80]}" if e["href"] else "")
        for e in elements)
    hist = "\n".join(history[-8:]) or "(none yet)"
    return (
        f"GOAL: {goal}\n\nCURRENT URL: {url}\nSTEPS LEFT: {remaining}\n\nWHAT YOU DID SO FAR:\n{hist}\n\n"
        f"VISIBLE PAGE TEXT (truncated):\n{text[:6000]}\n\nINTERACTIVE ELEMENTS:\n{el_lines}\n\n"
        "Choose the next action. JSON: {\"thought\": short reason, \"action\": one of "
        "\"type\" | \"click\" | \"scroll\" | \"extract\" | \"back\" | \"done\", \"id\": element number (for type/click), "
        "\"text\": text to type (for type)}.\n"
        "- Use \"extract\" when the page shows business listings you have not extracted yet.\n"
        "- After extracting, look for a Next / page-number link, or scroll for more.\n"
        "- \"type\" types into a search box and presses Enter.\n- \"done\" when there is nothing more to collect."
    )


def check_action(action: dict, elements: dict[int, dict], start_host: str, current_url: str) -> str | None:
    """Return a refusal reason, or None if allowed."""
    kind = action.get("action")
    if kind not in ("type", "click", "scroll", "extract", "back", "done"):
        return f"unknown action {kind!r}"
    if kind in ("type", "click"):
        try:
            el = elements[int(action.get("id"))]
        except (TypeError, ValueError, KeyError):
            return "no such element"
        if el["in_login_form"] or el["type"] in ("password", "email"):
            return "refused: login / account form"
        if RISKY_LABEL.search(el["label"] or ""):
            return f"refused: risky control '{el['label']}'"
        if kind == "type":
            if el["tag"] not in ("input", "textarea") or el["type"] in ("checkbox", "radio", "submit", "button", "file"):
                return "can only type into a text box"
            if not str(action.get("text", "")).strip():
                return "nothing to type"
        if kind == "click" and el["href"]:
            href = el["href"].strip()
            if href.lower().startswith(("mailto:", "tel:", "whatsapp:", "sms:")):
                return "refused: contact link"
            if not href.lower().startswith("javascript:") and not href.startswith("#"):
                if not same_site(urljoin(current_url, href), start_host):
                    return "refused: link leaves the website"
    return None


def run(session, llm: LLMProvider, start_url: str, goal: str, *, max_steps: int, on_entries, before_llm=None,
        after_llm=None) -> AgentReport:
    """`on_entries(url, entries) -> int` stores extracted businesses and returns how many were new."""
    page = session.goto(start_url, wait_until="load")
    start_host = urlparse(page.url).hostname or ""
    report = AgentReport()
    history: list[str] = []
    extracted_urls: set[str] = set()
    last, repeats = None, 0
    while report.steps < max_steps:
        report.steps += 1
        elements = page.evaluate(SNAPSHOT_JS)
        by_id = {e["id"]: e for e in elements}
        try:
            text = page.inner_text("body")
        except Exception:  # noqa: BLE001
            text = ""
        if before_llm:
            before_llm()
        raw = llm.generate_json(build_prompt(goal, page.url, text, elements, history, max_steps - report.steps + 1), SYSTEM)
        if after_llm:
            after_llm()
        action = raw[0] if isinstance(raw, list) and raw else raw if isinstance(raw, dict) else {}
        sig = json.dumps({k: action.get(k) for k in ("action", "id", "text")}, sort_keys=True) + page.url
        repeats = repeats + 1 if sig == last else 0
        last = sig
        if repeats >= 2:
            report.stop_reason = "repeated the same action"
            break
        refusal = check_action(action, by_id, start_host, page.url)
        kind = action.get("action")
        if refusal:
            history.append(f"{report.steps}. {kind} {action.get('id', '')} -> {refusal}")
            report.log.append(refusal)
            continue
        if kind == "done":
            report.stop_reason = "agent finished"
            break
        if kind == "extract":
            if page.url in extracted_urls and not action.get("force"):
                history.append(f"{report.steps}. extract -> this page was already extracted; move on")
                continue
            if before_llm:
                before_llm()
            entries = extract_entries(llm, page.url, page.content())
            if after_llm:
                after_llm()
            added = on_entries(page.url, entries)
            extracted_urls.add(page.url)
            report.extracted += added
            report.pages_extracted += 1
            history.append(f"{report.steps}. extract -> {len(entries)} businesses on page ({added} new)")
            continue
        session.wait_politely()
        if kind == "scroll":
            page.mouse.wheel(0, 900)
            page.wait_for_timeout(1200)
            history.append(f"{report.steps}. scroll")
        elif kind == "back":
            page.go_back(wait_until="domcontentloaded")
            history.append(f"{report.steps}. back -> {page.url}")
        else:
            loc = page.locator(f'[data-agent-id="{int(action["id"])}"]').first
            label = by_id[int(action["id"])]["label"]
            if kind == "type":
                from app.services.browser import type_like_person

                type_like_person(page, loc, str(action["text"])[:100])
                history.append(f"{report.steps}. typed '{str(action['text'])[:40]}' into [{action['id']}] and pressed Enter")
            else:
                loc.click()
                history.append(f"{report.steps}. clicked [{action['id']}] '{label}'")
            try:
                page.wait_for_load_state("domcontentloaded", timeout=15000)
            except Exception:  # noqa: BLE001 - in-page update without navigation
                pass
            page.wait_for_timeout(1200)
        session.after_action()
        if not same_site(page.url, start_host):  # e.g. a button that redirected elsewhere
            report.stop_reason = f"left the website ({page.url[:80]})"
            break
    else:
        report.stop_reason = "step limit reached"
    return report
