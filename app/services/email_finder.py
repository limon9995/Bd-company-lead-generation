"""Pick the best outreach address for a decision maker.

Order: the person's own address seen on the website > the company's own address format applied to the
person ("pattern", learned from other staff addresses on the same site) > a generic company address from
the website > a plain pattern guess on the company domain (only if the domain can receive mail).
Pattern and guessed addresses are never auto-sent (see pipeline), because bounces hurt the sender account.
Addresses on a domain that cannot receive email at all are dropped.
"""
import re
from collections import Counter
from functools import lru_cache

import dns.exception
import dns.resolver

from app.services.normalize import FREE_MAIL_DOMAINS, person_key

GENERIC_PREFIXES = ("info", "contact", "hello", "office", "admin", "support", "sales", "enquiry", "inquiry", "mail", "hr")

# Local-part formats seen in company email addresses, e.g. "rahim.uddin", "ruddin", "rahim".
FORMATS = {
    "first.last": lambda f, l: f"{f}.{l}",
    "firstlast": lambda f, l: f"{f}{l}",
    "first_last": lambda f, l: f"{f}_{l}",
    "flast": lambda f, l: f"{f[0]}{l}",
    "f.last": lambda f, l: f"{f[0]}.{l}",
    "firstl": lambda f, l: f"{f}{l[0]}",
    "last.first": lambda f, l: f"{l}.{f}",
    "first": lambda f, l: f,
    "last": lambda f, l: l,
}


def has_mx(domain: str) -> bool:
    try:
        return bool(dns.resolver.resolve(domain, "MX", lifetime=5))
    except (dns.exception.DNSException, OSError):
        return False


@lru_cache(maxsize=2048)
def mail_domain_status(domain: str) -> str:
    """'yes' = can receive mail, 'no' = certainly cannot (no such domain / null MX / no MX and no A record),
    'unknown' = DNS did not answer (offline, timeout) - treated as usable."""
    try:
        answers = dns.resolver.resolve(domain, "MX", lifetime=5)
        hosts = [str(r.exchange).rstrip(".") for r in answers]
        return "no" if hosts == [""] else "yes"  # RFC 7505 null MX: "0 ."
    except dns.resolver.NXDOMAIN:
        return "no"
    except dns.resolver.NoAnswer:
        try:  # no MX: mail goes to the domain's A record (RFC 5321 implicit MX)
            dns.resolver.resolve(domain, "A", lifetime=5)
            return "yes"
        except dns.resolver.NoAnswer:
            return "no"
        except (dns.exception.DNSException, OSError):
            return "unknown"
    except (dns.exception.DNSException, OSError):
        return "unknown"


def _name_parts(full_name: str) -> list[str]:
    return [p for p in person_key(full_name).split() if len(p) >= 3]


def personal_match(full_name: str, emails: list[str]) -> str:
    parts = _name_parts(full_name)
    for e in emails:
        local = e.split("@")[0].lower()
        if any(p in local for p in parts):
            return e
    return ""


# "tajul.islam", "soud20079" - some staff member's (or student's) own inbox
_PERSONAL_LOCAL = re.compile(r"^([a-z]{3,}[._][a-z]{3,}|[a-z]{3,}\d{3,})$")


_OFFICE_WORDS = {"info", "admission", "admissions", "office", "contact", "sales", "support", "career", "careers",
                 "registrar", "accounts", "account", "enquiry", "inquiry", "hr", "admin", "helpdesk", "service",
                 "marketing", "exam", "controller", "principal", "chairman", "director", "hello", "mail", "team"}


def _looks_personal(email: str) -> bool:
    local, _, domain = email.lower().partition("@")
    if domain in FREE_MAIL_DOMAINS or set(re.split(r"[._\d]+", local)) & _OFFICE_WORDS:
        return False  # "admission.info", "exam.controller" are office inboxes
    return bool(_PERSONAL_LOCAL.match(local))


def generic_company_email(emails: list[str], domain: str | None) -> str:
    on_domain = [e for e in emails if domain and e.endswith("@" + domain)]
    pool = on_domain or [e for e in emails if e.split("@")[-1] not in FREE_MAIL_DOMAINS] or emails
    for prefix in GENERIC_PREFIXES:
        for e in pool:
            if e.startswith(prefix + "@"):
                return e
    # never pass another staff member's personal inbox off as "the company address" (it is auto-sendable)
    shared = [e for e in pool if not _looks_personal(e)]
    return shared[0] if shared else ""


def learn_format(known: list[tuple[str, str]], domain: str | None) -> str | None:
    """From (person name, their address) pairs on the company domain, the local-part format most of them use."""
    if not domain:
        return None
    votes = Counter()
    for name, email in known:
        if not email.lower().endswith("@" + domain):
            continue
        local = email.split("@")[0].lower()
        parts = _name_parts(name)
        if len(parts) < 2:
            continue  # "Imtiaj" alone can't tell a first-name format from a last-name one
        f, l = parts[0], parts[-1]
        for fmt, build in FORMATS.items():
            if build(f, l) == local:
                votes[fmt] += 1
                break
    return votes.most_common(1)[0][0] if votes else None


# Official role mailboxes published on the website: the Chairman's own inbox beats info@ and any guess.
ROLE_MAILBOXES = [  # (title words, mailbox local parts) - most specific first
    ("pro vice chancellor", ("provc", "pvc", "pro-vc")), ("vice chancellor", ("vc", "vicechancellor")),
    ("vice chairman", ("vicechairman", "vice-chairman")), ("chairman", ("chairman", "chair")),
    ("chairperson", ("chairperson", "chair")), ("managing director", ("md", "managingdirector")),
    ("chief executive", ("ceo",)), ("ceo", ("ceo",)), ("principal", ("principal",)),
    ("registrar", ("registrar",)), ("founder", ("founder",)), ("president", ("president",)),
]


def role_mailbox(title: str, emails: list[str]) -> str:
    t = re.sub(r"[\s\-]+", " ", (title or "").lower())
    for words, locals_ in ROLE_MAILBOXES:
        if words in t:
            for e in emails:
                if e.split("@")[0].lower() in locals_:
                    return e
            return ""  # the most specific role decides: a Vice Chairman never gets chairman@
    return ""


_VALID_LOCAL = re.compile(r"^[a-z0-9!#$%&'*+/=?^_`{|}~-]+(\.[a-z0-9!#$%&'*+/=?^_`{|}~-]+)*$", re.I)


def valid_address(email: str) -> bool:
    """Rejects scraped fragments like 'doorstep.@site.com' or 'a..b@site.com'."""
    local, _, domain = (email or "").partition("@")
    return bool(local and domain and "." in domain and _VALID_LOCAL.match(local))


def apply_format(fmt: str, full_name: str, domain: str) -> str:
    parts = _name_parts(full_name)
    if not parts:
        return ""
    return f"{FORMATS[fmt](parts[0], parts[-1])}@{domain}"


def guess_patterns(full_name: str, domain: str) -> list[str]:
    parts = _name_parts(full_name)
    if not parts or not domain:
        return []
    first, last = parts[0], parts[-1]
    guesses = [f"{first}@{domain}"]
    if last != first:
        guesses += [f"{first}.{last}@{domain}", f"{first}{last}@{domain}", f"{first[0]}{last}@{domain}"]
    return guesses


def choose_email(full_name: str, site_emails: list[str], domain: str | None, mx_check=has_mx,
                 known: list[tuple[str, str]] | None = None, mail_ok=None, title: str = "") -> tuple[str, str]:
    """Returns (email, status) with status in found | pattern | company | guessed | unknown.
    known: (name, email) pairs of other people at this company, to learn its address format.
    mail_ok: domain -> 'yes' | 'no' | 'unknown'; addresses on a 'no' domain are dropped (None = no check).
    title: the person's job title, to use a published role mailbox (chairman@, vc@, md@ ...)."""
    def usable(email: str) -> bool:
        return valid_address(email) and (mail_ok is None or mail_ok(email.split("@")[-1].lower()) != "no")

    site_emails = [e for e in site_emails if usable(e)]
    if not full_name:
        e = generic_company_email(site_emails, domain)
        return (e, "company") if e else ("", "unknown")
    e = personal_match(full_name, site_emails) or role_mailbox(title, site_emails)
    if e:
        return e, "found"
    fmt = learn_format(known or [], domain)
    if fmt and domain and usable(f"x@{domain}"):
        e = apply_format(fmt, full_name, domain)
        if e:
            return e, "pattern"
    e = generic_company_email(site_emails, domain)
    if e:
        return e, "company"
    if domain and mx_check(domain):
        g = guess_patterns(full_name, domain)
        if g:
            return g[0], "guessed"
    return "", "unknown"
