"""Batch scoring: compute and persist TenderScore rows.

Scoring is a bulk operation -- the TF-IDF corpus is built once and reused across
every tender -- so it runs as a batch job after a crawl rather than per request.
"""
from datetime import datetime, timezone
from typing import Iterable, Optional

from sqlalchemy import or_
from sqlalchemy.orm import Session

from api.scoring.engine import ACTIONABLE_NOTICE_TYPES, ScoringEngine
from crawler.models import Tender, TenderScore


def active_tender_query(db: Session, include_expired: bool = False):
    """Tenders worth scoring: published, biddable, and not already closed."""
    query = db.query(Tender).filter(
        Tender.notice_status == "Published",
        # Contract Awards are already decided and have no deadline, so without
        # this filter they would slip through the "no deadline" branch below.
        Tender.notice_type.in_(ACTIONABLE_NOTICE_TYPES),
    )
    if not include_expired:
        now = datetime.now(timezone.utc)
        # General Procurement Notices carry no deadline but do flag upcoming
        # work, so a missing deadline is kept rather than treated as expired.
        query = query.filter(
            or_(Tender.submission_deadline.is_(None), Tender.submission_deadline >= now)
        )
    return query


def score_tenders(
    db: Session,
    limit: Optional[int] = None,
    rescore_all: bool = False,
    include_expired: bool = False,
    verbose: bool = True,
) -> dict:
    """Score tenders and upsert their TenderScore rows.

    By default only unscored tenders are processed; ``rescore_all`` recomputes
    everything, which is what you want after changing weights or seed data.
    """
    engine = ScoringEngine(db)
    if not engine.is_ready:
        raise RuntimeError(
            "Scoring engine has no company data to match against. "
            "Run `python -m api.seed.run_all` first."
        )

    query = active_tender_query(db, include_expired=include_expired)
    if not rescore_all:
        scored_ids = {row[0] for row in db.query(TenderScore.tender_id).all()}
        if scored_ids:
            query = query.filter(~Tender.id.in_(scored_ids))

    query = query.order_by(Tender.submission_deadline.asc().nullslast())
    if limit:
        query = query.limit(limit)

    tenders = query.all()
    if verbose:
        print(
            f"Scoring {len(tenders)} tenders against "
            f"{len(engine.domains)} capability domains and {len(engine.projects)} past projects..."
        )

    existing = {
        score.tender_id: score
        for score in db.query(TenderScore)
        .filter(TenderScore.tender_id.in_([t.id for t in tenders]))
        .all()
    } if tenders else {}

    created = 0
    updated = 0

    for index, tender in enumerate(tenders, start=1):
        breakdown = engine.score(tender)

        values = dict(
            overall_score=breakdown.overall,
            deadline_proximity_score=breakdown.deadline,
            capability_match_score=breakdown.capability,
            sector_relevance_score=breakdown.sector,
            experience_score=breakdown.experience,
            network_score=breakdown.network,
            win_probability=breakdown.win_probability,
            matched_skills=breakdown.matched_skills,
            matched_projects=breakdown.matched_projects,
            gaps=breakdown.gaps,
            risks=breakdown.risks,
            explanation=breakdown.explanation,
            scored_at=datetime.now(timezone.utc),
        )

        score = existing.get(tender.id)
        if score is None:
            db.add(TenderScore(tender_id=tender.id, **values))
            created += 1
        else:
            for key, value in values.items():
                setattr(score, key, value)
            updated += 1

        if index % 200 == 0:
            db.commit()
            if verbose:
                print(f"  ...{index}/{len(tenders)}")

    db.commit()

    summary = {"scored": len(tenders), "created": created, "updated": updated}
    if verbose:
        print(f"Done: {created} created, {updated} updated.")
    return summary


def _apply_assessment(score: TenderScore, assessment) -> None:
    """Copy one LLM assessment onto a TenderScore row."""
    now = datetime.now(timezone.utc)

    if not assessment.ok:
        score.llm_error = assessment.error
        score.llm_model = assessment.model
        score.llm_scored_at = now
        return

    data = assessment.data
    score.llm_overall_score = data.get("overall_fit")
    score.llm_capability_score = data.get("capability_fit")
    score.llm_sector_score = data.get("sector_fit")
    score.llm_experience_score = data.get("experience_fit")
    score.llm_win_probability = data.get("win_probability")
    score.llm_confidence = data.get("confidence")
    score.llm_recommendation = data.get("recommendation")
    score.llm_rationale = data.get("rationale")
    score.llm_scope_summary = data.get("scope_summary")
    score.llm_matched_capabilities = data.get("matched_capabilities") or []
    score.llm_relevant_projects = data.get("relevant_projects") or []
    score.llm_key_requirements = data.get("key_requirements") or []
    score.llm_gaps = data.get("gaps") or []
    score.llm_risks = data.get("risks") or []
    score.llm_suggested_team = data.get("suggested_team") or []
    score.llm_model = assessment.model
    score.llm_error = None
    score.llm_scored_at = now


# Procurement groups AMANA can actually bid. Goods (GO), Civil Works (CW) and
# Non-consulting Services (NC) are structurally outside an advisory firm's
# offering, so there is no point spending tokens reading them.
BIDDABLE_PROCUREMENT_GROUPS = {"CS"}


def run_llm_assessment(
    db: Session,
    limit: int = 300,
    min_heuristic_score: Optional[float] = None,
    rescore: bool = False,
    concurrency: int = 4,
    consulting_only: bool = True,
    verbose: bool = True,
) -> dict:
    """Deep-assess shortlisted tenders with Claude.

    This is the second half of the hybrid pipeline: the heuristic ranks every
    open tender cheaply, and Claude then actually reads the shortlist.

    The shortlist is drawn from consulting-services notices rather than from the
    top of the heuristic ranking alone. The heuristic ranks on keyword overlap,
    which puts plenty of unbiddable work at the top -- an environmental-safeguards
    role scores well on "institutional" and Indonesia without being remotely
    biddable. Filtering to the procurement categories AMANA can sell into is a
    far better prefilter than the score, and it drops roughly half the open
    notices before a single token is spent.
    """
    from api.scoring.llm import LLMScorer, MIN_CACHEABLE_TOKENS

    query = (
        db.query(TenderScore, Tender)
        .join(Tender, TenderScore.tender_id == Tender.id)
        .filter(
            Tender.notice_status == "Published",
            Tender.notice_type.in_(ACTIONABLE_NOTICE_TYPES),
            or_(
                Tender.submission_deadline.is_(None),
                Tender.submission_deadline >= datetime.now(timezone.utc),
            ),
        )
    )

    if consulting_only:
        # General Procurement Notices carry no procurement group but announce
        # upcoming work, so they are kept alongside the consulting notices.
        query = query.filter(
            or_(
                Tender.procurement_group.in_(BIDDABLE_PROCUREMENT_GROUPS),
                Tender.notice_type == "General Procurement Notice",
            )
        )
    if min_heuristic_score is not None:
        query = query.filter(TenderScore.overall_score >= min_heuristic_score)
    if not rescore:
        query = query.filter(TenderScore.llm_scored_at.is_(None))

    rows = query.order_by(TenderScore.overall_score.desc()).limit(limit).all()
    if not rows:
        if verbose:
            print("Nothing to assess — every shortlisted tender already has an LLM score.")
        return {"assessed": 0, "errors": 0, "cost_usd": 0.0}

    scorer = LLMScorer(db)
    if verbose:
        tokens = scorer.profile_tokens()
        cacheable = "will cache" if tokens >= MIN_CACHEABLE_TOKENS else "TOO SHORT TO CACHE"
        print(f"Model: {scorer.model}")
        print(f"Cached company profile: {tokens} tokens ({cacheable})")
        print(f"Assessing {len(rows)} shortlisted tenders with concurrency {concurrency}...")

    by_id = {str(score.tender_id): score for score, _ in rows}
    items = [
        (
            tender,
            {
                "overall": round(score.overall_score or 0),
                "capability": round(score.capability_match_score or 0),
                "skills": (score.matched_skills or [])[:5],
            },
        )
        for score, tender in rows
    ]

    done = {"n": 0}

    def on_result(assessment):
        done["n"] += 1
        score = by_id.get(assessment.tender_id)
        if score is not None:
            _apply_assessment(score, assessment)
        if verbose and done["n"] % 25 == 0:
            print(f"  ...{done['n']}/{len(items)}")

    assessments = scorer.assess_many(items, concurrency=concurrency, on_result=on_result)
    db.commit()

    errors = sum(1 for a in assessments if not a.ok)
    cost = scorer.estimated_cost_usd()

    if verbose:
        stats = scorer.stats
        cached = stats["cache_read_tokens"]
        fresh = stats["input_tokens"]
        hit_rate = cached / (cached + fresh) * 100 if (cached + fresh) else 0
        print(
            f"Done: {len(assessments) - errors} assessed, {errors} errors.\n"
            f"Tokens: {fresh} fresh in, {cached} cached in "
            f"({hit_rate:.0f}% of input served from cache), "
            f"{stats['output_tokens']} out.\n"
            f"Estimated cost: ${cost:.3f}"
        )

    return {
        "assessed": len(assessments) - errors,
        "errors": errors,
        "cost_usd": round(cost, 4),
        "stats": scorer.stats,
    }


def score_single(db: Session, tender: Tender, engine: Optional[ScoringEngine] = None):
    """Score one tender on demand, persisting the result."""
    engine = engine or ScoringEngine(db)
    breakdown = engine.score(tender)

    score = (
        db.query(TenderScore).filter(TenderScore.tender_id == tender.id).one_or_none()
    )
    values = dict(
        overall_score=breakdown.overall,
        deadline_proximity_score=breakdown.deadline,
        capability_match_score=breakdown.capability,
        sector_relevance_score=breakdown.sector,
        experience_score=breakdown.experience,
        network_score=breakdown.network,
        win_probability=breakdown.win_probability,
        matched_skills=breakdown.matched_skills,
        matched_projects=breakdown.matched_projects,
        gaps=breakdown.gaps,
        risks=breakdown.risks,
        explanation=breakdown.explanation,
        scored_at=datetime.now(timezone.utc),
    )

    if score is None:
        score = TenderScore(tender_id=tender.id, **values)
        db.add(score)
    else:
        for key, value in values.items():
            setattr(score, key, value)

    db.commit()
    db.refresh(score)
    return score
