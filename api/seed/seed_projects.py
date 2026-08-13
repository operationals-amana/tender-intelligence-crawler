"""Seed past projects extracted from the Company Profile and Portfolio PDFs."""
import os
from crawler.models import PastProject, ProjectSector


def seed_projects(db):
    # Projects extracted from the Company Profile and Portfolio PDFs
    projects = [
        {
            "project_name": "Artificial Intelligence Strategic Policy Dialogues and Policy Recommendation",
            "client": "Think Policy, British Embassy Jakarta, Ministry of Communications and Digital Affairs",
            "practice_group": "Strategy and Transformation",
            "sector_tags": ["AI", "PublicPolicy", "DigitalReform"],
            "start_year": 2025,
            "end_year": 2025,
            "description": "Developed AI policy recommendations including foundational principles, governance mechanisms, and institutional roles aligned with Indonesia's digital transformation agenda.",
            "source": "company_profile",
            "is_highlight": True,
        },
        {
            "project_name": "Indonesia Digital Government Transformation Support",
            "client": "PROSPERA, Ministry of Administrative and Bureaucratic Reform, Ministry of Communications and Digital Affairs",
            "practice_group": "Strategy and Transformation",
            "sector_tags": ["DigitalGovernment", "PublicSectorReform", "PMO"],
            "start_year": 2025,
            "end_year": 2026,
            "description": "Established a PMO to support digital government transformation, aligning ministries, tracking progress, and developing strategic recommendations for digital government governance.",
            "source": "company_profile",
            "is_highlight": True,
        },
        {
            "project_name": "Development of the Strategic Plan of the Ministry of Health 2025-2029",
            "client": "Planning and Budgeting Bureau, Ministry of Health",
            "practice_group": "Health and Wellbeing",
            "sector_tags": ["PublicHealth", "HealthSystem", "StrategicPlan"],
            "start_year": 2025,
            "end_year": 2025,
            "description": "Provided technical assistance for developing the Ministry of Health's five-year strategic plan, including situation analysis, core narratives, M&E matrices, and budgeting framework.",
            "source": "company_profile",
            "is_highlight": True,
        },
        {
            "project_name": "Development of Indonesia Digital Health Transformation Strategy (DHTS) 2025-2029",
            "client": "USAID CHISU, British Embassy Jakarta, Ministry of Health",
            "practice_group": "Health and Wellbeing",
            "sector_tags": ["DigitalHealth", "HealthSystem", "DigitalTransformation"],
            "start_year": 2025,
            "end_year": 2025,
            "description": "Facilitated cross-sectoral stakeholder engagement, insight gathering, situation analysis, and strategic direction development for digital health transformation.",
            "source": "company_profile",
            "is_highlight": True,
        },
        {
            "project_name": "Implementation of Regulatory Sandbox for Health Technology Innovations",
            "client": "Think Policy, Instellar, British Embassy Jakarta, Ministry of Health",
            "practice_group": "Health and Wellbeing",
            "sector_tags": ["DigitalHealth", "RegulatorySandbox", "HealthPolicy"],
            "start_year": 2023,
            "end_year": 2025,
            "description": "Assisted Ministry of Health in implementing a regulatory sandbox for health tech innovations. Successfully adopted as Ministry of Health program and incorporated into Minister of Health Regulation.",
            "source": "company_profile",
            "is_highlight": True,
        },
        {
            "project_name": "Development of National Strategy for Availability and Access to Innovative Medicines and Vaccines",
            "client": "International Pharmaceutical Manufacturing Group, Ministry of Health",
            "practice_group": "Health and Wellbeing",
            "sector_tags": ["HealthSystem", "Pharmaceutical", "NationalStrategy"],
            "start_year": 2025,
            "end_year": 2025,
            "description": "Developed National Strategy to improve availability, access, and affordability of innovative medicines, addressing regulatory hurdles, affordability, distribution, and R&D challenges.",
            "source": "company_profile",
            "is_highlight": True,
        },
        {
            "project_name": "Organizational Strengthening of Digital Transformation Office (DTO) of Ministry of Health",
            "client": "Think Policy, British Embassy Jakarta, Ministry of Health",
            "practice_group": "Health and Wellbeing",
            "sector_tags": ["DigitalHealth", "DigitalTransformation", "OrganizationalDesign"],
            "start_year": 2025,
            "end_year": 2025,
            "description": "Developed strategic recommendation for new agile DTO organizational structure, defining key functions, roles, and coordination mechanisms for digital health governance.",
            "source": "company_profile",
        },
        {
            "project_name": "13-Year Compulsory Education Program Advisory and Management",
            "client": "Ministry of Primary and Secondary Education",
            "practice_group": "Education",
            "sector_tags": ["Education", "CompulsoryEducation", "ProgramManagement"],
            "start_year": 2025,
            "end_year": 2025,
            "description": "Provided project management for Indonesia's 13-Year Compulsory Education program, coordinating policies, resources, and implementation strategies across ministries and stakeholders.",
            "source": "company_profile",
            "is_highlight": True,
        },
        {
            "project_name": "Co-Creating Indonesia's AI Future through Meaningful Policy Dialogues",
            "client": "Ministry of Communications and Digital Affairs",
            "practice_group": "Strategy and Transformation",
            "sector_tags": ["AI", "PublicPolicy", "StakeholderEngagement"],
            "start_year": 2025,
            "end_year": 2025,
            "description": "Facilitated structured dialogues with government, private sector, academia, and civil society across six sectors to identify AI policy gaps and gather practical insights.",
            "source": "portfolio",
        },
        {
            "project_name": "Data Law Reform Advisory",
            "client": "World Bank, UMBRA",
            "practice_group": "Strategy and Transformation",
            "sector_tags": ["DataGovernance", "DataLaw", "PublicPolicy", "WorldBank"],
            "start_year": 2025,
            "end_year": 2025,
            "description": "Structured key policy issues, formulated scope of the Satu Data Indonesia Bill, and contributed comparative lessons to strengthen the academic paper and legislation.",
            "source": "portfolio",
            "is_highlight": True,
        },
        {
            "project_name": "Communication Strategy for Competition Law Reform",
            "client": "KPPU, Prospera",
            "practice_group": "Strategy and Transformation",
            "sector_tags": ["CompetitionPolicy", "StakeholderEngagement", "PublicCommunication"],
            "start_year": 2025,
            "end_year": 2025,
            "description": "Designed stakeholder engagement strategy and multi-channel communication strategy for competition law reform, including workshops, seminars, and digital outreach.",
            "source": "portfolio",
        },
        {
            "project_name": "EITI SOE Transparency Reporting",
            "client": "EITI Indonesia",
            "practice_group": "Strategy and Transformation",
            "sector_tags": ["Transparency", "ExtractiveIndustries", "Governance"],
            "start_year": 2025,
            "end_year": 2025,
            "description": "Conducted diagnostic of SOE transparency practices, developed standardized reporting template aligned with EITI Standard, and facilitated capacity building workshops.",
            "source": "portfolio",
        },
        {
            "project_name": "EITI Kazakhstan and Tajikistan Knowledge Sharing",
            "client": "EITI Kazakhstan, EITI Tajikistan",
            "practice_group": "Strategy and Transformation",
            "sector_tags": ["Transparency", "ExtractiveIndustries", "Governance", "International"],
            "start_year": 2026,
            "end_year": 2026,
            "description": "Supported Kazakhstan and Tajikistan's EITI reform through knowledge sharing workshops and strategy development based on Indonesia's experience.",
            "source": "portfolio",
            "country": "Kazakhstan, Tajikistan",
        },
        {
            "project_name": "SIKD Blueprint and Digitalization Roadmap",
            "client": "Ministry of Finance",
            "practice_group": "Digital",
            "sector_tags": ["PublicFinance", "DigitalGovernment", "FiscalPolicy"],
            "start_year": 2025,
            "end_year": 2025,
            "description": "Developed blueprint for next-generation Regional Financial Information System (SIKD) including system assessment, gap analysis, use case design, and medium-term roadmap.",
            "source": "portfolio",
        },
        {
            "project_name": "AI Innovation Sandbox for Public Sector",
            "client": "Multiple government agencies",
            "practice_group": "Digital",
            "sector_tags": ["AI", "GovTech", "PublicSector", "Innovation"],
            "start_year": 2025,
            "end_year": 2026,
            "description": "Leading design and implementation of AI Innovation Sandbox to pilot AI solutions across education, climate resilience, and transportation public sectors.",
            "source": "portfolio",
            "is_highlight": True,
        },
        {
            "project_name": "Feasibility Assessment of National Policy Framework for Community Health Cadres in Posyandu",
            "client": "Ministry of Health",
            "practice_group": "Health and Wellbeing",
            "sector_tags": ["PublicHealth", "PrimaryHealthcare", "FeasibilityStudy"],
            "start_year": 2025,
            "end_year": 2025,
            "description": "Assessed feasibility of draft national policy framework for health-sector Posyandu cadres using HELOS framework across multiple cities and districts.",
            "source": "portfolio",
        },
        {
            "project_name": "Development of Breast Cancer Factsheets for Southeast Asia and Indonesia",
            "client": "Roche Indonesia",
            "practice_group": "Health and Wellbeing",
            "sector_tags": ["HealthAdvocacy", "PolicyCommunication", "HealthPolicy"],
            "start_year": 2025,
            "end_year": 2025,
            "description": "Developed evidence-based breast cancer factsheets synthesizing epidemiological trends, service delivery challenges, policy priorities, and emerging innovations.",
            "source": "portfolio",
        },
        {
            "project_name": "SPMI Stakeholder Analysis and Quality Platform Visualization Principles",
            "client": "Ministry of Education",
            "practice_group": "Education",
            "sector_tags": ["Education", "QualityAssurance", "EvidenceBasedPolicy"],
            "start_year": 2025,
            "end_year": 2026,
            "description": "Conducted stakeholder and process analysis to strengthen Internal Quality Assurance System (SPMI) implementation across schools and local governments.",
            "source": "portfolio",
        },
        {
            "project_name": "Promoting Better SOE Governance in Extractive Sector (EITI)",
            "client": "EITI Indonesia",
            "practice_group": "Strategy and Transformation",
            "sector_tags": ["Transparency", "ExtractiveIndustries", "Governance", "SOE"],
            "start_year": 2025,
            "end_year": 2025,
            "description": "Supported Indonesia's compliance with 2023 EITI Standard by conducting comprehensive diagnostic of SOE transparency practices across mining and oil and gas sectors.",
            "source": "portfolio",
        },
        {
            "project_name": "Development of National Strategy to Improve Availability, Access, and Affordability of Innovative Medicines",
            "client": "IPMG, Ministry of Health",
            "practice_group": "Health and Wellbeing",
            "sector_tags": ["HealthSystem", "Pharmaceutical", "NationalStrategy"],
            "start_year": 2025,
            "end_year": 2025,
            "description": "Synthesized 140+ policy documents, benchmarked 10+ countries, and facilitated cross-sectoral engagement across 17+ institutions to produce a 12-strategy, 34-action national roadmap.",
            "source": "portfolio",
        },
    ]

    existing = {p.project_name for p in db.query(PastProject).all()}
    added = 0

    for p in projects:
        entry = dict(p)
        sector_tags = entry.pop("sector_tags", [])
        if entry["project_name"] in existing:
            continue
        existing.add(entry["project_name"])

        # Every engagement below is Indonesian unless the record says otherwise.
        entry.setdefault("country", "Indonesia")
        # sector_tags lives on the project row as an array *and* is normalized
        # into project_sectors so it can be joined against.
        proj = PastProject(sector_tags=sector_tags, **entry)
        db.add(proj)
        db.flush()
        for tag in sector_tags:
            db.add(ProjectSector(project_id=proj.id, sector_tag=tag))
        added += 1

    db.flush()
    print(f"  Seeded {added} past projects")