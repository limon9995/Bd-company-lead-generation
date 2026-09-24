import re
import unicodedata
from urllib.parse import urlparse

# Hosts that are not a company's own domain. In Bangladesh many businesses list a
# Facebook page as their "website", so these must not be used as a dedupe/email domain.
NON_COMPANY_HOSTS = {
    "facebook.com", "fb.com", "fb.me", "m.facebook.com", "instagram.com", "linkedin.com", "twitter.com", "x.com",
    "youtube.com", "youtu.be", "wa.me", "whatsapp.com", "t.me", "google.com", "goo.gl", "g.page", "business.site",
    "sites.google.com", "linktr.ee", "tiktok.com", "blogspot.com", "wordpress.com", "wixsite.com", "bit.ly",
}
FREE_MAIL_DOMAINS = {"gmail.com", "yahoo.com", "hotmail.com", "outlook.com", "live.com", "ymail.com", "icloud.com", "yahoo.co.uk"}
SOCIAL_PATTERNS = {
    "facebook": re.compile(r"(?:^|\.)(facebook\.com|fb\.com|fb\.me)$"),
    "linkedin": re.compile(r"(?:^|\.)linkedin\.com$"),
    "instagram": re.compile(r"(?:^|\.)instagram\.com$"),
    "youtube": re.compile(r"(?:^|\.)(youtube\.com|youtu\.be)$"),
    "twitter": re.compile(r"(?:^|\.)(twitter\.com|x\.com)$"),
}

_COMPANY_SUFFIXES = re.compile(
    r"\b(limited|ltd|pvt|private|plc|inc|llc|co|company|corporation|corp|bd|bangladesh)\b\.?", re.I
)


def host_of(url: str) -> str:
    if not url:
        return ""
    if "://" not in url:
        url = "http://" + url
    host = (urlparse(url).hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


def _is_non_company(host: str) -> bool:
    return any(host == h or host.endswith("." + h) for h in NON_COMPANY_HOSTS)


def normalize_domain(url: str) -> str | None:
    """Registrable-ish domain of a company website, or None for social/free hosting URLs."""
    host = host_of(url)
    if not host or "." not in host or _is_non_company(host):
        return None
    parts = host.split(".")
    # keep 3 labels for second-level ccTLDs like example.com.bd / example.org.bd / example.ac.bd
    if len(parts) >= 3 and parts[-1] in {"bd", "uk", "in", "au", "sg", "my"} and len(parts[-2]) <= 3:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


def social_kind(url: str) -> str | None:
    host = host_of(url)
    for kind, pat in SOCIAL_PATTERNS.items():
        if pat.search(host):
            return kind
    return None


def name_key(name: str) -> str:
    s = unicodedata.normalize("NFKC", name or "").lower()
    s = _COMPANY_SUFFIXES.sub(" ", s)
    s = re.sub(r"[^\w\s]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def person_key(name: str) -> str:
    s = unicodedata.normalize("NFKC", name or "").lower()
    s = re.sub(r"\b(mr|mrs|ms|dr|prof|engr|md|mohammad|mohammed|muhammad)\b\.?", " ", s)
    s = re.sub(r"[^\w\s]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


_BD_MOBILE = re.compile(r"(?<!\d)(?:\+?88)?[\s-]?(01[3-9](?:[\s-]?\d){8})(?!\d)")
_BD_LANDLINE = re.compile(r"(?<!\d)(?:\+?88)?[\s-]?(0[2-9](?:[\s-]?\d){6,9})(?!\d)")


def normalize_bd_phone(raw: str) -> str:
    digits = re.sub(r"\D", "", raw or "")
    if digits.startswith("880"):
        digits = digits[3:]
    elif digits.startswith("88") and len(digits) >= 12:
        digits = digits[2:]
    if not digits.startswith("0"):
        digits = "0" + digits
    return "+880" + digits[1:] if 9 <= len(digits) <= 11 else ""


def find_bd_phones(text: str) -> list[str]:
    found: list[str] = []
    for pat in (_BD_MOBILE, _BD_LANDLINE):
        for m in pat.finditer(text or ""):
            p = normalize_bd_phone(m.group(1))
            if p and p not in found:
                found.append(p)
    return found


_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_OBFUSCATED_AT = re.compile(r"\s*(?:\[at\]|\(at\)|\{at\}|\s+at\s+)\s*", re.I)
_OBFUSCATED_DOT = re.compile(r"\s*(?:\[dot\]|\(dot\)|\{dot\})\s*", re.I)
_BAD_EMAIL_SUFFIXES = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".css", ".js")


def find_emails(text: str) -> list[str]:
    t = _OBFUSCATED_DOT.sub(".", _OBFUSCATED_AT.sub("@", text or ""))
    out: list[str] = []
    for m in _EMAIL.finditer(t):
        e = m.group(0).strip(".").lower()
        if e.endswith(_BAD_EMAIL_SUFFIXES) or "example." in e or e.startswith(("u00", "sentry")):
            continue
        if e not in out:
            out.append(e)
    return out


def squash_ws(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()
