"""Link notices from *different* banks that advertise the same opportunity.

Why link and not merge
----------------------
The World Bank and ADB co-finance work, and a package advertised by both banks
arrives here as two rows. The tempting fix is to merge them into one. That would
be wrong: each bank publishes its own deadline, its own reference number, its
own contact and its own submission channel, and a bid is made against exactly
one of them. Merging discards the half a bid manager needs.

So the rows stay. What this pass adds is a pointer: where two banks carry one
opportunity, one row is canonical and the other carries ``duplicate_of_id`` back
to it. Scoring skips the non-canonical row, so no opportunity is read by Claude
twice, and the dashboard shows it once with a note of the other bank. Nothing is
destroyed, so a wrong link costs a filter toggle rather than a re-crawl.

Why only across banks, and only on an exact fingerprint
-------------------------------------------------------
Rows are grouped by ``dedup_key`` — the significant words of the title, the
country and the closing date, hashed — and a group is only linked where it spans
more than one source.

Both restrictions were learned from the live table rather than assumed.

A fuzzy stage was tried first: blocking by country and closing date, then joining
any pair of titles overlapping by 85% of the smaller one. It produced seven
groups and five were wrong — it merged a national consultant with the
international consultant they support, "specialist 1" with "specialist 2",
equipment batch 8 with batch 2, and school-construction packages of 52, 42 and
25 schools. Those are separate contracts a firm could bid separately.

Removing it left the exact fingerprint, which found two groups, and reading those
two closely is what produced the second restriction: three Türkiye "Procurement
Specialist" notices under one project turned out to be three positions
(``...CS.03.B.01``, ``.02``, ``.03``), and two identically-titled Caribbean
notices turned out to be Grenada's ministry and Dominica's ministry advertising
their halves of a joint procurement. **Same title, country and deadline is not
an identity** — within one bank, the reference number is, and this fingerprint
deliberately ignores it so that two banks can still match.

Which is the resolution: the fingerprint is trusted for exactly the question it
can answer. One bank does not publish one contract twice under two ids, so a
fingerprint collision inside a single feed is two contracts and is left alone.
A collision *across* feeds is the co-financing case this pass exists for.

Underlying all of it is an asymmetry. A missed link shows one extra row, which a
reader sees and dismisses. A wrong link *hides* an opportunity behind another
one, and nobody goes looking for what the list did not show them. So this errs
toward missing.
"""
from collections import defaultdict
from datetime import datetime, timezone

from sqlalchemy import and_, or_
from sqlalchemy.orm import Session, load_only

from crawler.models import Tender
from crawler.parsers import build_dedup_key
from crawler.sources import ACTIONABLE_NOTICE_TYPES, CANONICAL_PRIORITY


def _order(row):
    """Sort key deciding which row of a group represents it.

    Earliest seen wins: it is the row people have already been looking at, and
    it may already carry notes and bookmarks. Source order only breaks a tie
    between rows first seen in the same crawl, and the id breaks that in turn so
    the choice never depends on dictionary ordering.
    """
    return (
        row.first_seen_at or datetime.max.replace(tzinfo=timezone.utc),
        CANONICAL_PRIORITY.get(row.source, 99),
        str(row.id),
    )


def _cross_source_links(members):
    """Given one fingerprint group, return ``(canonical, [duplicates])``.

    Returns ``(None, [])`` for a group confined to a single bank — see the module
    docstring: inside one feed a shared fingerprint means two contracts, not one
    contract twice.

    Where a bank contributed several rows to a group that *does* span banks, only
    its earliest is linked. The rest are that same single-feed case and are left
    alone rather than swept up by their neighbours.
    """
    by_source = defaultdict(list)
    for row in members:
        by_source[row.source].append(row)
    if len(by_source) < 2:
        return None, []

    representatives = sorted(
        (min(rows, key=_order) for rows in by_source.values()), key=_order
    )
    return representatives[0], representatives[1:]


def link_duplicates(db: Session, verbose: bool = True) -> dict:
    """Recompute ``duplicate_of_id`` over the open notices. Returns a summary.

    Only open, biddable notices are considered. A closed notice cannot be bid, so
    grouping it buys nothing; Contract Awards are excluded for the same reason
    and for a sharper one — they carry no deadline, which both slips them past an
    "open tender" filter and leaves them without the one fact that makes a
    fingerprint an identity.

    The pass is a full recomputation rather than an incremental update: it clears
    every existing link on the rows it considers before writing new ones, so a
    notice whose deadline moved and is no longer a duplicate is unlinked rather
    than left pointing at a row it no longer matches.
    """
    now = datetime.now(timezone.utc)
    open_notices = and_(
        Tender.notice_status == "Published",
        Tender.notice_type.in_(ACTIONABLE_NOTICE_TYPES),
        or_(Tender.submission_deadline.is_(None), Tender.submission_deadline >= now),
    )

    # Only the columns this pass reasons about. Loading whole Tender objects
    # would pull `notice_text` and the JSONB alongside them — tens of kilobytes
    # per row, for a pass that never looks at either.
    rows = (
        db.query(Tender)
        .options(
            load_only(
                Tender.id,
                Tender.source,
                Tender.dedup_key,
                Tender.duplicate_of_id,
                Tender.first_seen_at,
                Tender.bid_description,
                Tender.project_name,
                Tender.project_country,
                Tender.submission_deadline,
            )
        )
        .filter(open_notices)
        .all()
    )

    # Rows crawled before the key existed carry none, and would otherwise stay
    # ungrouped until they happened to be re-crawled. Filling them in here makes
    # the pass self-healing and saves a one-off backfill script.
    backfilled = 0
    for row in rows:
        if not row.dedup_key:
            key = build_dedup_key(
                row.bid_description or row.project_name or "",
                row.project_country,
                row.submission_deadline,
            )
            if key:
                row.dedup_key = key
                backfilled += 1

    by_key = defaultdict(list)
    for row in rows:
        if row.dedup_key:
            by_key[row.dedup_key].append(row)

    # Clear first, then relink, so this is a recomputation and not an accretion.
    cleared = 0
    for row in rows:
        if row.duplicate_of_id is not None:
            row.duplicate_of_id = None
            cleared += 1

    groups = 0
    linked = 0
    for members in by_key.values():
        if len(members) < 2:
            continue
        canonical, duplicates = _cross_source_links(members)
        if canonical is None:
            continue
        groups += 1
        for row in duplicates:
            row.duplicate_of_id = canonical.id
            linked += 1

    # A row outside this pass's scope can still point at one inside it — a
    # notice that closed since the last run, for instance. Leaving that pointer
    # would hide a closed notice behind a link nothing maintains any more.
    #
    # Expressed as the negation of the scope predicate rather than as "not in
    # this list of ids": the list grows with every deadline-less notice ever
    # crawled, and binding one parameter per id walks into Postgres's 65,535
    # parameter ceiling long before that becomes obvious.
    stale = (
        db.query(Tender)
        .filter(Tender.duplicate_of_id.isnot(None), ~open_notices)
        .update({Tender.duplicate_of_id: None}, synchronize_session=False)
    )

    db.commit()

    summary = {
        "considered": len(rows),
        "keys_backfilled": backfilled,
        "groups": groups,
        "linked": linked,
        "unlinked": cleared + stale,
    }
    if verbose:
        print(
            f"Duplicate linking: {groups} opportunity/-ies carried by more than one "
            f"bank, over {len(rows)} open notices; {linked} row(s) linked."
        )
    return summary
