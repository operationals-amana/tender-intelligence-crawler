"""Score banding vocabulary and thresholds.

Kept in its own module so the engine and the explainer can share it without
importing each other.

Calibration
-----------
Overall scores are a weighted blend of five dimensions, and because the
capability and experience dimensions rest on cosine similarity against short
capability documents, they occupy a naturally low band. Measured over the
current corpus (~700 open World Bank notices scored against AMANA's 73
capability domains and 20 reference projects) the distribution is:

    median 35    p75 44    p90 50    p95 53    p99 58    max 61

The thresholds below are set against that distribution, so "strong" means
roughly the top 2% of live opportunities rather than an absolute 75/100 that
nothing can reach. Retune them whenever the weights, the seed data or the
crawl window change materially -- and revisit them once real bid outcomes are
available, since those are the ground truth these bands are standing in for.
"""

# Band thresholds on the overall score, highest first.
BANDS = (
    (55, "excellent", "Strong opportunity"),
    (47, "good", "Promising opportunity"),
    (38, "moderate", "Possible opportunity"),
    (28, "limited", "Weak opportunity"),
    (0, "poor", "Poor fit"),
)

# Thresholds for the closing recommendation in an explanation.
PURSUE_THRESHOLD = 55
REVIEW_THRESHOLD = 38

# Dimension scores span the full 0-100 range, so they are labelled on their own
# scale rather than against the compressed overall distribution.
DIMENSION_BANDS = (
    (75, "excellent"),
    (60, "good"),
    (40, "moderate"),
    (20, "limited"),
    (0, "poor"),
)


def band(value: float) -> tuple:
    """Return ``(label, verdict)`` for an overall score."""
    for threshold, label, verdict in BANDS:
        if value >= threshold:
            return label, verdict
    return BANDS[-1][1], BANDS[-1][2]


def dimension_label(value: float) -> str:
    """Qualitative label for a single dimension score."""
    for threshold, label in DIMENSION_BANDS:
        if value >= threshold:
            return label
    return DIMENSION_BANDS[-1][1]


def verdict_for(value: float) -> str:
    """Headline verdict for an overall score."""
    return band(value)[1]
