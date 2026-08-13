"""Text matching between tender notices and AMANA's capability profile.

The matcher builds a TF-IDF vector space once per scoring run from AMANA's
"corpus" -- the capability domains (enriched with the skills of the people who
hold them) and the past project descriptions -- then scores each tender's text
against it with cosine similarity.

Fitting the vectorizer on the AMANA corpus rather than on the tenders keeps the
vocabulary anchored to what the firm actually does, so an unrelated tender
scores near zero instead of being force-fit into the nearest domain.
"""
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity

# Weight of each competency level when aggregating a domain's bench strength.
COMPETENCY_WEIGHTS = {
    "Expert": 4.0,
    "Proficient": 3.0,
    "Applied": 2.0,
    "Familiar": 1.0,
}

# Domain-relevant vocabulary is short, so include bigrams and keep rare terms.
VECTORIZER_KWARGS = dict(
    stop_words="english",
    ngram_range=(1, 2),
    min_df=1,
    sublinear_tf=True,
    max_features=50_000,
)


@dataclass
class DomainProfile:
    """One capability domain plus the bench behind it."""

    domain_id: str
    name: str
    sector: Optional[str]
    text: str
    people: Dict[str, List[str]] = field(default_factory=dict)  # level -> names
    strength: float = 0.0

    @property
    def experts(self) -> List[str]:
        return self.people.get("Expert", [])

    def top_people(self, limit: int = 3) -> List[str]:
        """Names ordered by competency, strongest first."""
        ordered = []
        for level in ("Expert", "Proficient", "Applied", "Familiar"):
            ordered.extend(self.people.get(level, []))
        return ordered[:limit]


@dataclass
class ProjectProfile:
    project_id: str
    name: str
    client: Optional[str]
    text: str
    sector_tags: List[str] = field(default_factory=list)
    country: Optional[str] = None
    practice_group: Optional[str] = None
    start_year: Optional[int] = None
    end_year: Optional[int] = None


@dataclass
class DomainMatch:
    domain: DomainProfile
    similarity: float


@dataclass
class ProjectMatch:
    project: ProjectProfile
    similarity: float


class CapabilityMatcher:
    """TF-IDF matcher over AMANA's capability domains and past projects."""

    def __init__(self, domains: List[DomainProfile], projects: List[ProjectProfile]):
        self.domains = domains
        self.projects = projects

        self._corpus = [d.text for d in domains] + [p.text for p in projects]
        self._fitted = False
        self._vectorizer = None
        self._domain_matrix = None
        self._project_matrix = None

        if self._corpus and any(text.strip() for text in self._corpus):
            self._vectorizer = TfidfVectorizer(**VECTORIZER_KWARGS)
            matrix = self._vectorizer.fit_transform(self._corpus)
            split = len(domains)
            self._domain_matrix = matrix[:split]
            self._project_matrix = matrix[split:]
            self._fitted = True

    @property
    def is_ready(self) -> bool:
        return self._fitted

    def _similarities(self, text: str, matrix):
        if not self._fitted or matrix is None or matrix.shape[0] == 0:
            return []
        cleaned = (text or "").strip()
        if not cleaned:
            return []
        vector = self._vectorizer.transform([cleaned])
        # A tender sharing no vocabulary with the corpus yields an all-zero
        # vector; cosine_similarity returns zeros for it, which is what we want.
        return cosine_similarity(vector, matrix)[0]

    def match_domains(self, text: str, top_n: int = 8, threshold: float = 0.02) -> List[DomainMatch]:
        """Return the best-matching capability domains, strongest first."""
        scores = self._similarities(text, self._domain_matrix)
        matches = [
            DomainMatch(domain=self.domains[i], similarity=float(score))
            for i, score in enumerate(scores)
            if score >= threshold
        ]
        matches.sort(key=lambda m: m.similarity, reverse=True)
        return matches[:top_n]

    def match_projects(self, text: str, top_n: int = 5, threshold: float = 0.02) -> List[ProjectMatch]:
        """Return the most similar past projects, strongest first."""
        scores = self._similarities(text, self._project_matrix)
        matches = [
            ProjectMatch(project=self.projects[i], similarity=float(score))
            for i, score in enumerate(scores)
            if score >= threshold
        ]
        matches.sort(key=lambda m: m.similarity, reverse=True)
        return matches[:top_n]


def build_domain_profiles(db) -> List[DomainProfile]:
    """Assemble capability domains with the people and skills attached to them."""
    from crawler.models import CapabilityDomain, Employee, EmployeeSkill

    domains = db.query(CapabilityDomain).all()
    rows = (
        db.query(EmployeeSkill, Employee)
        .join(Employee, EmployeeSkill.employee_id == Employee.id)
        .all()
    )

    by_domain = {}
    for skill, employee in rows:
        by_domain.setdefault(skill.capability_domain_id, []).append((skill, employee))

    profiles = []
    for domain in domains:
        members = by_domain.get(domain.id, [])

        people = {}
        strength = 0.0
        skill_text = []
        for skill, employee in members:
            level = skill.competency_level or "Familiar"
            people.setdefault(level, []).append(employee.full_name)
            strength += COMPETENCY_WEIGHTS.get(level, 1.0)
            # Fold each person's declared expertise into the domain's vocabulary,
            # weighted by competency: an Expert's wording counts more.
            if employee.top_technical_skills:
                repeats = 2 if level in ("Expert", "Proficient") else 1
                skill_text.extend([employee.top_technical_skills] * repeats)
            if employee.major and level in ("Expert", "Proficient"):
                skill_text.append(employee.major)

        # Repeat the domain name so it dominates the vector over incidental
        # vocabulary from CV free-text.
        text_parts = [domain.name] * 3
        if domain.sector:
            text_parts.append(domain.sector)
        if domain.keywords:
            text_parts.append(domain.keywords)
        text_parts.extend(skill_text)

        profiles.append(
            DomainProfile(
                domain_id=str(domain.id),
                name=domain.name,
                sector=domain.sector,
                text=" ".join(text_parts),
                people=people,
                strength=strength,
            )
        )

    return profiles


def build_project_profiles(db) -> List[ProjectProfile]:
    """Assemble past projects into searchable documents."""
    from crawler.models import PastProject

    profiles = []
    for project in db.query(PastProject).all():
        text_parts = [project.project_name] * 2
        for value in (
            project.description,
            project.role_description,
            project.deliverables,
            project.practice_group,
            project.client,
        ):
            if value:
                text_parts.append(value)
        if project.sector_tags:
            text_parts.extend(project.sector_tags)

        profiles.append(
            ProjectProfile(
                project_id=str(project.id),
                name=project.project_name,
                client=project.client,
                text=" ".join(text_parts),
                sector_tags=list(project.sector_tags or []),
                country=project.country,
                practice_group=project.practice_group,
                start_year=project.start_year,
                end_year=project.end_year,
            )
        )

    return profiles


def tender_text(tender) -> str:
    """Build the document we match a tender on.

    The bid description and project name are the most signal-dense fields, so
    they are repeated to outweigh the boilerplate that dominates notice bodies
    (eligibility clauses, submission instructions, standard World Bank language).
    """
    parts = []
    if tender.bid_description:
        parts.extend([tender.bid_description] * 3)
    if tender.project_name:
        parts.extend([tender.project_name] * 2)
    if tender.sector:
        parts.append(tender.sector)
    if tender.notice_text_clean:
        # Cap the body: past a few thousand characters it is almost entirely
        # standard clauses that dilute the vector.
        parts.append(tender.notice_text_clean[:4000])
    return " ".join(parts)
