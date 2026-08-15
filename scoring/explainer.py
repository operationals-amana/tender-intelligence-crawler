"""Turn a score breakdown into prose a bid manager can act on.

The explanation is generated from the same numbers that produced the score, so
the narrative and the dimension bars in the UI can never disagree.
"""
from scoring.engine_constants import (
    PURSUE_THRESHOLD,
    REVIEW_THRESHOLD,
    dimension_label,
    verdict_for,
)


def _format_deadline(breakdown) -> str:
    remaining = breakdown.days_until_deadline
    if remaining is None:
        return "No submission deadline is published for this notice."
    if remaining < 0:
        return f"The submission deadline passed {abs(remaining)} days ago."
    if remaining == 0:
        return "The submission deadline is today."
    return f"There are {remaining} days until the submission deadline."


def build_explanation(tender, breakdown) -> str:
    """Compose the multi-section explanation stored on the score record."""
    lines = []

    verdict = verdict_for(breakdown.overall)
    country = tender.project_country or "an unspecified country"
    lines.append(
        f"{verdict} - overall fit {breakdown.overall:.0f}/100 with an estimated "
        f"{breakdown.win_probability:.0f}% win probability. "
        f"This is a {tender.notice_type or 'procurement notice'} in {country}. "
        f"{_format_deadline(breakdown)}"
    )

    lines.append("")
    lines.append("Score breakdown:")
    for label, value in (
        ("Capability match", breakdown.capability),
        ("Sector relevance", breakdown.sector),
        ("Past experience", breakdown.experience),
        ("Deadline urgency", breakdown.deadline),
        ("Network strength", breakdown.network),
    ):
        lines.append(f"  - {label}: {value:.0f}/100 ({dimension_label(value)})")

    if breakdown.matched_skills:
        lines.append("")
        lines.append("Matching capabilities:")
        for skill in breakdown.matched_skills:
            lines.append(f"  - {skill}")

    if breakdown.matched_projects:
        lines.append("")
        lines.append("Comparable past work:")
        for project in breakdown.matched_projects:
            lines.append(f"  - {project}")

    if breakdown.gaps:
        lines.append("")
        lines.append("Capability gaps:")
        for gap in breakdown.gaps:
            lines.append(f"  - {gap}")

    if breakdown.risks:
        lines.append("")
        lines.append("Risks:")
        for risk in breakdown.risks:
            lines.append(f"  - {risk}")

    lines.append("")
    lines.append(_recommendation(breakdown))

    return "\n".join(lines)


def _recommendation(breakdown) -> str:
    """Close with the suggested next action."""
    remaining = breakdown.days_until_deadline

    if remaining is not None and remaining < 0:
        return "Recommendation: no action - this opportunity has closed."

    if breakdown.overall >= PURSUE_THRESHOLD:
        if remaining is not None and remaining <= 14:
            return (
                "Recommendation: pursue immediately. The fit is strong but the window "
                "is short, so confirm staffing availability before committing."
            )
        return "Recommendation: pursue. Assign a bid lead and begin drafting the expression of interest."

    if breakdown.overall >= REVIEW_THRESHOLD:
        return (
            "Recommendation: review. There is a workable fit, but confirm the capability "
            "gaps above can be covered, potentially through a partner or associate."
        )

    return (
        "Recommendation: deprioritise unless there is a strategic reason to bid - "
        "the scope sits outside AMANA's demonstrated strengths."
    )
