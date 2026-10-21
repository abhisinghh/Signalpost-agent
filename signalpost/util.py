"""Small shared helpers: org numbers, names, dates, hashing."""
from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from datetime import date, datetime, timezone

# --------------------------------------------------------------------------- time


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def today() -> date:
    return datetime.now(timezone.utc).date()


# --------------------------------------------------------------------------- hashing


def sha256_hex(data: bytes | str) -> str:
    if isinstance(data, str):
        data = data.encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def short_hash(text: str, n: int = 16) -> str:
    return sha256_hex(text)[:n]


def canonical_json(value) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str)


def value_hash(value) -> str:
    return sha256_hex(canonical_json(value))[:24]


def trunc(text, n: int = 240):
    if text is None:
        return None
    text = re.sub(r"\s+", " ", str(text)).strip()
    return text if len(text) <= n else text[: n - 1].rstrip() + "…"


# --------------------------------------------------------------------------- org numbers

_WEIGHTS = (3, 2, 7, 6, 5, 4, 3, 2)


def digits(s) -> str:
    return re.sub(r"\D", "", str(s or ""))


def valid_orgnr(s) -> bool:
    d = digits(s)
    if len(d) != 9 or d[0] not in "89":
        # Norwegian organisation numbers start with 8 or 9.
        return False
    total = sum(int(a) * w for a, w in zip(d[:8], _WEIGHTS))
    r = 11 - total % 11
    if r == 11:
        r = 0
    if r == 10:
        return False
    return r == int(d[8])


def clean_orgnr(s) -> str | None:
    d = digits(s)
    return d if len(d) == 9 else None


def spaced_orgnr(orgnr: str) -> str:
    return f"{orgnr[:3]} {orgnr[3:6]} {orgnr[6:]}"


ORGNR_RE = re.compile(r"(?<!\d)([89]\d{2})[  .\-]?(\d{3})[  .\-]?(\d{3})(?!\d)")
ORG_CONTEXT_RE = re.compile(
    r"(org\.?\s*-?\s*n(r|o|ummer)|organi[sz]ation|organisasjons|foretak|mva|vat|"
    r"business\s*reg|reg\.?\s*nr|company\s*(no|number|reg)|bedrift)",
    re.I,
)


def find_orgnrs(text: str) -> list[dict]:
    """Return checksum-valid organisation numbers found in text, with context flag."""
    out: list[dict] = []
    if not text:
        return out
    for m in ORGNR_RE.finditer(text):
        num = "".join(m.groups())
        if not valid_orgnr(num):
            continue
        before = text[max(0, m.start() - 45): m.start()]
        after = text[m.end(): m.end() + 6]
        ctx = bool(ORG_CONTEXT_RE.search(before)) or after.strip().upper().startswith("MVA")
        out.append({"orgnr": num, "start": m.start(), "end": m.end(), "context": ctx})
    return out


# --------------------------------------------------------------------------- names

LEGAL_FORM_TOKENS = {
    "as", "asa", "ans", "da", "enk", "sa", "ba", "nuf", "ks", "kf", "iks", "sf", "bbl", "hf",
    "stiftelse", "stiftelsen", "forening", "aksjeselskap", "allmennaksjeselskap", "ab", "ltd",
    "limited", "gmbh", "inc", "oy", "aps", "a/s", "as.", "sti", "fli", "bo", "esek", "pk",
}


def strip_legal_form(name: str) -> str:
    tokens = re.split(r"\s+", (name or "").strip())
    while tokens and tokens[-1].lower().strip(".,()") in LEGAL_FORM_TOKENS:
        tokens.pop()
    while tokens and tokens[0].lower().strip(".,()") in {"a/s"}:
        tokens.pop(0)
    return " ".join(tokens)


def fold(s: str | None) -> str:
    """Lowercase, transliterate Norwegian letters, strip accents and punctuation."""
    if not s:
        return ""
    s = s.lower().replace("æ", "ae").replace("ø", "o").replace("å", "a").replace("&", " og ")
    s = unicodedata.normalize("NFKD", s)
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


GENERIC_NAME_WORDS = {
    "holding", "holdings", "invest", "investering", "eiendom", "eiendommer", "consulting",
    "konsult", "service", "services", "group", "gruppen", "norge", "norway", "nordic", "capital",
    "drift", "utvikling", "management", "partner", "partners", "solutions", "bygg", "transport",
}


def slug_variants(base_name: str) -> list[str]:
    """Domain-label guesses for a company name, most likely first."""
    if not base_name:
        return []
    s = base_name.lower()
    maps = [
        {"æ": "ae", "ø": "o", "å": "a"},
        {"æ": "ae", "ø": "oe", "å": "aa"},
        {"æ": "a", "ø": "o", "å": "a"},
    ]
    out: list[str] = []
    for amp in ("og", "and", ""):
        for mp in maps:
            t = s.replace("&", f" {amp} " if amp else " ")
            for k, v in mp.items():
                t = t.replace(k, v)
            t = unicodedata.normalize("NFKD", t)
            t = "".join(ch for ch in t if not unicodedata.combining(ch))
            words = re.findall(r"[a-z0-9]+", t)
            if not words:
                continue
            for cand in ("".join(words), "-".join(words)):
                if 3 <= len(cand) <= 40 and cand not in out:
                    out.append(cand)
        if "&" not in s:
            break
    return out


def is_generic_name(base_name: str) -> bool:
    words = fold(base_name).split()
    if not words:
        return True
    if len(words) == 1 and (words[0] in GENERIC_NAME_WORDS or len(words[0]) < 4):
        return True
    return all(w in GENERIC_NAME_WORDS for w in words)


# --------------------------------------------------------------------------- dates

NO_MONTHS = {
    "januar": 1, "jan": 1, "februar": 2, "feb": 2, "mars": 3, "mar": 3, "april": 4, "apr": 4,
    "mai": 5, "may": 5, "juni": 6, "jun": 6, "june": 6, "juli": 7, "jul": 7, "july": 7,
    "august": 8, "aug": 8, "september": 9, "sep": 9, "sept": 9, "oktober": 10, "okt": 10,
    "october": 10, "oct": 10, "november": 11, "nov": 11, "desember": 12, "des": 12,
    "december": 12, "dec": 12, "january": 1, "february": 2, "march": 3,
}

_ISO_RE = re.compile(r"\b(20\d{2}|19\d{2})-(\d{1,2})-(\d{1,2})")
_DMY_RE = re.compile(r"\b(\d{1,2})[./](\d{1,2})[./](20\d{2}|19\d{2})\b")
_DMONY_RE = re.compile(r"\b(\d{1,2})\.?\s+([A-Za-zæøåÆØÅ]{3,9})\.?\s+(20\d{2}|19\d{2})\b")
_MONDY_RE = re.compile(r"\b([A-Za-z]{3,9})\.?\s+(\d{1,2}),?\s+(20\d{2})\b")
_URL_DATE_RE = re.compile(r"/(20\d{2})/(\d{1,2})/(\d{1,2})/")


def _mk(y, m, d) -> date | None:
    try:
        dt = date(int(y), int(m), int(d))
    except (ValueError, TypeError):
        return None
    if dt.year < 2000 or dt > today():
        return None
    return dt


def parse_date(text: str | None) -> date | None:
    """Find the first plausible calendar date in a short text."""
    if not text:
        return None
    text = str(text)
    m = _ISO_RE.search(text)
    if m:
        d = _mk(*m.groups())
        if d:
            return d
    m = _DMY_RE.search(text)
    if m:
        d = _mk(m.group(3), m.group(2), m.group(1))
        if d:
            return d
    m = _DMONY_RE.search(text)
    if m:
        mon = NO_MONTHS.get(m.group(2).lower())
        if mon:
            d = _mk(m.group(3), mon, m.group(1))
            if d:
                return d
    m = _MONDY_RE.search(text)
    if m:
        mon = NO_MONTHS.get(m.group(1).lower())
        if mon:
            d = _mk(m.group(3), mon, m.group(2))
            if d:
                return d
    return None


def date_from_url(url: str) -> date | None:
    m = _URL_DATE_RE.search(url or "")
    return _mk(*m.groups()) if m else None


def parse_any_datetime(text: str | None) -> date | None:
    """RFC-822 (RSS) or ISO dates."""
    if not text:
        return None
    from email.utils import parsedate_to_datetime

    try:
        dt = parsedate_to_datetime(text.strip())
        if dt:
            return _mk(dt.year, dt.month, dt.day)
    except (TypeError, ValueError, IndexError):
        pass
    return parse_date(text)
