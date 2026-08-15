"""Normalization for Asian Development Bank business opportunities.

Two upstream shapes feed one row
--------------------------------
ADB's tender listing at ``adb.org/projects/tenders`` is rendered in the browser
from a hosted Solr index, and that index is what this module normalizes. It
gives clean metadata -- title, country, sector, type, posted and closing dates --
but no notice body.

The body lives in a second place, and only for consulting notices: each carries
an ``ss_csrn_url`` into ADB's Consultant Management System, whose Consulting
Services Recruitment Notice page holds the assignment's budget, engagement
period, selection method, contact and eligibility requirements. That page is a
server-rendered Oracle E-Business Suite form, so it is parsed by walking its
label/value rows rather than by reading fixed selectors.

Goods and works notices publish their body as a PDF on ``www.adb.org``, which
sits behind a challenge this service cannot pass. Those rows therefore carry
metadata only, and their ``notice_url`` points a human at the page instead.

Mapping onto the World Bank vocabulary
--------------------------------------
The ``tenders`` table was shaped by the World Bank feed, and scoring, the LLM
shortlist and the practice-group classifier all read that vocabulary. Rather
than teach each of them a second dialect, ADB's terms are translated here:
an ADB consulting notice for a firm *is* a request for expressions of interest,
so that is what it is stored as. The untranslated ADB terms are kept in
``parsed_fields`` so nothing is lost in the process.
"""
import re
from datetime import datetime, timezone

from lxml import html as lxml_html

from crawler.parsers import (
    _text,
    build_dedup_key,
    detect_sectors,
    parse_notice_text,
    strip_html,
)
from crawler.sources import ADB

#: Public base of ADB's site, for turning the index's relative paths into links.
ADB_SITE = "https://www.adb.org"

#: ADB notice type -> (notice_type, procurement_group) in our vocabulary.
#:
#: ``notice_type`` values are reused verbatim from the World Bank feed wherever
#: the two banks mean the same thing, because the scoring engine's actionable-
#: notice allowlist and the dashboard's abbreviations are both keyed on them.
#:
#: ``Individual`` is mapped to the group ``IC`` rather than ``CS`` on purpose.
#: These are single-expert assignments — a named person, not a firm — so AMANA
#: cannot bid them as a company, and grouping them under ``CS`` would put
#: 29,000 unbiddable notices into the LLM shortlist. They are still ingested:
#: they are a live signal of where ADB is spending, and the dashboard can filter
#: to them when somebody is placing an associate.
NOTICE_TYPE_MAP = {
    "Firm": ("Request for Expression of Interest", "CS"),
    "Individual": ("Request for Expression of Interest", "IC"),
    "Invitation for Bids": ("Invitation for Bids", "GO"),
    "Invitation for prequalification": ("Invitation for Prequalification", "GO"),
    "General Procurement Notice": ("General Procurement Notice", None),
    # ADB's equivalent of an early heads-up: work that is coming but not yet
    # tendered. Kept actionable for the same reason a GPN is — it is the notice
    # that lets a bid team position before the package opens.
    "Advance Notice": ("Advance Notice", None),
    "Other Notice": ("Other Notice", None),
    "Prequalified applicants": ("Prequalified Applicants", None),
    "Contracts Awarded": ("Contract Award", None),
}

#: ADB status -> our ``notice_status``. Only ``Published`` is scored, so this is
#: what keeps closed and awarded ADB notices out of the shortlist.
STATUS_MAP = {
    "Active": "Published",
    "Closed": "Closed",
    "Awarded": "Awarded",
    "Archived": "Archived",
}

#: ADB's 11 sectors, expressed in the taxonomy ``crawler/parsers.py`` already
#: uses, so one sector filter serves both feeds. ADB's "Water and other urban
#: infrastructure and services" genuinely spans two of our labels and is mapped
#: to both. "Multisector" says nothing and is deliberately dropped.
SECTOR_MAP = {
    "Water and other urban infrastructure and services": [
        "Water & Sanitation",
        "Urban & Infrastructure",
    ],
    "Transport": ["Transport"],
    "Agriculture, natural resources and rural development": ["Agriculture & Rural"],
    "Public sector management": ["Governance & Public Sector"],
    "Energy": ["Energy"],
    "Education": ["Education"],
    "Health": ["Health"],
    "Finance": ["Finance & Private Sector"],
    "Industry and trade": ["Finance & Private Sector"],
    "Information and communication technology": ["Digital & ICT"],
    "Multisector": [],
}

#: Selection methods carry their own abbreviation in brackets, which is the
#: closest thing ADB has to the World Bank's procurement method code.
_METHOD_CODE_RE = re.compile(r"\(([A-Z][A-Z0-9-]{1,15})\)\s*$")

#: "USD2,088,000" as rendered by the CMS: currency and amount run together.
_BUDGET_RE = re.compile(r"^([A-Z]{3})\s*([\d,]+(?:\.\d+)?)$")

#: "54MONTH" / "15 Working Days" — a number and the unit it is counted in.
_PERIOD_RE = re.compile(r"^(\d+(?:\.\d+)?)\s*([A-Za-z ]+)$")

_NODE_RE = re.compile(r"/node/(\d+)")


def _first(value):
    """Solr returns multi-valued fields as lists; take the first, if any."""
    if isinstance(value, list):
        return value[0] if value else None
    return value


def _all(value):
    """Every value of a Solr field, as a list."""
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def notice_id_from_doc(doc: dict) -> str:
    """Our id for an ADB notice: ``ADB-<drupal node id>``.

    The node id is the only stable, human-checkable identifier ADB exposes — it
    is what its own permalinks are built from. The Solr document id embeds the
    same number inside an index-specific string (``...entity:node/1168041:en``)
    that would change if ADB ever reindexed under a new name, so the number is
    pulled out rather than used as given.
    """
    node = None
    match = _NODE_RE.search(str(doc.get("ss_url") or ""))
    if match:
        node = match.group(1)
    else:
        match = _NODE_RE.search(str(doc.get("id") or ""))
        if match:
            node = match.group(1)
    return f"ADB-{node}" if node else ""


def parse_solr_date(value):
    """Parse Solr's ``2026-08-14T12:00:00Z`` into an aware UTC datetime."""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def map_sectors(adb_sectors) -> list:
    """Translate ADB's sector labels into ours, preserving order and uniqueness.

    Unrecognized labels are passed through rather than dropped: a new ADB sector
    should show up in the filter looking foreign, not vanish silently.
    """
    mapped = []
    for label in _all(adb_sectors):
        for name in SECTOR_MAP.get(label, [label]):
            if name and name not in mapped:
                mapped.append(name)
    return mapped


# --------------------------------------------------------------------------
# The Consulting Services Recruitment Notice page
# --------------------------------------------------------------------------


def _row_label_and_value(row):
    """Read one Oracle form row as a (label, value) pair.

    The form nests rows inside rows, so a naive read of a row's cells swallows
    every field below it — "Package Number" comes back carrying the whole of
    "Package Name" as well. Nested rows are therefore detached before the value
    is read, which leaves each field holding only its own text.
    """
    cells = row.xpath("./td")
    if len(cells) < 2:
        return None, None

    label = " ".join(cells[0].text_content().split())
    if not label or len(label) > 80:
        return None, None

    parts = []
    for cell in cells[1:]:
        for nested in cell.xpath(".//tr[@id]"):
            parent = nested.getparent()
            if parent is not None:
                parent.remove(nested)
        text = " ".join(cell.text_content().split())
        if text:
            parts.append(text)

    return label, " ".join(parts).strip()


def parse_csrn_page(body) -> dict:
    """Extract the fields and the readable text of a CSRN page.

    Returns ``{"fields": {...}, "text": "..."}``. Both are best-effort: the CMS
    renders a different set of rows for individual and firm assignments, and a
    missing field means the notice did not carry it.

    An empty ``text`` is how the caller learns the page could not be read, and
    it marks the resulting item partial so the update keeps whatever body is
    already stored. So the regex fallback below matters: without it a page lxml
    chokes on would come back blank and look like a fetch failure.
    """
    if not body:
        return {"fields": {}, "text": ""}

    try:
        root = lxml_html.fromstring(body)
    except Exception:
        # Scrapy hands over bytes, so decode before falling back to the regex
        # strip -- `strip_html` works on text, and passing it bytes would return
        # nothing and lose a page we could still have read something from.
        if isinstance(body, bytes):
            body = body.decode("utf-8", "replace")
        return {"fields": {}, "text": strip_html(body)}

    # Read the fields from a throwaway copy: extraction detaches nested rows,
    # and the readable text below needs the document still intact.
    fields = {}
    try:
        scratch = lxml_html.fromstring(body)
        for row in scratch.xpath("//tr[@id]"):
            label, value = _row_label_and_value(row)
            # `setdefault`, so the outermost row wins where the form repeats a
            # label at two nesting depths.
            if label and value:
                fields.setdefault(label, value)
    except Exception:
        fields = {}

    for junk in root.xpath("//script | //style | //noscript"):
        parent = junk.getparent()
        if parent is not None:
            parent.remove(junk)

    # Read the application's content region rather than the whole document. The
    # page wraps the notice in the CMS's own chrome -- a skip link, the product
    # name, and a six-item navigation bar -- and that boilerplate is identical on
    # every notice, so keeping it would add nothing to search and would feed the
    # scoring engine the same words 38,000 times.
    content = root.xpath('//*[@id="oafusercontent"]') or root.xpath('//*[@id="oafcontent"]')
    region = content[0] if content else root
    text = re.sub(r"\s+", " ", region.text_content().replace("\xa0", " ")).strip()

    return {"fields": fields, "text": text}


#: Fields worth promoting out of the raw CSRN dump into ``parsed_fields``, and
#: the key each is stored under. Everything else stays in the readable text.
_CSRN_KEEP = {
    "Consultant Type": "consultant_type",
    "Selection Method": "selection_method",
    "Source": "selection_source",
    "Technical Proposal": "technical_proposal",
    "Selection Title": "selection_title",
    "Package Number": "package_number",
    "Package Name": "package_name",
    "Engagement Period": "engagement_period",
    "Total Inputs": "total_inputs",
    "Consulting Services Budget": "budget",
    "Budget Type": "budget_type",
    "Approval Number": "approval_number",
    "Estimated Commencement Date": "commencement_date",
    "Country of assignment": "country_of_assignment",
    "Indefinite Delivery Contract (IDC)": "indefinite_delivery_contract",
    "Other information": "other_information",
    "Project Officer": "project_officer",
    "Designation": "project_officer_designation",
    "Email": "project_officer_email",
}

#: Rendered, in this order, as the notice body on the detail page. Ordered by
#: what a bid manager reads first, not by where the CMS happens to put it.
_CSRN_RENDER_ORDER = [
    "Package Name",
    "Selection Title",
    "Package Number",
    "Consultant Type",
    "Selection Method",
    "Technical Proposal",
    "Source",
    "Engagement Period",
    "Total Inputs",
    "Consulting Services Budget",
    "Budget Type",
    "Estimated Commencement Date",
    "Country of assignment",
    "Project Officer",
    "Designation",
    "Email",
    "Other information",
]


def _escape(value: str) -> str:
    return (
        str(value)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def render_csrn_html(fields: dict) -> str:
    """Build the notice body the detail page renders.

    The detail page injects ``notice_text`` as HTML, and the CMS page it came
    from is a full Oracle application — forms, scripts, navigation — so storing
    it verbatim would be both unreadable and unsafe to inject. This composes a
    small document from the extracted fields instead, escaping every value, so
    what the page renders is markup this module wrote.
    """
    if not fields:
        return ""

    rendered = []
    for label in _CSRN_RENDER_ORDER:
        value = fields.get(label)
        if not value:
            continue
        # Long prose reads as a paragraph; short facts read as a labelled line.
        if len(value) > 160:
            rendered.append(f"<p><strong>{_escape(label)}</strong></p><p>{_escape(value)}</p>")
        else:
            rendered.append(f"<p><strong>{_escape(label)}:</strong> {_escape(value)}</p>")

    return "".join(rendered)


def _parse_budget(raw: str) -> dict:
    """Split "USD2,088,000" into a currency and a number."""
    match = _BUDGET_RE.match((raw or "").strip())
    if not match:
        return {}
    try:
        amount = float(match.group(2).replace(",", ""))
    except ValueError:
        return {}
    return {"budget_currency": match.group(1), "budget_amount": amount}


def _parse_period(raw: str) -> dict:
    """Split "54MONTH" or "15 Working Days" into a number and a unit."""
    match = _PERIOD_RE.match((raw or "").strip())
    if not match:
        return {}
    try:
        value = float(match.group(1))
    except ValueError:
        return {}
    unit = " ".join(match.group(2).split()).lower()
    return {"engagement_value": value, "engagement_unit": unit}


# --------------------------------------------------------------------------
# Solr document -> tender row
# --------------------------------------------------------------------------


def normalize_adb_doc(doc: dict, detail: dict = None) -> dict:
    """Map one ADB Solr document, plus its CSRN detail if we fetched one, onto
    our column names.

    ``detail`` is the output of :func:`parse_csrn_page`, or ``None`` for the
    goods and works notices whose body is a PDF we cannot reach.
    """
    detail = detail or {}
    fields = detail.get("fields") or {}
    detail_text = detail.get("text") or ""

    notice_id = notice_id_from_doc(doc)
    title = _text(_first(doc.get("tm_X3b_en_title"))) or ""
    country = _text(_first(doc.get("tm_X3b_en_country")), 100)
    adb_type = _text(_first(doc.get("tm_X3b_en_type"))) or ""
    adb_status = _text(_first(doc.get("tm_X3b_en_status"))) or ""
    deadline = parse_solr_date(doc.get("ds_date_closing"))
    posted = parse_solr_date(doc.get("ds_date_posted"))

    notice_type, group = NOTICE_TYPE_MAP.get(adb_type, (adb_type or "Other Notice", None))

    # Sector comes from ADB's own classification, enriched by the same keyword
    # pass the World Bank notices go through — ADB assigns exactly one sector
    # per notice, so a digital health platform arrives tagged only "Health".
    #
    # The keyword pass reads the *naming* fields and not the whole notice. The
    # CMS record ends in pages of eligibility boilerplate that mentions shipping,
    # insurance and registration on every notice regardless of subject, and
    # letting sector detection see it tags half the feed Transport.
    sectors = map_sectors(doc.get("tm_X3b_en_sector"))
    context = " ".join(
        filter(
            None,
            [
                title,
                adb_type,
                fields.get("Selection Title"),
                fields.get("Package Name"),
                fields.get("Description of assignment"),
            ],
        )
    )
    detected_sectors = detect_sectors(context)
    for detected in detected_sectors:
        if detected not in sectors:
            sectors.append(detected)

    # Budget, duration and qualification extraction *does* read the whole
    # record: those are facts stated once, wherever they appear, and a false
    # positive there is a stray number rather than a mis-filed opportunity.
    parsed = parse_notice_text(detail_text, extra_context=context)
    # Overwrite what that pass detected over the full record with the reading
    # taken from the naming fields, so `parsed_fields` agrees with the `sector`
    # column rather than quietly recording a second, noisier answer.
    if detected_sectors:
        parsed["detected_sectors"] = detected_sectors
    else:
        parsed.pop("detected_sectors", None)
    parsed["adb_type"] = adb_type or None
    parsed["adb_status"] = adb_status or None
    parsed["adb_sectors"] = _all(doc.get("tm_X3b_en_sector"))
    parsed["adb_project_numbers"] = _all(doc.get("tm_X3b_en_project_number"))

    csrn_url = _text(doc.get("ss_csrn_url"))
    if csrn_url:
        parsed["csrn_url"] = csrn_url
    file_url = _text(doc.get("ss_file_url"))
    if file_url:
        # The notice PDF. Recorded so a person can open it even though this
        # service cannot fetch it.
        parsed["document_url"] = f"{ADB_SITE}{file_url}" if file_url.startswith("/") else file_url

    for label, key in _CSRN_KEEP.items():
        value = fields.get(label)
        if value:
            parsed[key] = value

    parsed.update(_parse_budget(fields.get("Consulting Services Budget", "")))
    parsed.update(_parse_period(fields.get("Engagement Period", "")))

    # Contract awards carry the winner and the price, which is competitor
    # intelligence rather than an opportunity, but it is worth keeping.
    contractor = _text(_first(doc.get("tm_X3b_en_contractor_name")))
    if contractor:
        parsed["contractor_name"] = contractor
    price = _first(doc.get("sm_price"))
    if price:
        parsed["contract_price"] = str(price)

    method_name = _text(fields.get("Selection Method"), 100)
    method_code = None
    if method_name:
        code_match = _METHOD_CODE_RE.search(method_name)
        if code_match:
            method_code = code_match.group(1)[:20]

    notice_text = render_csrn_html(fields)
    ss_url = _text(doc.get("ss_url")) or ""

    return {
        "notice_id": notice_id,
        "source": ADB,
        "notice_type": _text(notice_type, 100),
        "noticedate": posted.date() if posted else None,
        "notice_status": STATUS_MAP.get(adb_status, adb_status or "Published")[:50],
        "submission_deadline": deadline,
        "project_id": _text(_first(doc.get("tm_X3b_en_project_number")), 50),
        # ADB's title names the assignment; the umbrella project only has a name
        # of its own on consulting notices, where the CMS calls it the selection
        # title. Falling back to the assignment keeps the column populated.
        "project_name": _text(fields.get("Selection Title")) or title or None,
        "project_country": country,
        "bid_reference_no": _text(
            fields.get("Package Number") or _first(doc.get("tm_X3b_en_approval_number")), 100
        ),
        "bid_description": title or None,
        "procurement_group": group,
        "procurement_method_code": method_code,
        "procurement_method_name": method_name,
        "sector": ", ".join(sectors) if sectors else None,
        "contact_organization": _text(_first(doc.get("tm_X3b_en_executing_agency"))),
        "contact_name": _text(fields.get("Project Officer"), 200),
        "contact_email": _text(fields.get("Email"), 200),
        "contact_phone": None,
        "contact_address": _text(_first(doc.get("tm_X3b_en_location"))),
        "notice_text": notice_text or None,
        # The CMS page's readable text, which is what the scoring engine and the
        # keyword search actually read. Falls back to the rendered body so a
        # notice with fields but no page text is still searchable.
        "notice_text_clean": detail_text or strip_html(notice_text) or None,
        "parsed_fields": parsed,
        # The public ADB page. It is behind a challenge this service cannot
        # pass, but a person's browser opens it fine, and it is the address ADB
        # itself publishes.
        "notice_url": f"{ADB_SITE}{ss_url}" if ss_url.startswith("/") else (ss_url or None),
        "dedup_key": build_dedup_key(title, country, deadline),
    }
