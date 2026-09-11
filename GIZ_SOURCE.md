# GIZ Tender Sources — Exploration & Ingestion Design

Findings from exploring how the Deutsche Gesellschaft für Internationale
Zusammenarbeit (GIZ) publishes procurement opportunities, and the design of the
`giz` spider that ingests them. Explored September 2026.

## Where GIZ publishes tenders

GIZ names its channels on <https://www.giz.de/en/partner/contractor/tenders>:

| # | Channel | URL | What it carries |
|---|---------|-----|-----------------|
| 1 | **Country-office tender pages** | `https://www.giz.de/en/regions/<region>/<country>/tenders` (Indonesia: `/en/regions/asia/indonesia/tenders`) | Local procurement run by the country office: goods and services bought in-country, documents attached as a ZIP. **This is the feed the spider crawls.** |
| 2 | **Vergabemarktplatz GIZ** (e-procurement platform) | `https://ausschreibungen.giz.de/` → `/Satellite/company/welcome.do` | Tenders under German/EU procurement law (UVgO/VgV): HQ purchases and internationally-tendered services, including expert assignments abroad. cosinex-hosted JSP application. |
| 3 | **TED** (Tenders Electronic Daily) | `https://ted.europa.eu` — search `FT=GIZ` | EU-wide notices above threshold. Subset of #2; has a real REST API (`api.ted.europa.eu`). |
| 4 | **bund.de** | `service.bund.de` | German federal announcement portal. Also a subset of #2. |

For tenders abroad GIZ says: "please contact the relevant country office" — i.e.
the country pages (#1) are the official channel for in-country opportunities,
which is what matters for AMANA.

## Source 1: country tender pages (implemented)

### Structure

- Drupal 10. The list is a server-rendered Views block (`dd_country_tenders`,
  view argument = the country page's node id; Indonesia is `6228`). The rows are
  **in the initial HTML** — no browser or JS needed, no auth, no challenge.
- There is also an AJAX endpoint (`POST /en/views/ajax` with
  `view_name=dd_country_tenders&view_args=<node id>`) that returns the same
  rendered rows inside a JSON command array. Plain page fetch is simpler and
  carries the same data, so the spider uses the page.
- **Pagination: none.** The view renders every current tender in one response
  (Indonesia had 6, Viet Nam 2, Ghana 4; `page=1` returns the same rows). The
  spider still follows a Drupal `pager__item--next` link if one ever appears.
- **Publication/update behavior:** tenders appear when the office posts them and
  the row is removed after award/closure. No publication date is shown, so
  "published in the last N days" cannot be filtered upstream — the crawl is
  always a full read of a very small list, and `--days` is accepted but has
  nothing to act on. New/updated/unchanged is decided by the pipeline's
  `content_hash` as with the other feeds.

### Per-row fields

```
div.views-row
  .list-item__meta            → "Deadline: 14.09.2026"   (optional, DD.MM.YYYY)
  .list-item__title span      → "Procurement of Service 10045631-ZME-German Language and ..."
  .download__title span@title → ZIP filename
  .download__info             → file type / size ("1.41 MB")
  a.action@href               → /sites/default/files/media/els-document/<yyyy-mm>/<slug>.zip
```

- The title carries the only structure: a `Procurement of Service|Goods` prefix
  and usually an 8-digit GIZ processing number (`10045631`).
- The document ZIP (ToR, bid documents, forms) downloads **without
  authentication** from `www.giz.de`. Its path embeds the upload month
  (`/2026-08/`), the closest thing to a publication date the page offers.
- Not present: notice body text, contacts, sector, publication date, per-tender
  detail page.

### Field mapping (page → `tenders` row)

| Tender column | From | Note |
|---|---|---|
| `notice_id` | `GIZ-<8-digit number>` from the title, else `GIZ-<sha1(title+country)[:12]>` | number is GIZ's own processing id, stable across re-posts |
| `source` | `giz` | |
| `notice_type` | title prefix — Service → `Request for Proposals` (GIZ's RFP method), Goods → `Invitation for Bids` (RFQ), Works → `Invitation for Bids` | shared vocabulary; raw prefix kept in `parsed_fields.giz_procurement_type` |
| `procurement_group` | Service → `CS`, Goods → `GO`, Works → `CW` | |
| `noticedate` | upload month from the ZIP path, day 1 | approximate; flagged `parsed_fields.noticedate_is_approximate` |
| `notice_status` | `Published` | the page only lists open tenders |
| `submission_deadline` | `Deadline:` meta, stored 23:59 UTC | page gives a date only; end-of-day keeps it open through the local day |
| `project_id`, `bid_reference_no` | the 8-digit number | |
| `project_name` | title minus prefix and number | |
| `project_country` | from the crawled page | |
| `bid_description` | full title | |
| `contact_organization` | `Deutsche Gesellschaft für Internationale Zusammenarbeit (GIZ) GmbH` | |
| `sector` | keyword pass (`detect_sectors`) over the title | |
| `notice_url` | the country tenders page | there is no per-tender page |
| `parsed_fields.document_url` | the ZIP link | absolute |
| `dedup_key` | `build_dedup_key(title, country, deadline)` | lets a GIZ notice link to a co-financed WB/ADB row |

### Limitations

- **Thin metadata.** No body text means heuristic scoring and the LLM read only
  the title. The ZIP holds the real ToR; parsing it (unzip → PDF/DOCX text) is a
  possible enrichment, not done in this pass.
- **Approximate publication date** (upload month).
- **Identity rests on the title's number.** Titles without one fall back to a
  title hash, so a retitled notice would re-appear under a new id — acceptable
  for a list this small.
- Adding another country = adding one `Country|URL` pair (env
  `GIZ_TENDER_PAGES`, comma-separated) — no code change.

## Source 2: Vergabemarktplatz (documented, not implemented)

- `https://ausschreibungen.giz.de/Satellite/company/welcome.do` lists ~220
  current publications, 20 per page, paginated by
  `?method=showTable&selectedTablePagePROJECT_RESULT=<n>` — **stateful**: the
  table lives in a JSP session, so a cookie jar is required.
- Row fields: publication date, submission deadline, title, regulation
  (UVgO/VgV), publication kind (Ausschreibung / TNW / Vergebener Auftrag /
  Beabsichtigte Ausschreibung), contracting authority.
- `projectForwarding.do?pid=<n>` redirects to a **public** overview page
  (`/Satellite/public/company/project/<CX-id>/de/overview`) with procedure
  type, status, deadlines (incl. clarification deadline), CPV codes and the
  notice PDF. Bid documents themselves require registration/participation.
- Mostly German-language, HQ/Germany-centric; the internationally relevant
  service tenders it carries (e.g. framework contracts naming a partner
  country) overlap with TED. **Recommendation:** if HQ-tendered work becomes
  interesting, ingest via the TED REST API filtered on buyer = GIZ rather than
  scraping the session-bound cosinex UI — TED is structured JSON with stable
  pagination.

## Recommended ingestion (as built)

Scrapy spider `giz` reading the country tender pages (Indonesia by default),
normalized in `crawler/giz_parsers.py`, stored through the shared
`ValidationPipeline`/`PostgresPipeline`, deduplicated and scored like every
other feed, and scheduled by the existing cron entry point (`cron.py` /
`docker-entrypoint.sh cron|serve`) — GIZ is in `SPIDERS`, so every scheduled
cycle crawls it alongside `worldbank` and `adb`.
