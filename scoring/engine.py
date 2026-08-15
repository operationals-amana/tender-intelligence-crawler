"""Weighted multi-factor opportunity scoring.

Each active tender is scored 0-100 across five dimensions, combined with the
weights below. Every dimension returns a plain 0-100 number plus the evidence
behind it, so the explanation shown in the UI is derived from the same values
that produced the score rather than written separately.
"""
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional

# Re-exported below for the callers that have always imported it from here.
from crawler.sources import ACTIONABLE_NOTICE_TYPES  # noqa: F401
from scoring.matcher import (
    CapabilityMatcher,
    DomainMatch,
    ProjectMatch,
    build_domain_profiles,
    build_project_profiles,
    tender_text,
)

WEIGHTS = {
    "deadline": 0.15,
    "capability": 0.35,
    "sector": 0.20,
    "experience": 0.20,
    "network": 0.10,
}

# A deadline this far out scores 0 on urgency; anything nearer scales up.
DEADLINE_HORIZON_DAYS = 90
# Below this many days a bid is realistically too tight to mobilise for.
DEADLINE_RISK_DAYS = 14

# `ACTIONABLE_NOTICE_TYPES` — the notice types that represent an actual biddable
# opportunity — is imported at the top of this module and re-exported from here,
# where it used to be defined. It moved to `crawler/sources.py` because duplicate
# linking needs the same set, and reaching into the scoring engine for it would
# drag scikit-learn into a crawl that has no use for it.

# AMANA is an advisory firm: consulting-services notices are the target market.
# ADB's notices for consulting *firms* are normalized to this same group, while
# its individual-expert assignments carry `IC` and stay out — a firm cannot bid
# a named-person contract. See `crawler/adb_parsers.py`.
PREFERRED_PROCUREMENT_GROUPS = {"CS"}

# Countries where AMANA has an established presence.
HOME_COUNTRIES = {"indonesia"}

# Clients that indicate an existing institutional relationship.
KNOWN_CLIENT_KEYWORDS = [
    "world bank", "ministry of health", "ministry of education", "ministry of finance",
    "ministry of communications", "prospera", "usaid", "british embassy", "eiti",
    "kadin", "bappenas", "kppu",
]


@dataclass
class ScoreBreakdown:
    """Result of scoring one tender."""

    overall: float = 0.0
    deadline: float = 0.0
    capability: float = 0.0
    sector: float = 0.0
    experience: float = 0.0
    network: float = 0.0
    win_probability: float = 0.0
    matched_skills: List[str] = field(default_factory=list)
    matched_projects: List[str] = field(default_factory=list)
    gaps: List[str] = field(default_factory=list)
    risks: List[str] = field(default_factory=list)
    explanation: str = ""
    domain_matches: List[DomainMatch] = field(default_factory=list)
    project_matches: List[ProjectMatch] = field(default_factory=list)
    days_until_deadline: Optional[int] = None


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _as_aware(value: Optional[datetime]) -> Optional[datetime]:
    """Postgres may hand back naive datetimes; treat those as UTC."""
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def days_until(deadline: Optional[datetime]) -> Optional[int]:
    deadline = _as_aware(deadline)
    if deadline is None:
        return None
    return (deadline - _now()).days


def score_deadline(tender) -> tuple:
    """Urgency: nearer deadlines score higher, within a 90-day horizon."""
    remaining = days_until(tender.submission_deadline)

    if remaining is None:
        # No published deadline (typical of contract awards and GPNs). Score it
        # neutrally rather than penalising a notice for a missing field.
        return 40.0, remaining

    if remaining < 0:
        return 0.0, remaining

    score = 100.0 * (1 - min(remaining, DEADLINE_HORIZON_DAYS) / DEADLINE_HORIZON_DAYS)
    return round(score, 1), remaining


def score_capability(matches: List[DomainMatch]) -> tuple:
    """Capability fit: similarity to our domains, weighted by bench strength.

    A domain we merely recognise scores lower than one where several people sit
    at Expert level, so ``strength`` modulates each match.
    """
    if not matches:
        return 0.0, []

    max_strength = max((m.domain.strength for m in matches), default=0.0) or 1.0

    weighted = 0.0
    total_weight = 0.0
    for rank, match in enumerate(matches):
        # Rank decay: the top domain carries the most signal.
        rank_weight = 1.0 / (1 + rank * 0.6)
        bench = 0.4 + 0.6 * (match.domain.strength / max_strength)
        weighted += match.similarity * bench * rank_weight
        total_weight += rank_weight

    raw = weighted / total_weight if total_weight else 0.0

    # Cosine similarities against short domain documents land in a low band, so
    # rescale into a usable 0-100 range and clamp.
    score = min(100.0, raw * 320)

    skills = []
    for match in matches[:5]:
        people = match.domain.top_people(2)
        if people:
            level = "Expert" if match.domain.experts else "Practitioner"
            skills.append(f"{match.domain.name} ({level}: {', '.join(people)})")
        else:
            skills.append(match.domain.name)

    return round(score, 1), skills


def score_sector(tender, matches: List[DomainMatch], projects: List[ProjectMatch]) -> float:
    """Sector relevance: does this tender's sector overlap our practice areas?"""
    tender_sectors = {s.strip().lower() for s in (tender.sector or "").split(",") if s.strip()}
    if not tender_sectors:
        detected = (tender.parsed_fields or {}).get("detected_sectors") or []
        tender_sectors = {s.lower() for s in detected}

    if not tender_sectors:
        return 30.0  # unknown sector: neutral-low rather than zero

    # Sectors we can evidence, from our capability map and delivered projects.
    covered = set()
    for match in matches:
        if match.domain.sector:
            covered.add(match.domain.sector.lower())
        covered.add(match.domain.name.lower())
    for match in projects:
        for tag in match.project.sector_tags:
            covered.add(tag.lower())
        if match.project.practice_group:
            covered.add(match.project.practice_group.lower())

    hits = 0
    for sector in tender_sectors:
        tokens = [t for t in sector.replace("&", " ").split() if len(t) > 3]
        if any(any(token in c for c in covered) for token in tokens):
            hits += 1

    coverage = hits / len(tender_sectors)
    score = 100.0 * coverage

    # Consulting-services notices are the ones AMANA can actually bid.
    if tender.procurement_group in PREFERRED_PROCUREMENT_GROUPS:
        score = min(100.0, score + 15)
    elif tender.procurement_group in {"GO", "CW"}:
        # Goods and civil works are outside an advisory firm's offering.
        score *= 0.4

    return round(score, 1)


def score_experience(tender, projects: List[ProjectMatch]) -> tuple:
    """Past-performance fit: similarity to delivered work, plus context bonuses."""
    if not projects:
        return 0.0, []

    best = projects[0].similarity
    mean_top = sum(m.similarity for m in projects[:3]) / min(len(projects), 3)
    raw = 0.6 * best + 0.4 * mean_top
    score = min(100.0, raw * 300)

    country = (tender.project_country or "").lower()
    if country and country in HOME_COUNTRIES:
        score = min(100.0, score + 15)

    # Having delivered for this client (or one like it) is strong evidence.
    client_text = " ".join(
        filter(None, [tender.contact_organization, tender.project_name])
    ).lower()
    if any(keyword in client_text for keyword in KNOWN_CLIENT_KEYWORDS):
        score = min(100.0, score + 10)

    labels = []
    for match in projects[:3]:
        year = match.project.end_year or match.project.start_year
        suffix = f", {year}" if year else ""
        client = f" - {match.project.client}" if match.project.client else ""
        labels.append(f"{match.project.name}{suffix}{client}"[:220])

    return round(score, 1), labels


def score_network(db_context, tender, projects: List[ProjectMatch]) -> float:
    """Relationship strength: country presence and known client relationships."""
    score = 0.0
    country = (tender.project_country or "").lower()

    # AMANA's association network is Indonesian, so it only applies at home.
    if country in HOME_COUNTRIES:
        score += 50.0
        if db_context.get("association_count", 0) > 0:
            score += 15.0
    elif any((m.project.country or "").lower() == country for m in projects if country):
        score += 35.0  # delivered in this country before

    client_text = " ".join(
        filter(None, [tender.contact_organization, tender.project_name])
    ).lower()
    if any(keyword in client_text for keyword in KNOWN_CLIENT_KEYWORDS):
        score += 30.0

    # Every World Bank notice is at least a familiar counterparty.
    if db_context.get("has_world_bank_experience"):
        score += 10.0

    return round(min(100.0, score), 1)


def win_probability(breakdown: ScoreBreakdown, tender, context: Dict) -> float:
    """Heuristic v1 win likelihood.

    Deliberately simple and explainable; it should be replaced with a model
    fitted on real bid outcomes once enough have been recorded.
    """
    remaining = breakdown.days_until_deadline
    if remaining is None:
        timing = 0.5
    elif remaining < 0:
        timing = 0.0
    else:
        # More lead time means a better shot at a competitive proposal.
        timing = min(1.0, remaining / 45.0)

    probability = (
        0.30 * (breakdown.capability / 100)
        + 0.25 * (breakdown.experience / 100)
        + 0.20 * timing
        + 0.15 * (breakdown.network / 100)
        + 0.10 * (1.0 if context.get("has_world_bank_experience") else 0.5)
    ) * 100

    return round(min(100.0, probability), 1)


def identify_gaps(matches: List[DomainMatch], breakdown: ScoreBreakdown) -> List[str]:
    """Describe where the bench is thin for this opportunity."""
    gaps = []

    for match in matches[:5]:
        domain = match.domain
        if not domain.experts and match.similarity > 0.05:
            depth = domain.people.get("Proficient") or domain.people.get("Applied")
            if depth:
                gaps.append(
                    f"No Expert-level cover in {domain.name}; deepest is "
                    f"{'Proficient' if domain.people.get('Proficient') else 'Applied'}"
                )
            else:
                gaps.append(f"No staffed capability in {domain.name}")

    if breakdown.capability < 30:
        gaps.append("Weak overall capability alignment with the stated scope")
    if not matches:
        gaps.append("No capability domain matched this notice's scope")

    return gaps[:5]


def identify_risks(tender, breakdown: ScoreBreakdown, projects: List[ProjectMatch]) -> List[str]:
    """Flag the practical reasons this bid could go wrong."""
    risks = []
    remaining = breakdown.days_until_deadline

    if remaining is not None:
        if remaining < 0:
            risks.append("Submission deadline has passed")
        elif remaining <= DEADLINE_RISK_DAYS:
            risks.append(f"Tight deadline: {remaining} days remaining")
    else:
        risks.append("No submission deadline published; confirm timing with the client")

    country = (tender.project_country or "").strip()
    if country and country.lower() not in HOME_COUNTRIES:
        delivered_here = any((m.project.country or "").lower() == country.lower() for m in projects)
        if not delivered_here:
            risks.append(f"No delivery track record in {country}")

    if tender.procurement_group in {"GO", "CW"}:
        risks.append(
            f"Procurement type is {'Goods' if tender.procurement_group == 'GO' else 'Civil Works'}, "
            "outside AMANA's advisory offering"
        )

    quals = (tender.parsed_fields or {}).get("qualifications") or {}
    min_years = quals.get("min_years_experience")
    if isinstance(min_years, int) and min_years >= 15:
        risks.append(f"Demands {min_years}+ years of experience from key personnel")

    if breakdown.experience < 25:
        risks.append("Little comparable past work to cite in the proposal")

    return risks[:5]


class ScoringEngine:
    """Scores tenders against AMANA's capability profile.

    Build once per run: the TF-IDF corpus and the company context are assembled
    up front and reused across every tender.
    """

    def __init__(self, db):
        self.db = db
        self.domains = build_domain_profiles(db)
        self.projects = build_project_profiles(db)
        self.matcher = CapabilityMatcher(self.domains, self.projects)
        self.context = self._build_context()

    def _build_context(self) -> Dict:
        from crawler.models import Association

        association_count = self.db.query(Association).count()
        has_wb = any(
            "world bank" in (p.client or "").lower()
            or "world bank" in (p.text or "").lower()
            for p in self.projects
        )
        return {
            "association_count": association_count,
            "has_world_bank_experience": has_wb,
        }

    @property
    def is_ready(self) -> bool:
        """False when company data has not been seeded, so scoring is meaningless."""
        return self.matcher.is_ready and bool(self.domains)

    def score(self, tender) -> ScoreBreakdown:
        text = tender_text(tender)
        domain_matches = self.matcher.match_domains(text)
        project_matches = self.matcher.match_projects(text)

        breakdown = ScoreBreakdown(
            domain_matches=domain_matches, project_matches=project_matches
        )

        breakdown.deadline, breakdown.days_until_deadline = score_deadline(tender)
        breakdown.capability, breakdown.matched_skills = score_capability(domain_matches)
        breakdown.sector = score_sector(tender, domain_matches, project_matches)
        breakdown.experience, breakdown.matched_projects = score_experience(
            tender, project_matches
        )
        breakdown.network = score_network(self.context, tender, project_matches)

        breakdown.overall = round(
            WEIGHTS["deadline"] * breakdown.deadline
            + WEIGHTS["capability"] * breakdown.capability
            + WEIGHTS["sector"] * breakdown.sector
            + WEIGHTS["experience"] * breakdown.experience
            + WEIGHTS["network"] * breakdown.network,
            1,
        )

        breakdown.win_probability = win_probability(breakdown, tender, self.context)
        breakdown.gaps = identify_gaps(domain_matches, breakdown)
        breakdown.risks = identify_risks(tender, breakdown, project_matches)

        from scoring.explainer import build_explanation

        breakdown.explanation = build_explanation(tender, breakdown)
        return breakdown
