"""LLM-based deep assessment of tender fit, using Claude.

The heuristic engine (``engine.py``) scores every open tender cheaply with
TF-IDF. It is good at ranking but blind to meaning: it cannot tell that a notice
asking for "a firm to design a national health data interoperability roadmap" is
squarely AMANA's Digital Health work, nor that "supply of laboratory reagents"
is not, however many health keywords it contains.

This module is the second stage. The heuristic shortlists; Claude reads the
shortlisted notices properly and returns a structured assessment -- fit scores
with reasoning, the specific people who should staff the bid, the requirements
that must be met, and the gaps and risks worth knowing before committing.

Cost control rests on two things:

* **Prompt caching.** AMANA's capability profile is large and identical on every
  request, so it is sent as a cached system prompt. The first call writes the
  cache at 1.25x; every later call reads it at 0.1x. Claude Haiku 4.5 needs a
  4096-token prefix before caching engages, which the profile comfortably clears.
* **Shortlisting.** Only the top-ranked notices are sent, not all of them.
"""
import json
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional

import anthropic

from scoring.matcher import COMPETENCY_WEIGHTS

DEFAULT_MODEL = "claude-haiku-4-5"

# Claude Haiku 4.5 caches prefixes of 4096 tokens or more. Below that the
# cache_control marker is silently ignored -- no error, just no cache.
MIN_CACHEABLE_TOKENS = 4096

# The assessment schema. Note the JSON-schema subset the API accepts: no
# minimum/maximum, no minLength/maxLength, and every object needs
# additionalProperties: false.
ASSESSMENT_SCHEMA = {
    "type": "object",
    "properties": {
        "overall_fit": {
            "type": "integer",
            "description": "0-100. How well this opportunity fits AMANA overall.",
        },
        "capability_fit": {
            "type": "integer",
            "description": "0-100. Does AMANA have the skills and the bench to deliver this scope?",
        },
        "sector_fit": {
            "type": "integer",
            "description": "0-100. Is this sector and client type one AMANA works in?",
        },
        "experience_fit": {
            "type": "integer",
            "description": "0-100. Can AMANA evidence comparable delivered work?",
        },
        "win_probability": {
            "type": "integer",
            "description": "0-100. Realistic chance of winning if AMANA bids, given fit and competition.",
        },
        "confidence": {
            "type": "string",
            "enum": ["low", "medium", "high"],
            "description": "Your confidence in this assessment. Use 'low' when the notice text is thin or ambiguous.",
        },
        "recommendation": {
            "type": "string",
            "enum": ["pursue", "review", "decline"],
            "description": "pursue = strong fit, bid it. review = plausible but needs a judgement call. decline = outside AMANA's offering.",
        },
        "rationale": {
            "type": "string",
            "description": "2-4 sentences explaining the verdict in plain language a bid manager can act on.",
        },
        "scope_summary": {
            "type": "string",
            "description": "One sentence describing what the client actually wants delivered.",
        },
        "matched_capabilities": {
            "type": "array",
            "description": "AMANA capability domains this scope draws on. Use exact domain names from the profile.",
            "items": {"type": "string"},
        },
        "practice_groups": {
            "type": "array",
            "description": (
                "Which of AMANA's client-facing practice groups this opportunity belongs to, "
                "most relevant first. Choose only from: Strategy and Transformation, Digital, "
                "Health, Education. Name a second or third group only where the scope genuinely "
                "draws on it -- a national health data platform is Health and Digital both. "
                "Return an empty array when the scope sits outside all four."
            ),
            "items": {
                "type": "string",
                "enum": ["Strategy and Transformation", "Digital", "Health", "Education"],
            },
        },
        "suggested_team": {
            "type": "array",
            "description": "Named people from the roster who should staff this bid, each with their role. Use exact names from the profile.",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "role": {"type": "string", "description": "What they would do on this engagement."},
                },
                "required": ["name", "role"],
                "additionalProperties": False,
            },
        },
        "relevant_projects": {
            "type": "array",
            "description": "Past AMANA projects worth citing in the proposal. Use exact project names from the profile.",
            "items": {"type": "string"},
        },
        "key_requirements": {
            "type": "array",
            "description": "Concrete requirements the notice imposes (qualifications, experience, deliverables, timelines).",
            "items": {"type": "string"},
        },
        "gaps": {
            "type": "array",
            "description": "Capabilities or credentials this scope needs that AMANA lacks or is thin on.",
            "items": {"type": "string"},
        },
        "risks": {
            "type": "array",
            "description": "Practical reasons this bid could fail or cost more than it returns.",
            "items": {"type": "string"},
        },
    },
    "required": [
        "overall_fit",
        "capability_fit",
        "sector_fit",
        "experience_fit",
        "win_probability",
        "confidence",
        "recommendation",
        "rationale",
        "scope_summary",
        "matched_capabilities",
        "practice_groups",
        "suggested_team",
        "relevant_projects",
        "key_requirements",
        "gaps",
        "risks",
    ],
    "additionalProperties": False,
}

SYSTEM_INSTRUCTIONS = """You are a bid qualification analyst at AMANA Solutions, an Indonesian
public-sector advisory firm. You assess World Bank procurement notices and decide whether AMANA
should bid.

Be useful and honest rather than encouraging: a notice outside AMANA's offering should be scored
low and declined plainly. Equally, do not mark down a genuinely good fit out of caution -- a bid
team relies on these scores to find the opportunities worth their time, and a scale where nothing
ever scores well is no more useful than one where everything does.

Score `overall_fit` against these anchors, and use the full range:

- 80-100  Core AMANA work. The scope is something the firm has delivered before, the bench covers
          it at Expert level, and there is a clear path to a competitive proposal.
- 60-79   Strong fit. Squarely advisory work in a practice area AMANA owns, with the people to
          staff it. Some gaps, none disqualifying.
- 40-59   Plausible but qualified. Real overlap with AMANA's offering, alongside a material gap:
          a thin bench in the core domain, an unfamiliar country, or a credential AMANA lacks.
- 20-39   Weak. Advisory in form, but the substance sits outside AMANA's practice areas, or the
          mandatory qualifications rule the firm out.
- 0-19    Outside the offering entirely. Goods, works, clinical services, or a technical trade
          AMANA does not sell.

Map the recommendation from the score: `pursue` at 60 and above, `review` from 40 to 59, `decline`
below 40. Keep the two consistent.

How to judge:

- Read what the client actually wants delivered, not the keywords in the text. A notice mentioning
  "health" that is buying laboratory equipment is not a health advisory opportunity.
- AMANA sells advisory, strategy, policy, research, programme management and digital transformation
  services. It does not supply goods, build infrastructure, or provide clinical staff.
- Weigh the bench, not just the domain. A domain with several Expert-level people is a real
  strength; one with a single Familiar-level person is not.
- Individual-consultant notices are usually a weaker fit than firm-level assignments, because AMANA
  bids as a firm -- but they are not automatically a decline.
- Indonesia and Southeast Asia are AMANA's home ground. Work elsewhere is possible but carries
  delivery risk worth flagging.
- Only name people and projects that appear in the profile, spelled exactly as written there. Never
  invent a name, a project, or a credential.
- Assign practice groups on what the client wants delivered, not on the sector the project sits in.
  A road-safety programme is not Health because it reduces injuries; a hospital construction
  supervision role is not Health advisory. Where the scope genuinely spans two groups, name both.
  Operations is AMANA's internal function and is never an option here.
- When the notice text is thin or ambiguous, say so through the confidence field rather than
  guessing.

Score against the anchors above, not relative to the other notices you have seen."""


@dataclass
class LLMAssessment:
    """A parsed assessment plus the usage metadata from the call that produced it."""

    tender_id: str
    data: Dict
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    error: Optional[str] = None

    @property
    def ok(self) -> bool:
        return self.error is None


def build_company_profile(db) -> str:
    """Render AMANA's capability profile as the cached system context.

    This text is identical on every request in a run, which is what makes it
    worth caching. Keep it stable: any change invalidates the cache for the whole
    batch, so avoid interpolating timestamps or per-tender values.
    """
    from crawler.models import (
        Association,
        CapabilityDomain,
        Employee,
        EmployeeSkill,
        PastProject,
        PracticeGroup,
    )

    lines: List[str] = ["# AMANA Solutions — capability profile", ""]

    groups = db.query(PracticeGroup).order_by(PracticeGroup.name).all()
    if groups:
        lines.append("## Practice groups")
        for group in groups:
            lines.append(f"- **{group.name}**: {group.description or ''}".rstrip())
        lines.append("")

    # Capability domains, grouped by practice sector, each listing the people who
    # hold it and at what level. This is the core of the profile.
    domains = (
        db.query(CapabilityDomain)
        .order_by(CapabilityDomain.sector, CapabilityDomain.name)
        .all()
    )
    rows = (
        db.query(EmployeeSkill, Employee)
        .join(Employee, EmployeeSkill.employee_id == Employee.id)
        .all()
    )
    by_domain: Dict = {}
    for skill, employee in rows:
        by_domain.setdefault(skill.capability_domain_id, []).append(
            (skill.competency_level or "Familiar", employee.full_name)
        )

    lines.append("## Capability map")
    lines.append(
        "Competency levels, strongest first: Expert, Proficient, Applied, Familiar."
    )
    lines.append("")

    current_sector = None
    for domain in domains:
        if domain.sector != current_sector:
            current_sector = domain.sector
            lines.append(f"### {current_sector}")
        people = by_domain.get(domain.id, [])
        if not people:
            lines.append(f"- **{domain.name}** — nobody currently mapped")
            continue
        buckets: Dict[str, List[str]] = {}
        for level, name in people:
            buckets.setdefault(level, []).append(name)
        parts = [
            f"{level}: {', '.join(sorted(buckets[level]))}"
            for level in ("Expert", "Proficient", "Applied", "Familiar")
            if buckets.get(level)
        ]
        lines.append(f"- **{domain.name}** — " + "; ".join(parts))
    lines.append("")

    # Senior people with their declared strengths, so the model can staff a bid.
    lines.append("## Senior people")
    seniors = (
        db.query(Employee)
        .filter(Employee.is_active.is_(True))
        .order_by(Employee.full_name)
        .all()
    )
    for employee in seniors:
        if not (employee.position or employee.top_technical_skills):
            continue
        detail = [employee.position or ""]
        if employee.practice_group:
            detail.append(employee.practice_group)
        if employee.years_of_experience:
            detail.append(f"{employee.years_of_experience:g} yrs")
        header = f"- **{employee.full_name}** ({', '.join(p for p in detail if p)})"
        lines.append(header)
        if employee.top_technical_skills:
            skills = " ".join(employee.top_technical_skills.split())
            lines.append(f"  - Strengths: {skills[:600]}")
        if employee.certification:
            certs = " ".join(employee.certification.split())
            lines.append(f"  - Certifications: {certs[:300]}")
    lines.append("")

    lines.append("## Delivered projects")
    projects = (
        db.query(PastProject)
        .order_by(PastProject.is_highlight.desc(), PastProject.end_year.desc())
        .all()
    )
    for project in projects:
        years = ""
        if project.start_year:
            years = f" ({project.start_year}"
            if project.end_year and project.end_year != project.start_year:
                years += f"–{project.end_year}"
            years += ")"
        lines.append(f"### {project.project_name}{years}")
        if project.client:
            lines.append(f"- Client: {project.client}")
        if project.practice_group:
            lines.append(f"- Practice group: {project.practice_group}")
        if project.country:
            lines.append(f"- Country: {project.country}")
        if project.sector_tags:
            lines.append(f"- Tags: {', '.join(project.sector_tags)}")
        if project.description:
            lines.append(f"- {project.description}")
        lines.append("")

    categories = {}
    for association in db.query(Association).all():
        categories[association.category] = categories.get(association.category, 0) + 1
    if categories:
        lines.append("## Network")
        summary = ", ".join(f"{name} ({count})" for name, count in sorted(categories.items()))
        lines.append(
            f"AMANA holds relationships across {sum(categories.values())} Indonesian "
            f"industry associations: {summary}."
        )
        lines.append("")

    return "\n".join(lines)


def build_tender_prompt(tender, heuristic=None) -> str:
    """Render one tender as the per-request user message."""
    parts = [
        "Assess this World Bank procurement notice for AMANA.",
        "",
        "## Notice",
        f"- Reference: {tender.notice_id}",
        f"- Type: {tender.notice_type or 'unknown'}",
        f"- Country: {tender.project_country or 'unspecified'}",
        f"- Project: {tender.project_name or 'unspecified'}",
        f"- Title: {tender.bid_description or 'unspecified'}",
        f"- Procurement category: {tender.procurement_method_name or tender.procurement_group or 'unspecified'}",
        f"- Client organisation: {tender.contact_organization or 'unspecified'}",
        f"- Detected sectors: {tender.sector or 'none detected'}",
        f"- Submission deadline: {tender.submission_deadline.isoformat() if tender.submission_deadline else 'not published'}",
    ]

    parsed = tender.parsed_fields or {}
    quals = parsed.get("qualifications") or {}
    if quals:
        parts.append(f"- Extracted qualification hints: {json.dumps(quals)}")
    if parsed.get("budget_mentions"):
        parts.append(f"- Budget mentions: {', '.join(parsed['budget_mentions'][:3])}")
    if parsed.get("duration_mentions"):
        parts.append(f"- Duration mentions: {', '.join(parsed['duration_mentions'][:3])}")

    if heuristic is not None:
        # Give the model the cheap signal as context, explicitly labelled as a
        # prior it may overrule -- otherwise it tends to anchor on the number.
        parts += [
            "",
            "## Prior from the keyword-matching prefilter",
            "This is a crude TF-IDF signal, not a judgement. Overrule it freely.",
            f"- Heuristic overall: {heuristic.get('overall')}",
            f"- Heuristic capability: {heuristic.get('capability')}",
            f"- Keyword-matched domains: {', '.join(heuristic.get('skills') or []) or 'none'}",
        ]

    body = tender.notice_text_clean or ""
    parts += [
        "",
        "## Notice text",
        body[:12000] if body else "(no notice body published)",
    ]
    return "\n".join(parts)


class LLMScorer:
    """Scores tenders with Claude, reusing one cached company profile per run."""

    def __init__(self, db, model: Optional[str] = None, max_tokens: int = 2000):
        self.db = db
        self.model = model or os.getenv("ANTHROPIC_MODEL") or DEFAULT_MODEL
        self.max_tokens = max_tokens
        self.client = anthropic.Anthropic()
        self.profile = build_company_profile(db)
        self._lock = threading.Lock()
        self.stats = {
            "calls": 0,
            "errors": 0,
            "input_tokens": 0,
            "output_tokens": 0,
            "cache_read_tokens": 0,
            "cache_write_tokens": 0,
        }

    def profile_tokens(self) -> int:
        """Token count of the cached profile, to confirm caching will engage."""
        response = self.client.messages.count_tokens(
            model=self.model,
            system=self._system_blocks(),
            messages=[{"role": "user", "content": "x"}],
        )
        return response.input_tokens

    def _system_blocks(self) -> List[Dict]:
        # Two blocks, cache marker on the last one: everything above it (both the
        # instructions and the profile) is cached as a single prefix.
        return [
            {"type": "text", "text": SYSTEM_INSTRUCTIONS},
            {
                "type": "text",
                "text": self.profile,
                "cache_control": {"type": "ephemeral"},
            },
        ]

    def assess(self, tender, heuristic: Optional[Dict] = None) -> LLMAssessment:
        """Assess one tender. Never raises -- failures come back on `.error`."""
        try:
            response = self.client.messages.create(
                model=self.model,
                max_tokens=self.max_tokens,
                system=self._system_blocks(),
                output_config={
                    "format": {"type": "json_schema", "schema": ASSESSMENT_SCHEMA}
                },
                messages=[
                    {"role": "user", "content": build_tender_prompt(tender, heuristic)}
                ],
            )
        except anthropic.APIError as exc:
            with self._lock:
                self.stats["errors"] += 1
            return LLMAssessment(
                tender_id=str(tender.id), data={}, model=self.model, error=str(exc)[:500]
            )

        usage = response.usage
        with self._lock:
            self.stats["calls"] += 1
            self.stats["input_tokens"] += usage.input_tokens or 0
            self.stats["output_tokens"] += usage.output_tokens or 0
            self.stats["cache_read_tokens"] += getattr(usage, "cache_read_input_tokens", 0) or 0
            self.stats["cache_write_tokens"] += getattr(usage, "cache_creation_input_tokens", 0) or 0

        if response.stop_reason == "refusal":
            with self._lock:
                self.stats["errors"] += 1
            return LLMAssessment(
                tender_id=str(tender.id),
                data={},
                model=self.model,
                error="Model declined to assess this notice.",
            )

        text = next((b.text for b in response.content if b.type == "text"), "")
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            # output_config guarantees schema-valid JSON, so this should only
            # happen if the response was truncated at max_tokens.
            with self._lock:
                self.stats["errors"] += 1
            hint = " (response hit max_tokens)" if response.stop_reason == "max_tokens" else ""
            return LLMAssessment(
                tender_id=str(tender.id),
                data={},
                model=self.model,
                error=f"Could not parse assessment{hint}: {exc}",
            )

        return LLMAssessment(
            tender_id=str(tender.id),
            data=data,
            model=self.model,
            input_tokens=usage.input_tokens or 0,
            output_tokens=usage.output_tokens or 0,
            cache_read_tokens=getattr(usage, "cache_read_input_tokens", 0) or 0,
            cache_write_tokens=getattr(usage, "cache_creation_input_tokens", 0) or 0,
        )

    def assess_many(
        self, items: List[tuple], concurrency: int = 4, on_result=None
    ) -> List[LLMAssessment]:
        """Assess several tenders concurrently.

        ``items`` is a list of ``(tender, heuristic_dict)`` pairs. The first call
        is made alone so it writes the prompt cache; the rest then run in
        parallel and read it. Firing everything at once would have every request
        miss the cache, since none can read what the others are still writing.
        """
        if not items:
            return []

        results = [self.assess(items[0][0], items[0][1])]
        if on_result:
            on_result(results[0])

        remaining = items[1:]
        if remaining:
            with ThreadPoolExecutor(max_workers=max(1, concurrency)) as pool:
                futures = [
                    pool.submit(self.assess, tender, heuristic)
                    for tender, heuristic in remaining
                ]
                for future in futures:
                    result = future.result()
                    results.append(result)
                    if on_result:
                        on_result(result)

        return results

    def estimated_cost_usd(self) -> float:
        """Rough spend for this run, at Claude Haiku 4.5 list rates."""
        if "haiku" not in self.model:
            return 0.0
        stats = self.stats
        return (
            stats["input_tokens"] / 1_000_000 * 1.00
            + stats["cache_write_tokens"] / 1_000_000 * 1.25
            + stats["cache_read_tokens"] / 1_000_000 * 0.10
            + stats["output_tokens"] / 1_000_000 * 5.00
        )
