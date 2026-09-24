"""Pick the best outreach address for a decision maker.

Order: the person's own address seen on the website > a generic company address from the
website > a pattern guess on the company domain (only if the domain has MX records).
Guesses are never auto-sent (see pipeline), because bounces hurt the sender account.
"""
import dns.exception
import dns.resolver

from app.services.normalize import FREE_MAIL_DOMAINS, person_key

GENERIC_PREFIXES = ("info", "contact", "hello", "office", "admin", "support", "sales", "enquiry", "inquiry", "mail", "hr")


def has_mx(domain: str) -> bool:
    try:
        return bool(dns.resolver.resolve(domain, "MX", lifetime=5))
    except (dns.exception.DNSException, OSError):
        return False


def _name_parts(full_name: str) -> list[str]:
    return [p for p in person_key(full_name).split() if len(p) >= 3]


def personal_match(full_name: str, emails: list[str]) -> str:
    parts = _name_parts(full_name)
    for e in emails:
        local = e.split("@")[0].lower()
        if any(p in local for p in parts):
            return e
    return ""


def generic_company_email(emails: list[str], domain: str | None) -> str:
    on_domain = [e for e in emails if domain and e.endswith("@" + domain)]
    pool = on_domain or [e for e in emails if e.split("@")[-1] not in FREE_MAIL_DOMAINS] or emails
    for prefix in GENERIC_PREFIXES:
        for e in pool:
            if e.startswith(prefix + "@"):
                return e
    return pool[0] if pool else ""


def guess_patterns(full_name: str, domain: str) -> list[str]:
    parts = _name_parts(full_name)
    if not parts or not domain:
        return []
    first, last = parts[0], parts[-1]
    guesses = [f"{first}@{domain}"]
    if last != first:
        guesses += [f"{first}.{last}@{domain}", f"{first}{last}@{domain}", f"{first[0]}{last}@{domain}"]
    return guesses


def choose_email(full_name: str, site_emails: list[str], domain: str | None, mx_check=has_mx) -> tuple[str, str]:
    """Returns (email, status) with status in found | company | guessed | unknown."""
    if not full_name:
        e = generic_company_email(site_emails, domain)
        return (e, "company") if e else ("", "unknown")
    e = personal_match(full_name, site_emails)
    if e:
        return e, "found"
    e = generic_company_email(site_emails, domain)
    if e:
        return e, "company"
    if domain and mx_check(domain):
        g = guess_patterns(full_name, domain)
        if g:
            return g[0], "guessed"
    return "", "unknown"
