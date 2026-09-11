"""Normalization for GIZ country-office tender pages.

Where the notices come from
---------------------------
GIZ's country offices publish their local procurement on pages like
``giz.de/en/regions/asia/indonesia/tenders``. The list is a server-rendered
Drupal Views block, so the rows are in the initial HTML: no browser, no auth,
no challenge. What each row carries is thin — a title, an optional deadline,
and a ZIP of the tender documents — and that is *all* it carries: there is no
body text, no per-tender page and no publication date. The full survey of the
feed, and of GIZ's other channels, is in ``GIZ_SOURCE.md``.

Identity
--------
Most titles embed GIZ's own 8-digit processing number ("Procurement of Service
10045631-ZME-German Language..."), which is stable across re-posts and is what
GIZ itself quotes in the documents; that number is the notice id. A title
without one falls back to a hash of the title and country, which means a
retitled notice re-appears under a new id — an accepted trade for a list this
small.

Dates
-----
The page shows the deadline as a bare date. It is stored at 23:59 UTC so the
notice stays "open" through the whole of the local day rather than expiring at
midnight UTC the night before. There is no publication date at all; the upload
month embedded in the document ZIP's path (``/2026-08/``) is used as an
approximate one and flagged as such in ``parsed_fields``.
"""
import hashlib
import re
from datetime import datetime, timezone

from lxml import html as lxml_html

from crawler.parsers import _text, build_dedup_key, detect_sectors
from crawler.sources import GIZ

GIZ_SITE = "https://www.giz.de"

GIZ_ORGANIZATION = "Deutsche Gesellschaft für Internationale Zusammenarbeit (GIZ) GmbH"

#: "Procurement of Service 10045631-..." / "Procurement of Goods-10051989-...".
#: The separator varies between a space, a dash and nothing at all.
_TYPE_PREFIX_RE = re.compile(
    r"^\s*Procurement\s+of\s+(Services?|Goods?|Works?)\b[\s:\u2013-]*", re.I
)

#: GIZ processing numbers are 8 digits and currently all start with 10.
_NUMBER_RE = re.compile(r"\b(\d{8})\b")

#: "Deadline: 14.09.2026" as the row's meta line renders it.
_DEADLINE_RE = re.compile(r"(\d{1,2})\.(\d{1,2})\.(\d{4})")

#: The upload month in a document path: /sites/default/files/media/els-document/2026-08/...
_UPLOAD_MONTH_RE = re.compile(r"/(\d{4})-(\d{2})/")

#: GIZ's procurement method -> (notice_type, procurement_group) in the shared
#: vocabulary. GIZ's own names are RFP for services and RFQ for goods/works
#: (see the country page's own explainer); the shared terms below are the ones
#: the scoring engine's actionable-notice allowlist and the dashboard already
#: key on. The raw prefix is kept in ``parsed_fields.giz_procurement_type``.
_TYPE_MAP = {
    "service": ("Request for Proposals", "CS"),
    "good": ("Invitation for Bids", "GO"),
    "work": ("Invitation for Bids", "CW"),
}


def parse_giz_listing(body) -> list:
    """Extract the raw tender rows from a country tenders page.

    Returns a list of dicts with the page's own vocabulary: ``title``,
    ``deadline`` (the meta line's text), ``document_url``, ``document_name``
    and ``document_size``. Empty when the page has no rows — the caller
    distinguishes "no tenders right now" from "page changed shape" by looking
    for the view marker itself.
    """
    if isinstance(body, bytes):
        body = body.decode("utf-8", "replace")
    try:
        root = lxml_html.fromstring(body)
    except Exception:
        return []

    rows = []
    for row in root.xpath('//div[contains(@class, "views-row")]'):
        title = _first_text(row, './/div[contains(@class, "list-item__title")]')
        if not title:
            continue

        raw = {"title": title}

        meta = _first_text(row, './/div[contains(@class, "list-item__meta")]')
        if meta:
            raw["deadline"] = meta

        href = row.xpath('.//a[contains(@class, "action")]/@href')
        if href:
            url = href[0].strip()
            raw["document_url"] = url if url.startswith("http") else f"{GIZ_SITE}{url}"

        doc_name = row.xpath('.//div[contains(@class, "download__title")]/span/@title')
        if doc_name:
            raw["document_name"] = doc_name[0].strip()

        # The info cells hold the file type and the size; keep the one that
        # reads as a size and skip the icon-bearing type cell.
        for info in row.xpath('.//div[contains(@class, "download__info")]'):
            text = " ".join(info.text_content().split())
            if re.search(r"\d\s*(KB|MB|GB)", text, re.I):
                raw["document_size"] = text
                break

        rows.append(raw)
    return rows


def _first_text(node, xpath: str):
    found = node.xpath(xpath)
    if not found:
        return None
    return " ".join(found[0].text_content().split()) or None


def parse_giz_deadline(raw):
    """Parse the meta line's ``DD.MM.YYYY`` into an aware datetime.

    Stored at the end of the day in UTC: the page gives no time and no zone,
    and every zone on earth is still inside that date at 23:59 UTC or has only
    just left it — so the notice does not read as closed while the office that
    posted it would still accept a bid.
    """
    match = _DEADLINE_RE.search(str(raw or ""))
    if not match:
        return None
    day, month, year = (int(g) for g in match.groups())
    try:
        return datetime(year, month, day, 23, 59, tzinfo=timezone.utc)
    except ValueError:
        return None


def _upload_month(document_url):
    """The document path's upload month, as an approximate publication date."""
    match = _UPLOAD_MONTH_RE.search(str(document_url or ""))
    if not match:
        return None
    year, month = int(match.group(1)), int(match.group(2))
    try:
        return datetime(year, month, 1).date()
    except ValueError:
        return None


def giz_notice_id(title: str, country: str) -> str:
    """``GIZ-<processing number>``, or a title hash when there is none.

    The hash folds the country in because two offices can plausibly post the
    same generic title ("Office Furniture"), and those are two tenders.
    """
    match = _NUMBER_RE.search(title or "")
    if match:
        return f"GIZ-{match.group(1)}"
    digest = hashlib.sha1(f"{(title or '').lower()}|{country or ''}".encode()).hexdigest()
    return f"GIZ-{digest[:12]}"


def normalize_giz_row(raw: dict, country: str, page_url: str) -> dict:
    """Map one listing row onto our column names."""
    title = _text(raw.get("title")) or ""

    prefix_match = _TYPE_PREFIX_RE.match(title)
    notice_type, group = ("Other Notice", None)
    giz_type = None
    remainder = title
    if prefix_match:
        giz_type = prefix_match.group(0).strip(" :-\u2013")
        remainder = title[prefix_match.end():]
        kind = prefix_match.group(1).lower().rstrip("s")
        notice_type, group = _TYPE_MAP.get(kind, ("Other Notice", None))

    number_match = _NUMBER_RE.search(title)
    number = number_match.group(1) if number_match else None

    # The human name of the work: the title minus the type prefix and the
    # number's own separators ("10045631-ZME-Training" -> "ZME-Training").
    project_name = remainder
    if number:
        project_name = re.sub(rf"^\s*{number}\s*[-\u2013:]*\s*", "", remainder)
    project_name = _text(project_name) or _text(remainder) or title

    deadline = parse_giz_deadline(raw.get("deadline"))
    document_url = raw.get("document_url")
    noticedate = _upload_month(document_url)

    parsed = {
        "giz_procurement_type": giz_type,
        "country_page": page_url,
    }
    if document_url:
        parsed["document_url"] = document_url
    if raw.get("document_name"):
        parsed["document_name"] = raw["document_name"]
    if raw.get("document_size"):
        parsed["document_size"] = raw["document_size"]
    if noticedate:
        # The ZIP's upload month, not a date GIZ states; see the module docstring.
        parsed["noticedate_is_approximate"] = True

    sectors = detect_sectors(title)
    if sectors:
        parsed["detected_sectors"] = sectors

    return {
        "notice_id": giz_notice_id(title, country),
        "source": GIZ,
        "notice_type": _text(notice_type, 100),
        "noticedate": noticedate,
        # The page only lists open tenders; a closed one is simply removed.
        "notice_status": "Published",
        "submission_deadline": deadline,
        "project_id": number,
        "project_name": _text(project_name, 500),
        "project_country": _text(country, 100),
        "bid_reference_no": number,
        "bid_description": title or None,
        "procurement_group": group,
        "procurement_method_code": None,
        "procurement_method_name": _text(giz_type, 100),
        "sector": ", ".join(sectors) if sectors else None,
        "contact_organization": GIZ_ORGANIZATION,
        "contact_name": None,
        "contact_email": None,
        "contact_phone": None,
        "contact_address": None,
        "notice_text": None,
        # The title is the only prose the feed carries; storing it here keeps
        # the notice reachable by the keyword search that reads this column.
        "notice_text_clean": title or None,
        "parsed_fields": parsed,
        # There is no per-tender page; the country listing is where a person
        # finds this notice and its document.
        "notice_url": page_url,
        "dedup_key": build_dedup_key(title, country, deadline),
    }
