"""The upstream feeds this service crawls, and the vocabulary they share.

Every tender row carries the ``source`` it came from. The column exists so the
dashboard can say where an opportunity was published and filter on it, but it
also does quieter work: notice ids are only unique *within* a feed, so anything
that reasons about identity across feeds has to know which one a row belongs to.

Notice ids are prefixed per source rather than left bare. World Bank ids already
look like ``OP00427268`` and ADB's are bare Drupal node numbers, so a collision
is unlikely today -- but "unlikely" is not a constraint the database enforces,
and a prefix is. World Bank rows keep their historical unprefixed ids because
22,000 of them are already stored, bookmarked and scored under those ids;
rewriting them to gain a prefix would be a migration that buys nothing.
"""

WORLD_BANK = "worldbank"
ADB = "adb"
GIZ = "giz"

SOURCES = (WORLD_BANK, ADB, GIZ)

#: Human-readable feed names, for logs and for the dashboard's source filter.
SOURCE_LABELS = {
    WORLD_BANK: "World Bank",
    ADB: "Asian Development Bank",
    GIZ: "GIZ (Deutsche Gesellschaft für Internationale Zusammenarbeit)",
}

#: Preference order when two feeds carry the same opportunity and one has to be
#: named canonical. It is a tie-break, not a judgement: the earlier-seen notice
#: wins first, and this only decides rows that arrived in the same crawl.
CANONICAL_PRIORITY = {WORLD_BANK: 0, ADB: 1, GIZ: 2}

#: Notice types that represent an actual biddable opportunity, in the shared
#: vocabulary both feeds are normalized into.
#:
#: Declared here rather than in the scoring engine because it describes the
#: *feed*, not the scoring: duplicate linking needs it as much as scoring does,
#: and reaching into `scoring` for it would drag scikit-learn into a crawl that
#: has no use for it. The engine re-exports it.
#:
#: Contract Awards are deliberately excluded: they are closed by definition, and
#: because they carry no submission deadline they would otherwise pass an "open
#: tender" filter.
ACTIONABLE_NOTICE_TYPES = {
    "Request for Expression of Interest",
    "Invitation for Bids",
    "Invitation for Prequalification",
    "Specific Procurement Notice",
    "General Procurement Notice",
    "Request for Proposals",
    # ADB's pre-tender heads-up, and the same kind of signal a General
    # Procurement Notice is: work that is coming but not yet open. Like a GPN it
    # carries no deadline, which is why the "no deadline" branch of the open-
    # tender filter exists.
    "Advance Notice",
}
