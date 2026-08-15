"""SQLAlchemy mappings for the tables this service touches.

The schema is **owned by the tender-intelligence app**, which defines it in
`lib/db/schema.ts` and migrates it with drizzle-kit. Nothing here creates or
alters a table; these classes exist only so the crawler and the scoring pass can
read and write rows.

Only the tables this service actually uses are mapped. Application-side tables —
users, saved searches, tender notes, project sectors — are absent on purpose: an
unused mapping is a schema claim nobody verifies, and it would drift.

Written here:  tenders, crawl_runs, crawl_errors, tender_scores
Read here:     practice_groups, capability_domains, employees, employee_skills,
               past_projects, associations
"""
import uuid
from datetime import datetime
from sqlalchemy import Column, String, Text, Integer, Float, Boolean, DateTime, Date, ForeignKey, JSON, ARRAY, DECIMAL, Index, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID, JSONB
from sqlalchemy.orm import declarative_base, relationship

Base = declarative_base()


class Tender(Base):
    __tablename__ = "tenders"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    notice_id = Column(String(50), unique=True, nullable=False, index=True)
    # Which upstream feed published this notice; see crawler/sources.py.
    source = Column(String(20), nullable=False, default="worldbank", index=True)
    notice_type = Column(String(100), index=True)
    noticedate = Column(Date, index=True)
    notice_status = Column(String(50), default="Published", index=True)
    submission_deadline = Column(DateTime(timezone=True), index=True)
    project_id = Column(String(50))
    project_name = Column(Text)
    project_country = Column(String(100), index=True)
    bid_reference_no = Column(String(100))
    bid_description = Column(Text)
    procurement_group = Column(String(10))
    procurement_method_code = Column(String(20))
    procurement_method_name = Column(String(100))
    # Derived, comma-separated list of sector labels, so it has no fixed width.
    sector = Column(Text, index=True)
    contact_organization = Column(Text)
    contact_name = Column(String(200))
    contact_email = Column(String(200))
    contact_phone = Column(String(100))
    contact_address = Column(Text)
    notice_text = Column(Text)
    # Plain-text rendering of notice_text. The API delivers HTML; the scoring
    # engine and keyword search both need it stripped.
    notice_text_clean = Column(Text)
    parsed_fields = Column(JSONB, default=dict)
    notice_url = Column(Text)
    content_hash = Column(String(64))
    # Fingerprint of the opportunity itself rather than of the notice text:
    # normalized title + country + closing date. Two rows sharing it describe
    # the same procurement, whether they came from one feed or two.
    dedup_key = Column(String(64), index=True)
    # Set on the *non*-canonical row of a duplicate group, pointing at the row
    # that represents the group. Null on canonical rows and on unique notices,
    # so `duplicate_of_id IS NULL` is the "show me each opportunity once" filter.
    # Deliberately a link and not a merge: deadlines, reference numbers and
    # submission channels differ per financier, and merging would lose exactly
    # the detail somebody needs in order to actually bid.
    duplicate_of_id = Column(
        UUID(as_uuid=True), ForeignKey("tenders.id", ondelete="SET NULL"), index=True
    )
    first_seen_at = Column(DateTime(timezone=True), default=datetime.utcnow)
    updated_at = Column(DateTime(timezone=True), default=datetime.utcnow, onupdate=datetime.utcnow)
    last_crawled_at = Column(DateTime(timezone=True), default=datetime.utcnow)
    change_flag = Column(String(20), default="new")

    score = relationship(
        "TenderScore",
        back_populates="tender",
        uselist=False,
        cascade="all, delete-orphan",
    )
    __table_args__ = (
        Index("ix_tenders_parsed_fields", "parsed_fields", postgresql_using="gin"),
        Index("ix_tenders_status_deadline", "notice_status", "submission_deadline"),
    )


class CrawlRun(Base):
    __tablename__ = "crawl_runs"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    started_at = Column(DateTime(timezone=True), nullable=False)
    ended_at = Column(DateTime(timezone=True))
    notices_found = Column(Integer, default=0)
    notices_new = Column(Integer, default=0)
    notices_updated = Column(Integer, default=0)
    errors = Column(Integer, default=0)
    status = Column(String(20), default="running")


class CrawlError(Base):
    __tablename__ = "crawl_errors"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    crawl_run_id = Column(UUID(as_uuid=True), ForeignKey("crawl_runs.id"))
    notice_id = Column(String(50))
    error_message = Column(Text)
    error_type = Column(String(100))
    occurred_at = Column(DateTime(timezone=True), default=datetime.utcnow)


class PracticeGroup(Base):
    __tablename__ = "practice_groups"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name = Column(String(200), nullable=False, unique=True)
    description = Column(Text)


class CapabilityDomain(Base):
    __tablename__ = "capability_domains"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    practice_group_id = Column(UUID(as_uuid=True), ForeignKey("practice_groups.id"))
    sector = Column(String(200))
    name = Column(String(200), nullable=False)
    keywords = Column(Text)

    skills = relationship("EmployeeSkill", back_populates="domain")

    __table_args__ = (
        UniqueConstraint("sector", "name", name="uq_capability_domain_sector_name"),
    )


class Employee(Base):
    __tablename__ = "employees"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    full_name = Column(String(200), nullable=False)
    employee_id = Column(String(50))
    employment_type = Column(String(50))
    gender = Column(String(20))
    position = Column(String(200))
    grading = Column(String(100))
    practice_group = Column(String(200))
    email = Column(String(200))
    phone = Column(String(50))
    birth_date = Column(Date)
    years_of_experience = Column(Float)
    education_level = Column(String(50))
    graduation_year = Column(Integer)
    major = Column(String(200))
    university = Column(String(200))
    education_level2 = Column(String(50))
    graduation_year2 = Column(Integer)
    major2 = Column(String(200))
    university2 = Column(String(200))
    certification = Column(Text)
    top_technical_skills = Column(Text)
    self_development_areas = Column(Text)
    cv_url = Column(Text)
    is_active = Column(Boolean, default=True)

    skills = relationship(
        "EmployeeSkill", back_populates="employee", cascade="all, delete-orphan"
    )

    __table_args__ = (
        UniqueConstraint("full_name", name="uq_employee_full_name"),
    )


class EmployeeSkill(Base):
    __tablename__ = "employee_skills"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    employee_id = Column(UUID(as_uuid=True), ForeignKey("employees.id", ondelete="CASCADE"), index=True)
    capability_domain_id = Column(UUID(as_uuid=True), ForeignKey("capability_domains.id"), index=True)
    competency_level = Column(String(20))

    employee = relationship("Employee", back_populates="skills")
    domain = relationship("CapabilityDomain", back_populates="skills")

    __table_args__ = (
        UniqueConstraint("employee_id", "capability_domain_id", name="uq_employee_skill"),
    )


class PastProject(Base):
    __tablename__ = "past_projects"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    project_name = Column(String(500), nullable=False)
    client = Column(String(500))
    practice_group = Column(String(200))
    sector_tags = Column(ARRAY(String))
    start_year = Column(Integer)
    end_year = Column(Integer)
    description = Column(Text)
    role_description = Column(Text)
    deliverables = Column(Text)
    country = Column(String(100))
    is_highlight = Column(Boolean, default=False)
    source = Column(String(50))

    __table_args__ = (
        UniqueConstraint("project_name", name="uq_past_project_name"),
    )


class Association(Base):
    __tablename__ = "associations"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name = Column(String(500), nullable=False)
    abbreviation = Column(String(100))
    category = Column(String(200))
    sub_cluster = Column(String(200))
    contact = Column(String(200))
    description = Column(Text)

    __table_args__ = (
        UniqueConstraint("name", "category", name="uq_association_name_category"),
    )


class TenderScore(Base):
    __tablename__ = "tender_scores"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tender_id = Column(UUID(as_uuid=True), ForeignKey("tenders.id", ondelete="CASCADE"), unique=True)
    overall_score = Column(Float)
    deadline_proximity_score = Column(Float)
    capability_match_score = Column(Float)
    sector_relevance_score = Column(Float)
    experience_score = Column(Float)
    network_score = Column(Float)
    win_probability = Column(Float)
    matched_skills = Column(ARRAY(String))
    matched_projects = Column(ARRAY(String))
    gaps = Column(ARRAY(String))
    risks = Column(ARRAY(String))
    explanation = Column(Text)
    scored_at = Column(DateTime(timezone=True), default=datetime.utcnow)

    # --- LLM deep assessment -------------------------------------------------
    # Populated for shortlisted tenders only. The heuristic columns above rank
    # everything cheaply; these carry Claude's reading of the notice.
    llm_overall_score = Column(Float)
    llm_capability_score = Column(Float)
    llm_sector_score = Column(Float)
    llm_experience_score = Column(Float)
    llm_win_probability = Column(Float)
    llm_confidence = Column(String(10))
    llm_recommendation = Column(String(20))
    llm_rationale = Column(Text)
    llm_scope_summary = Column(Text)
    llm_matched_capabilities = Column(ARRAY(String))
    llm_relevant_projects = Column(ARRAY(String))
    llm_key_requirements = Column(ARRAY(String))
    llm_gaps = Column(ARRAY(String))
    llm_risks = Column(ARRAY(String))
    # Practice groups Claude named for this notice, verbatim. The resolved
    # classification lives in tender_practice_groups, which the app writes;
    # this is the raw signal that pass reads.
    llm_practice_groups = Column(ARRAY(String))
    # [{"name": ..., "role": ...}] — suggested bid team from the roster.
    llm_suggested_team = Column(JSONB)
    llm_model = Column(String(60))
    llm_error = Column(Text)
    llm_scored_at = Column(DateTime(timezone=True))

    tender = relationship("Tender", back_populates="score")

    __table_args__ = (Index("ix_tender_scores_overall", "overall_score"),)