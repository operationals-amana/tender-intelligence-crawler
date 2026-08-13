from pydantic import BaseModel, ConfigDict
from typing import Optional, List, Dict, Any
from datetime import datetime, date
from uuid import UUID

# NOTE: UUID/datetime columns must be declared with their real types. Declaring
# them as `str` makes Pydantic v2 reject the ORM objects outright (it does not
# coerce UUID -> str), which turns every response into a 500.


class TenderOut(BaseModel):
    id: UUID
    notice_id: str
    notice_type: Optional[str] = None
    noticedate: Optional[date] = None
    notice_status: Optional[str] = None
    submission_deadline: Optional[datetime] = None
    project_id: Optional[str] = None
    project_name: Optional[str] = None
    project_country: Optional[str] = None
    bid_reference_no: Optional[str] = None
    bid_description: Optional[str] = None
    procurement_group: Optional[str] = None
    procurement_method_code: Optional[str] = None
    procurement_method_name: Optional[str] = None
    sector: Optional[str] = None
    contact_organization: Optional[str] = None
    contact_name: Optional[str] = None
    contact_email: Optional[str] = None
    contact_phone: Optional[str] = None
    contact_address: Optional[str] = None
    notice_url: Optional[str] = None
    parsed_fields: Optional[Dict[str, Any]] = None
    first_seen_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    change_flag: Optional[str] = None

    model_config = ConfigDict(from_attributes=True)


class TenderDetailOut(TenderOut):
    """Tender detail additionally carries the full notice body and its score."""

    notice_text: Optional[str] = None
    score: Optional["TenderScoreOut"] = None
    notes: List["TenderNoteOut"] = []


class TenderScoreOut(BaseModel):
    id: UUID
    tender_id: UUID
    overall_score: Optional[float] = None
    deadline_proximity_score: Optional[float] = None
    capability_match_score: Optional[float] = None
    sector_relevance_score: Optional[float] = None
    experience_score: Optional[float] = None
    network_score: Optional[float] = None
    win_probability: Optional[float] = None
    matched_skills: Optional[List[str]] = None
    matched_projects: Optional[List[str]] = None
    gaps: Optional[List[str]] = None
    risks: Optional[List[str]] = None
    explanation: Optional[str] = None
    scored_at: Optional[datetime] = None

    # Claude's deep assessment. Present only for shortlisted tenders.
    llm_overall_score: Optional[float] = None
    llm_capability_score: Optional[float] = None
    llm_sector_score: Optional[float] = None
    llm_experience_score: Optional[float] = None
    llm_win_probability: Optional[float] = None
    llm_confidence: Optional[str] = None
    llm_recommendation: Optional[str] = None
    llm_rationale: Optional[str] = None
    llm_scope_summary: Optional[str] = None
    llm_matched_capabilities: Optional[List[str]] = None
    llm_relevant_projects: Optional[List[str]] = None
    llm_key_requirements: Optional[List[str]] = None
    llm_gaps: Optional[List[str]] = None
    llm_risks: Optional[List[str]] = None
    llm_suggested_team: Optional[List[Dict[str, Any]]] = None
    llm_model: Optional[str] = None
    llm_scored_at: Optional[datetime] = None

    model_config = ConfigDict(from_attributes=True)


class RecommendationOut(BaseModel):
    """A scored tender joined with the tender fields the cards need."""

    tender: TenderOut
    score: TenderScoreOut

    model_config = ConfigDict(from_attributes=True)


class DashboardStatsOut(BaseModel):
    total_active_tenders: int = 0
    deadlines_this_week: int = 0
    deadlines_next_30_days: int = 0
    average_score: float = 0
    scored_tenders: int = 0
    assessed_tenders: int = 0
    pursue_count: int = 0
    last_crawl: Optional[datetime] = None
    last_crawl_status: Optional[str] = None
    top_opportunities: List[RecommendationOut] = []
    deadline_histogram: List[Dict[str, Any]] = []
    top_countries: List[Dict[str, Any]] = []


class EmployeeSkillOut(BaseModel):
    domain: str
    sector: Optional[str] = None
    level: Optional[str] = None


class EmployeeOut(BaseModel):
    id: UUID
    full_name: str
    employee_id: Optional[str] = None
    position: Optional[str] = None
    grading: Optional[str] = None
    practice_group: Optional[str] = None
    email: Optional[str] = None
    years_of_experience: Optional[float] = None
    education_level: Optional[str] = None
    major: Optional[str] = None
    university: Optional[str] = None
    education_level2: Optional[str] = None
    major2: Optional[str] = None
    university2: Optional[str] = None
    certification: Optional[str] = None
    top_technical_skills: Optional[str] = None
    skill_count: int = 0

    model_config = ConfigDict(from_attributes=True)


class EmployeeDetailOut(EmployeeOut):
    self_development_areas: Optional[str] = None
    cv_url: Optional[str] = None
    skills: List[EmployeeSkillOut] = []


class PastProjectOut(BaseModel):
    id: UUID
    project_name: str
    client: Optional[str] = None
    practice_group: Optional[str] = None
    sector_tags: Optional[List[str]] = None
    start_year: Optional[int] = None
    end_year: Optional[int] = None
    description: Optional[str] = None
    role_description: Optional[str] = None
    deliverables: Optional[str] = None
    country: Optional[str] = None
    is_highlight: Optional[bool] = False

    model_config = ConfigDict(from_attributes=True)


class CapabilityDomainOut(BaseModel):
    id: UUID
    name: str
    sector: Optional[str] = None
    expert: List[str] = []
    proficient: List[str] = []
    applied: List[str] = []
    familiar: List[str] = []

    model_config = ConfigDict(from_attributes=True)


class AssociationOut(BaseModel):
    id: UUID
    name: str
    abbreviation: Optional[str] = None
    category: Optional[str] = None
    sub_cluster: Optional[str] = None
    contact: Optional[str] = None
    description: Optional[str] = None

    model_config = ConfigDict(from_attributes=True)


class CrawlRunOut(BaseModel):
    id: UUID
    started_at: Optional[datetime] = None
    ended_at: Optional[datetime] = None
    notices_found: int = 0
    notices_new: int = 0
    notices_updated: int = 0
    errors: int = 0
    status: str = ""

    model_config = ConfigDict(from_attributes=True)


class TenderNoteOut(BaseModel):
    id: UUID
    note: str
    created_by: Optional[str] = None
    created_at: Optional[datetime] = None

    model_config = ConfigDict(from_attributes=True)


class TenderNoteCreate(BaseModel):
    note: str
    created_by: str = "anonymous"


class SavedSearchOut(BaseModel):
    id: UUID
    name: Optional[str] = None
    filters: Dict[str, Any] = {}
    created_at: Optional[datetime] = None

    model_config = ConfigDict(from_attributes=True)


class SavedSearchCreate(BaseModel):
    name: str
    filters: Dict[str, Any] = {}


class TenderListOut(BaseModel):
    total: int
    limit: int
    offset: int
    items: List[TenderOut]


class FilterOptionsOut(BaseModel):
    countries: List[str] = []
    notice_types: List[str] = []
    sectors: List[str] = []
    procurement_groups: List[str] = []


TenderDetailOut.model_rebuild()


# ---------------------------------------------------------------- auth


class UserOut(BaseModel):
    id: UUID
    email: str
    full_name: Optional[str] = None
    role: Optional[str] = None
    last_login_at: Optional[datetime] = None

    model_config = ConfigDict(from_attributes=True)


class LoginRequest(BaseModel):
    email: str
    password: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int
    user: UserOut
