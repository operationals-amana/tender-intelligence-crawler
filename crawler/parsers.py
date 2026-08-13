"""Normalization and free-text extraction for World Bank procurement notices.

The API returns clean JSON for the structured fields, but the substance of a
notice lives in ``notice_text`` as an HTML blob. Everything the scoring engine
needs beyond the structured columns -- sector, budget, duration, qualification
requirements -- is mined out of that text here.

Note that the API response has **no** sector field, so ``sector`` is derived
from a keyword taxonomy rather than read off the payload.
"""
import hashlib
import json
import re
from datetime import datetime, timezone

from lxml import html as lxml_html

# Sector taxonomy. Keys are the canonical sector labels we store; values are the
# keywords that, when present in the notice text, imply that sector. Ordered
# roughly by how specific the terms are so that a notice can carry several.
SECTOR_KEYWORDS = {
    "Health": [
        "health", "hospital", "medical", "epidemiolog", "vaccine", "immuni",
        "nutrition", "maternal", "disease", "clinical", "pharmac", "nursing",
        "public health", "primary care", "hiv", "malaria", "tuberculosis",
    ],
    "Education": [
        "education", "school", "teacher", "curriculum", "student", "learning",
        "university", "vocational", "training institute", "literacy", "tvet",
    ],
    "Digital & ICT": [
        "digital", "software", "information system", "ict", "information technology",
        "e-government", "data center", "cyber", "artificial intelligence", "platform",
        "database", "mis ", "management information system", "interoperab", "gis",
    ],
    "Governance & Public Sector": [
        "governance", "public sector", "institutional", "civil service", "policy",
        "regulatory", "public financial management", "anti-corruption", "procurement reform",
        "decentraliz", "public administration", "legal framework",
    ],
    "Energy": [
        "energy", "electricity", "power plant", "solar", "renewable", "grid",
        "transmission line", "hydropower", "petroleum", "oil and gas", "geothermal",
    ],
    "Transport": [
        "transport", "road", "highway", "railway", "port ", "airport", "bridge",
        "logistics", "traffic", "urban mobility",
    ],
    "Water & Sanitation": [
        "water supply", "sanitation", "wastewater", "irrigation", "drainage",
        "water resource", "hygiene", "sewer",
    ],
    "Agriculture & Rural": [
        "agricultur", "farmer", "crop", "livestock", "fisher", "rural development",
        "food security", "agribusiness", "forestry",
    ],
    "Environment & Climate": [
        "climate", "environment", "emission", "biodiversity", "conservation",
        "resilience", "disaster risk", "green growth", "carbon", "pollution",
    ],
    "Finance & Private Sector": [
        "financial sector", "banking", "microfinance", "insurance", "capital market",
        "sme ", "private sector development", "investment climate", "fintech",
    ],
    "Social Protection": [
        "social protection", "social safety", "cash transfer", "pension",
        "labor market", "employment program", "poverty", "gender", "social inclusion",
    ],
    "Urban & Infrastructure": [
        "urban development", "municipal", "housing", "construction", "civil works",
        "infrastructure", "spatial planning", "land administration",
    ],
}

# Procurement group codes used by the World Bank.
PROCUREMENT_GROUPS = {
    "CS": "Consulting Services",
    "GO": "Goods",
    "CW": "Civil Works",
    "NC": "Non-Consulting Services",
}

_WS_RE = re.compile(r"\s+")


def hash_response(data: dict) -> str:
    """Stable SHA256 of a raw notice payload, used for change detection."""
    raw = json.dumps(data, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode()).hexdigest()


def strip_html(raw: str) -> str:
    """Turn the HTML notice body into readable plain text.

    Falls back to a regex strip if the markup is too broken for lxml.
    """
    if not raw:
        return ""
    try:
        fragment = lxml_html.fromstring(raw)
        text = fragment.text_content()
    except Exception:
        text = re.sub(r"<[^>]+>", " ", raw)
    # Collapse the entity noise and whitespace the notices are full of.
    text = text.replace("\xa0", " ").replace("&nbsp;", " ")
    return _WS_RE.sub(" ", text).strip()


def detect_sectors(text: str) -> list:
    """Return the sector labels whose keywords appear in the text."""
    if not text:
        return []
    lowered = text.lower()
    found = []
    for sector, keywords in SECTOR_KEYWORDS.items():
        if any(kw in lowered for kw in keywords):
            found.append(sector)
    return found


def _extract_budget(text: str) -> list:
    """Find currency amounts mentioned in the notice."""
    patterns = [
        # "USD 1,500,000" / "US$ 2.5 million" / "EUR 750,000"
        r"(?:US\$|USD|EUR|GBP|IDR|Rp\.?)\s*[\d,]+(?:\.\d+)?\s*(?:million|billion|m\b|bn\b)?",
        # "1,500,000 USD"
        r"[\d,]+(?:\.\d+)?\s*(?:million|billion)?\s*(?:US\$|USD|EUR|GBP)",
    ]
    hits = []
    for pattern in patterns:
        for match in re.findall(pattern, text, re.IGNORECASE):
            cleaned = _WS_RE.sub(" ", match.strip())
            if cleaned not in hits:
                hits.append(cleaned)
    return hits[:5]


def _extract_durations(text: str) -> list:
    """Find stated assignment durations, normalized to '<n> <unit>'."""
    hits = []
    for number, unit in re.findall(
        r"(\d{1,3})\s*[-\s]?\s*(month|months|year|years|week|weeks|day|days)\b",
        text,
        re.IGNORECASE,
    ):
        value = int(number)
        unit = unit.lower().rstrip("s")
        # Filter obvious noise (dates, counts) by keeping plausible durations.
        if unit == "month" and not 1 <= value <= 120:
            continue
        if unit == "year" and not 1 <= value <= 20:
            continue
        if unit == "week" and not 1 <= value <= 200:
            continue
        if unit == "day" and not 1 <= value <= 400:
            continue
        label = f"{value} {unit}{'s' if value != 1 else ''}"
        if label not in hits:
            hits.append(label)
    return hits[:5]


def _extract_qualifications(text: str) -> dict:
    """Pull out experience / education / certification requirements."""
    quals = {}

    years = [
        int(n)
        for n in re.findall(
            r"(?:at least|minimum(?:\s+of)?|not less than)\s+(\d{1,2})\s*(?:\+)?\s*years?",
            text,
            re.IGNORECASE,
        )
        if int(n) <= 40
    ]
    if years:
        quals["min_years_experience"] = max(years)

    degrees = set()
    for match in re.findall(
        r"\b(master'?s?|bachelor'?s?|phd|doctorate|post[- ]?graduate|msc|bsc|mba)\b",
        text,
        re.IGNORECASE,
    ):
        degrees.add(match.lower().replace("'", "").rstrip("s"))
    if degrees:
        quals["degrees"] = sorted(degrees)

    if re.search(r"\b(certif|accredit|licens|chartered)\w*", text, re.IGNORECASE):
        quals["certification_required"] = True

    return quals


def parse_notice_text(notice_text: str, extra_context: str = "") -> dict:
    """Extract structured facts from a notice body.

    ``extra_context`` lets the caller fold in the bid description and project
    name so sector detection still works when the body is empty.
    """
    clean = strip_html(notice_text)
    haystack = f"{extra_context} {clean}".strip()

    if not haystack:
        return {}

    parsed = {}

    sectors = detect_sectors(haystack)
    if sectors:
        parsed["detected_sectors"] = sectors

    budget = _extract_budget(haystack)
    if budget:
        parsed["budget_mentions"] = budget

    durations = _extract_durations(haystack)
    if durations:
        parsed["duration_mentions"] = durations

    quals = _extract_qualifications(haystack)
    if quals:
        parsed["qualifications"] = quals

    parsed["text_length"] = len(clean)
    return parsed


def _parse_deadline(value):
    """Parse the ISO-8601 deadline the API returns, as an aware UTC datetime."""
    if not value:
        return None
    text = str(value).strip()
    try:
        if "T" in text:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        else:
            parsed = datetime.strptime(text[:10], "%Y-%m-%d")
    except (ValueError, TypeError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def parse_noticedate(value):
    """Parse the ``noticedate`` field, which uses the '11-Aug-2026' format."""
    if not value:
        return None
    text = str(value).strip()
    for fmt in ("%d-%b-%Y", "%Y-%m-%d", "%d/%m/%Y"):
        try:
            return datetime.strptime(text[:11], fmt).date()
        except ValueError:
            continue
    return None


def _combine_deadline(date_value, time_value):
    """Fold ``submission_deadline_time`` ('17:00') into the deadline date."""
    deadline = _parse_deadline(date_value)
    if not deadline or not time_value:
        return deadline
    match = re.match(r"^(\d{1,2}):(\d{2})", str(time_value).strip())
    if not match:
        return deadline
    hour, minute = int(match.group(1)), int(match.group(2))
    if hour > 23 or minute > 59:
        return deadline
    return deadline.replace(hour=hour, minute=minute)


def _text(value, max_length=None):
    """Normalize a value to a trimmed string, optionally capped.

    The API occasionally returns values longer than our column widths (contact
    names concatenated with titles, for example), so callers pass the column
    width to avoid a StringDataRightTruncation that would fail the whole batch.
    """
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if max_length and len(text) > max_length:
        text = text[:max_length].rstrip()
    return text


def normalize_notice_item(raw_item: dict) -> dict:
    """Map a raw API notice onto our column names.

    ``sector`` is absent from the API payload, so it is derived from the notice
    text and stored as a comma-separated list of canonical sector labels.
    """
    notice_text = raw_item.get("notice_text") or ""
    clean_text = strip_html(notice_text)

    context = " ".join(
        filter(
            None,
            [
                _text(raw_item.get("bid_description")),
                _text(raw_item.get("project_name")),
                _text(raw_item.get("notice_type")),
            ],
        )
    )
    parsed = parse_notice_text(notice_text, extra_context=context)
    sectors = parsed.get("detected_sectors", [])

    group_code = _text(raw_item.get("procurement_group"), 10)
    method_name = _text(raw_item.get("procurement_method_name"), 100)

    notice_id = _text(raw_item.get("id"), 50)

    return {
        "notice_id": notice_id,
        "notice_type": _text(raw_item.get("notice_type"), 100),
        "noticedate": parse_noticedate(raw_item.get("noticedate")),
        "notice_status": _text(raw_item.get("notice_status"), 50) or "Published",
        "submission_deadline": _combine_deadline(
            raw_item.get("submission_deadline_date"),
            raw_item.get("submission_deadline_time"),
        ),
        "project_id": _text(raw_item.get("project_id"), 50),
        "project_name": _text(raw_item.get("project_name")),
        "project_country": _text(raw_item.get("project_ctry_name"), 100),
        "bid_reference_no": _text(raw_item.get("bid_reference_no"), 100),
        "bid_description": _text(raw_item.get("bid_description")),
        "procurement_group": group_code,
        "procurement_method_code": _text(raw_item.get("procurement_method_code"), 20),
        "procurement_method_name": method_name or PROCUREMENT_GROUPS.get(group_code or ""),
        "sector": ", ".join(sectors) if sectors else None,
        "contact_organization": _text(raw_item.get("contact_organization")),
        "contact_name": _text(raw_item.get("contact_name"), 200),
        "contact_email": _text(raw_item.get("contact_email"), 200),
        "contact_phone": _text(raw_item.get("contact_phone_no"), 100),
        "contact_address": _text(raw_item.get("contact_address")),
        "notice_text": notice_text or None,
        "notice_text_clean": clean_text or None,
        "parsed_fields": parsed,
        # The API has no per-notice permalink field; this is the public UI route.
        "notice_url": (
            f"https://projects.worldbank.org/en/projects-operations/procurement-detail/{notice_id}"
            if notice_id
            else None
        ),
    }
