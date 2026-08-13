from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import String, cast, desc, func
from sqlalchemy.orm import Session

from api.schemas import (
    CapabilityDomainOut,
    EmployeeDetailOut,
    EmployeeOut,
    EmployeeSkillOut,
    PastProjectOut,
)
from crawler.database import get_session
from crawler.models import CapabilityDomain, Employee, EmployeeSkill, PastProject, PracticeGroup

router = APIRouter()

LEVEL_ORDER = {"Expert": 0, "Proficient": 1, "Applied": 2, "Familiar": 3}


@router.get("/employees", response_model=List[EmployeeOut])
def list_employees(
    practice_group: Optional[str] = Query(None),
    search: Optional[str] = Query(None),
    db: Session = Depends(get_session),
):
    query = db.query(Employee).filter(Employee.is_active.is_(True))

    if practice_group:
        query = query.filter(Employee.practice_group.ilike(f"%{practice_group}%"))
    if search:
        pattern = f"%{search}%"
        query = query.filter(
            Employee.full_name.ilike(pattern)
            | Employee.position.ilike(pattern)
            | Employee.top_technical_skills.ilike(pattern)
        )

    employees = query.order_by(Employee.full_name).all()

    # Attach skill counts in one grouped query rather than per employee.
    counts = dict(
        db.query(EmployeeSkill.employee_id, func.count(EmployeeSkill.id))
        .group_by(EmployeeSkill.employee_id)
        .all()
    )

    results = []
    for employee in employees:
        out = EmployeeOut.model_validate(employee)
        out.skill_count = counts.get(employee.id, 0)
        results.append(out)
    return results


@router.get("/employees/{employee_id}", response_model=EmployeeDetailOut)
def get_employee(employee_id: str, db: Session = Depends(get_session)):
    employee = (
        db.query(Employee).filter(cast(Employee.id, String) == employee_id).one_or_none()
    )
    if employee is None:
        raise HTTPException(status_code=404, detail="Employee not found")

    rows = (
        db.query(EmployeeSkill, CapabilityDomain)
        .join(CapabilityDomain, EmployeeSkill.capability_domain_id == CapabilityDomain.id)
        .filter(EmployeeSkill.employee_id == employee.id)
        .all()
    )

    skills = [
        EmployeeSkillOut(
            domain=domain.name, sector=domain.sector, level=skill.competency_level
        )
        for skill, domain in rows
    ]
    skills.sort(key=lambda s: (LEVEL_ORDER.get(s.level or "", 9), s.domain))

    # Build from EmployeeOut rather than validating the ORM object directly:
    # Employee.skills is a relationship of EmployeeSkill rows, which would be
    # fed straight into the EmployeeSkillOut field and fail validation.
    return EmployeeDetailOut(
        **EmployeeOut.model_validate(employee).model_dump(),
        self_development_areas=employee.self_development_areas,
        cv_url=employee.cv_url,
        skills=skills,
    ).model_copy(update={"skill_count": len(skills)})


@router.get("/capabilities", response_model=List[CapabilityDomainOut])
def list_capabilities(
    sector: Optional[str] = Query(None), db: Session = Depends(get_session)
):
    """The capability map: every domain with the people staffing it."""
    query = db.query(CapabilityDomain)
    if sector:
        query = query.filter(CapabilityDomain.sector.ilike(f"%{sector}%"))
    domains = query.order_by(CapabilityDomain.sector, CapabilityDomain.name).all()

    rows = (
        db.query(EmployeeSkill, Employee)
        .join(Employee, EmployeeSkill.employee_id == Employee.id)
        .all()
    )
    by_domain = {}
    for skill, employee in rows:
        by_domain.setdefault(skill.capability_domain_id, []).append(
            (skill.competency_level, employee.full_name)
        )

    results = []
    for domain in domains:
        out = CapabilityDomainOut.model_validate(domain)
        for level, name in by_domain.get(domain.id, []):
            bucket = (level or "").lower()
            if bucket in ("expert", "proficient", "applied", "familiar"):
                getattr(out, bucket).append(name)
        for bucket in ("expert", "proficient", "applied", "familiar"):
            getattr(out, bucket).sort()
        results.append(out)

    return results


@router.get("/practice-groups")
def list_practice_groups(db: Session = Depends(get_session)):
    groups = db.query(PracticeGroup).order_by(PracticeGroup.name).all()
    headcount = dict(
        db.query(Employee.practice_group, func.count(Employee.id))
        .filter(Employee.is_active.is_(True))
        .group_by(Employee.practice_group)
        .all()
    )
    return [
        {
            "id": str(g.id),
            "name": g.name,
            "description": g.description,
            "headcount": headcount.get(g.name, 0),
        }
        for g in groups
    ]


@router.get("/projects", response_model=List[PastProjectOut])
def list_projects(
    practice_group: Optional[str] = Query(None),
    sector: Optional[str] = Query(None),
    db: Session = Depends(get_session),
):
    query = db.query(PastProject)
    if practice_group:
        query = query.filter(PastProject.practice_group.ilike(f"%{practice_group}%"))
    if sector:
        query = query.filter(PastProject.sector_tags.any(sector))

    return query.order_by(
        desc(PastProject.is_highlight), desc(PastProject.end_year)
    ).all()
