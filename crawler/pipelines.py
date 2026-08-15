"""Scrapy item pipelines: validate notices, then upsert them into Postgres.

Deduplication and persistence deliberately live in the *same* pipeline stage.
Splitting them means two round trips and two sessions per notice, and leaves a
race where the dedup check and the write disagree. Here a single session per
batch looks the notice up by ``notice_id`` and decides insert / update / skip
from the content hash.
"""
from datetime import datetime, timezone

from itemadapter import ItemAdapter
from sqlalchemy.exc import SQLAlchemyError

from crawler.database import check_connection, get_db
from crawler.models import CrawlError, Tender

# Columns copied straight from the item onto the Tender row.
TENDER_FIELDS = [
    "notice_id",
    "source",
    "notice_type",
    "noticedate",
    "notice_status",
    "submission_deadline",
    "project_id",
    "project_name",
    "project_country",
    "bid_reference_no",
    "bid_description",
    "procurement_group",
    "procurement_method_code",
    "procurement_method_name",
    "sector",
    "contact_organization",
    "contact_name",
    "contact_email",
    "contact_phone",
    "contact_address",
    "notice_text",
    "notice_text_clean",
    "parsed_fields",
    "notice_url",
    "content_hash",
    "dedup_key",
]

# Flush to Postgres every N notices rather than per row.
BATCH_SIZE = 50


class ValidationPipeline:
    """Drop notices that carry no usable identity."""

    def process_item(self, item, spider):
        adapter = ItemAdapter(item)
        if not adapter.get("notice_id"):
            spider.logger.warning("Notice without an id, dropping")
            return None
        return item


class PostgresPipeline:
    """Upsert notices, tracking new/updated/unchanged counts on the spider."""

    def open_spider(self, spider):
        check_connection()
        self.db = get_db()
        # Notices buffered since the last commit, so a failed batch can be
        # retried one row at a time instead of discarding all of them.
        self.batch = []
        # The crawl run id is injected by run_crawler so errors can be attributed.
        self.crawl_run_id = getattr(spider, "crawl_run_id", None)
        spider.notices_new = 0
        spider.notices_updated = 0
        spider.notices_unchanged = 0
        spider.notices_errored = 0

    def close_spider(self, spider):
        try:
            self._flush(spider)
        finally:
            self.db.close()
        spider.logger.info(
            "Stored: %d new, %d updated, %d unchanged, %d errors",
            spider.notices_new,
            spider.notices_updated,
            spider.notices_unchanged,
            spider.notices_errored,
        )

    def process_item(self, item, spider):
        adapter = ItemAdapter(item)
        values = {field: adapter.get(field) for field in TENDER_FIELDS}
        # Carried alongside the columns, not as one; `_write` consumes it.
        values["_partial"] = bool(adapter.get("partial"))
        self.batch.append(values)

        if len(self.batch) >= BATCH_SIZE:
            self._flush(spider)

        return item

    def _flush(self, spider):
        """Write the buffered notices, falling back to row-by-row on failure."""
        if not self.batch:
            return

        batch, self.batch = self.batch, []

        try:
            counts = self._write(batch)
            self.db.commit()
        except SQLAlchemyError as exc:
            # One malformed notice must not cost us the other 49, so replay the
            # batch individually and isolate the row that actually failed.
            self.db.rollback()
            spider.logger.warning("Batch write failed (%s); retrying individually.", exc)
            counts = self._write_individually(spider, batch)

        spider.notices_new += counts["new"]
        spider.notices_updated += counts["updated"]
        spider.notices_unchanged += counts["unchanged"]

    def _write(self, batch) -> dict:
        counts = {"new": 0, "updated": 0, "unchanged": 0}
        now = datetime.now(timezone.utc)

        for row in batch:
            values = {k: v for k, v in row.items() if k != "_partial"}
            partial = row.get("_partial", False)
            notice_id = values["notice_id"]
            existing = (
                self.db.query(Tender).filter(Tender.notice_id == notice_id).one_or_none()
            )

            if existing is None:
                self.db.add(
                    Tender(
                        **values,
                        change_flag="new",
                        first_seen_at=now,
                        updated_at=now,
                        last_crawled_at=now,
                    )
                )
                counts["new"] += 1
                continue

            # Unchanged payloads still get their crawl timestamp refreshed, so
            # "last seen" stays meaningful without rewriting the whole row.
            if existing.content_hash == values.get("content_hash"):
                existing.last_crawled_at = now
                counts["unchanged"] += 1
                continue

            deadline_moved = existing.submission_deadline != values.get("submission_deadline")
            if partial:
                values = self._without_blanks(existing, values)
            for field, value in values.items():
                setattr(existing, field, value)
            existing.change_flag = "extended" if deadline_moved else "modified"
            existing.last_crawled_at = now
            existing.updated_at = now
            counts["updated"] += 1

        return counts

    @staticmethod
    def _without_blanks(existing, values: dict) -> dict:
        """Drop the fields a partial item has nothing to say about.

        A notice whose body could not be fetched still arrives as a full item,
        with the body, the contacts and the extracted budget simply absent. Left
        alone, the update would write those blanks over a body we already have
        and successfully stored on an earlier crawl -- so one timed-out detail
        page would silently strip a notice back to its metadata.

        `parsed_fields` is merged rather than dropped: the partial item carries
        real facts from the listing, and the stored copy carries the ones the
        detail page contributed.

        `content_hash` is *not* protected, deliberately. It records what the
        crawl fetched rather than what the row now holds, so after a partial
        update it no longer matches the stored body — which is what makes the
        next successful crawl see a difference and re-sync the row instead of
        reading "unchanged" and leaving a half-written notice in place.
        """
        kept = {}
        for field, value in values.items():
            current = getattr(existing, field, None)

            if field == "parsed_fields":
                if isinstance(current, dict) and isinstance(value, dict):
                    kept[field] = {**current, **value}
                else:
                    kept[field] = value or current
                continue

            # Only a *blank* new value defers to the stored one. A partial item
            # that actually carries a field is still the fresher answer.
            if value in (None, "") and current not in (None, ""):
                continue
            kept[field] = value
        return kept

    def _write_individually(self, spider, batch) -> dict:
        counts = {"new": 0, "updated": 0, "unchanged": 0}

        for values in batch:
            try:
                single = self._write([values])
                self.db.commit()
                for key in counts:
                    counts[key] += single[key]
            except SQLAlchemyError as exc:
                self.db.rollback()
                spider.notices_errored += 1
                spider.logger.error("Failed to store %s: %s", values.get("notice_id"), exc)
                self._log_error(values.get("notice_id"), exc)

        return counts

    def _log_error(self, notice_id, exc):
        """Record the failure for review; never let logging break the crawl."""
        try:
            self.db.add(
                CrawlError(
                    crawl_run_id=self.crawl_run_id,
                    notice_id=notice_id,
                    error_message=str(exc)[:2000],
                    error_type=type(exc).__name__,
                )
            )
            self.db.commit()
        except SQLAlchemyError:
            self.db.rollback()
