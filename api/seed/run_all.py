"""Populate the database with AMANA company data.

Every seeder is idempotent, so this is safe to re-run whenever the source
spreadsheets or portfolio documents are refreshed.
"""
from crawler.database import init_db, get_db
from crawler.models import PracticeGroup
from api.seed.seed_employees import seed_employees
from api.seed.seed_associations import seed_associations
from api.seed.seed_projects import seed_projects
from api.seed.seed_users import seed_users

PRACTICE_GROUPS = [
    {
        "name": "Strategy and Transformation",
        "description": "Institutional transformation, strategic planning, public sector reform",
    },
    {
        "name": "Health",
        "description": "Public health, health systems, epidemiology, health policy",
    },
    {
        "name": "Education",
        "description": "Education systems, curriculum development, compulsory education programs",
    },
    {
        "name": "Digital",
        "description": "Digital strategy, AI policy, digital government, technology implementation",
    },
    {
        "name": "Operations",
        "description": "Internal operations, finance, people and delivery support",
    },
]


def seed_practice_groups(db):
    existing = {pg.name for pg in db.query(PracticeGroup).all()}
    added = 0
    for group in PRACTICE_GROUPS:
        if group["name"] in existing:
            continue
        db.add(PracticeGroup(**group))
        added += 1
    db.flush()
    print(f"  Seeded {added} practice groups")


def run_all_seeds():
    print("Initializing database schema...")
    init_db()

    db = get_db()
    try:
        print("Seeding application users...")
        seed_users(db)

        print("Seeding practice groups...")
        seed_practice_groups(db)

        print("Seeding employees, capability domains and skills...")
        seed_employees(db)

        print("Seeding associations...")
        seed_associations(db)

        print("Seeding past projects...")
        seed_projects(db)

        db.commit()
        print("All seeds completed successfully.")
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


if __name__ == "__main__":
    run_all_seeds()
