"""Seed employees, capability domains and skill mappings from the Talent Roster.

The workbook has three sheets we care about:

* ``Mapping Capability``  - the capability map. Practice-sector rows are written
  in ALL CAPS with only column A filled; the rows beneath them are the capability
  domains, with columns C-F holding newline-separated employee names for the
  Expert / Proficient / Applied / Familiar competency levels.
* ``Database For Project`` - the full employee roster (90 people), including
  education, certifications and self-declared technical skills.
* ``Sheet7``               - a smaller roster that additionally carries a CV link,
  which we merge in by name.

The workbook is loaded with ``data_only=True`` so we read the *computed* values.
Loading it without that flag yields raw ``IFERROR(...)`` formula strings.
"""
import difflib
import os
import re
from datetime import datetime, date

from openpyxl import load_workbook

from api.seed.paths import resolve_data_file
from crawler.models import Employee, CapabilityDomain, EmployeeSkill, PracticeGroup

# Column indices for the "Database For Project" sheet.
COL = {
    "name": 0,
    "employee_id": 1,
    "employment_type": 2,
    "gender": 3,
    "position": 4,
    "grading": 5,
    "practice_group": 6,
    "email": 7,
    "phone": 8,
    "birth_date": 10,
    "yoe": 12,
    "s1": 13,
    "grad_year": 14,
    "major": 15,
    "university": 16,
    "s2": 17,
    "grad_year2": 18,
    "major2": 19,
    "university2": 20,
    "certification": 22,
    "top_skills": 23,
    "self_dev": 24,
}

# Column indices for "Sheet7" (used only to pick up CV links).
SHEET7_NAME = 0
SHEET7_CV = 10

# Competency columns on the "Mapping Capability" sheet.
LEVEL_COLUMNS = {2: "Expert", 3: "Proficient", 4: "Applied", 5: "Familiar"}

# Values that Excel leaves behind for empty/broken lookups.
BLANKS = {"", "-", "n/a", "na", "#n/a", "#ref!", "#value!", "none", "null", "0"}


def _clean(value):
    """Normalize a cell into a stripped string, or None when it carries no data."""
    if value is None:
        return None
    text = str(value).strip()
    if text.lower() in BLANKS:
        return None
    # Any residual formula text means the workbook was read without data_only.
    if text.startswith("=") or "IFERROR" in text:
        return None
    return text


def _to_float(value):
    text = _clean(value)
    if text is None:
        return None
    try:
        return float(text.replace(",", "."))
    except ValueError:
        return None


def _to_year(value):
    number = _to_float(value)
    if number is None:
        return None
    year = int(number)
    return year if 1900 <= year <= 2100 else None


def _to_date(value):
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = _clean(value)
    if not text:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def _split_names(cell):
    """Split a competency cell into individual employee names.

    Cells hold names separated by newlines, but a few use commas or trailing
    semicolons, so we normalize all of those separators.
    """
    text = _clean(cell)
    if not text:
        return []
    parts = re.split(r"[\n;]+", text)
    names = []
    for part in parts:
        name = part.strip().strip(",").strip()
        # Drop leading list markers like "1." or "-".
        name = re.sub(r"^[\d]+[.)]\s*", "", name).strip()
        if len(name) >= 3:
            names.append(name)
    return names


def _normalize_key(name: str) -> str:
    """Lowercase + collapse whitespace, for tolerant name matching."""
    name = re.sub(r"\([^)]*\)", " ", name)  # drop nicknames like "(Angie)"
    return re.sub(r"\s+", " ", name.strip().lower())


class NameResolver:
    """Match the informal names in the capability map to roster employees.

    The capability map is maintained by hand, so a person shows up as "Diyon",
    "Diyon Iskandar" or "Diyon Iskandar Setiawan" across different rows, and
    occasionally with a typo ("Fia Mahnani"). We resolve progressively, from
    exact matches down to fuzzy ones, and only accept a partial match when it is
    unambiguous.
    """

    def __init__(self, employees):
        self.by_key = {_normalize_key(e.full_name): e for e in employees}

        # first+last, so "Andara Rahmadina" finds "Andara Chantika Rahmadina".
        self.by_first_last = {}
        # first token, used only when it points at exactly one person.
        first_token_counts = {}
        for key, emp in self.by_key.items():
            parts = key.split()
            if len(parts) >= 2:
                self.by_first_last.setdefault(f"{parts[0]} {parts[-1]}", emp)
            first_token_counts.setdefault(parts[0], []).append(emp)
        self.by_first_token = {
            token: emps[0] for token, emps in first_token_counts.items() if len(emps) == 1
        }

    def resolve(self, raw_name: str):
        key = _normalize_key(raw_name)
        if not key:
            return None

        if key in self.by_key:
            return self.by_key[key]

        parts = key.split()
        if len(parts) >= 2 and f"{parts[0]} {parts[-1]}" in self.by_first_last:
            return self.by_first_last[f"{parts[0]} {parts[-1]}"]

        # A prefix match ("Diyon Iskandar" -> "Diyon Iskandar Setiawan") counts
        # only when a single roster name extends it.
        prefixed = [e for k, e in self.by_key.items() if k.startswith(key + " ")]
        if len(prefixed) == 1:
            return prefixed[0]

        close = difflib.get_close_matches(key, self.by_key.keys(), n=1, cutoff=0.84)
        if close:
            return self.by_key[close[0]]

        # Last resort: a distinctive given name that belongs to exactly one person.
        if len(parts[0]) >= 5 and parts[0] in self.by_first_token:
            return self.by_first_token[parts[0]]

        return None


def _roster_path() -> str:
    return resolve_data_file("Talent Roster Expertise - AMANA Solutions.xlsx")


def seed_employees(db):
    path = _roster_path()
    if not os.path.exists(path):
        print(f"  Talent roster not found at {path}, skipping employee seed")
        return

    wb = load_workbook(path, data_only=True)

    mapping_ws = wb["Mapping Capability"]
    seed_capability_domains(db, mapping_ws)
    seed_employee_records(db, wb["Database For Project"], wb["Sheet7"])
    seed_employee_skills(db, mapping_ws)


def seed_capability_domains(db, ws):
    """Read practice sectors (ALL CAPS rows) and their capability domains."""
    existing = {(d.sector, d.name) for d in db.query(CapabilityDomain).all()}
    practice_groups = {pg.name.lower(): pg for pg in db.query(PracticeGroup).all()}

    added = 0
    for sector, domain_name, _row in _iter_capability_rows(ws):
        if (sector, domain_name) in existing:
            continue
        existing.add((sector, domain_name))

        # Attach to a practice group when the sector name maps onto one.
        pg = None
        for pg_name, pg_obj in practice_groups.items():
            if pg_name.split()[0] in sector.lower():
                pg = pg_obj
                break

        db.add(
            CapabilityDomain(
                sector=sector,
                name=domain_name,
                practice_group_id=pg.id if pg else None,
                keywords=domain_name.lower(),
            )
        )
        added += 1

    db.flush()
    print(f"  Seeded {added} capability domains")


def _iter_capability_rows(ws):
    """Yield ``(sector, domain_name, row)`` for every capability domain row.

    A sector header is an ALL-CAPS row with no competency columns filled. Using
    "column A set and columns B/C empty" as the test misfires on domains that
    simply have nobody at Expert level (e.g. "Civic tech"), so we key off the
    casing instead.
    """
    current_sector = None
    for idx, row in enumerate(ws.iter_rows(values_only=True)):
        label = _clean(row[0]) if row else None
        if not label:
            continue
        if idx == 0 or label.lower().startswith("manpower capability"):
            continue

        letters = [c for c in label if c.isalpha()]
        is_header = bool(letters) and all(c.isupper() for c in letters)
        if is_header:
            current_sector = label
            continue

        if current_sector:
            yield current_sector, label, row


def seed_employee_records(db, ws, sheet7):
    """Insert one row per person from the "Database For Project" sheet."""
    cv_links = {}
    for idx, row in enumerate(sheet7.iter_rows(values_only=True)):
        if idx == 0:
            continue
        name = _clean(row[SHEET7_NAME]) if row else None
        cv = _clean(row[SHEET7_CV]) if row and len(row) > SHEET7_CV else None
        if name and cv:
            cv_links[_normalize_key(name)] = cv

    existing = {_normalize_key(e.full_name) for e in db.query(Employee).all()}
    added = 0

    for idx, row in enumerate(ws.iter_rows(values_only=True)):
        if idx == 0:  # header
            continue

        name = _clean(row[COL["name"]]) if row else None
        if not name or len(name) < 3:
            continue
        if name.lower().startswith("nama lengkap"):
            continue

        key = _normalize_key(name)
        if key in existing:
            continue
        existing.add(key)

        def cell(field):
            index = COL[field]
            return _clean(row[index]) if len(row) > index else None

        db.add(
            Employee(
                full_name=name,
                employee_id=cell("employee_id"),
                employment_type=cell("employment_type"),
                gender=cell("gender"),
                position=cell("position"),
                grading=cell("grading"),
                practice_group=cell("practice_group"),
                email=cell("email"),
                phone=cell("phone"),
                birth_date=_to_date(row[COL["birth_date"]]) if len(row) > COL["birth_date"] else None,
                years_of_experience=_to_float(row[COL["yoe"]]) if len(row) > COL["yoe"] else None,
                education_level=cell("s1"),
                graduation_year=_to_year(row[COL["grad_year"]]) if len(row) > COL["grad_year"] else None,
                major=cell("major"),
                university=cell("university"),
                education_level2=cell("s2"),
                graduation_year2=_to_year(row[COL["grad_year2"]]) if len(row) > COL["grad_year2"] else None,
                major2=cell("major2"),
                university2=cell("university2"),
                certification=cell("certification"),
                top_technical_skills=cell("top_skills"),
                self_development_areas=cell("self_dev"),
                cv_url=cv_links.get(key),
                is_active=True,
            )
        )
        added += 1

    db.flush()
    print(f"  Seeded {added} employees")


def seed_employee_skills(db, ws):
    """Map each name in the competency columns onto an employee + domain pair."""
    resolver = NameResolver(db.query(Employee).all())
    domains = {(d.sector, d.name): d for d in db.query(CapabilityDomain).all()}
    seen = {
        (s.employee_id, s.capability_domain_id) for s in db.query(EmployeeSkill).all()
    }

    added = 0
    unmatched = set()

    for sector, domain_name, row in _iter_capability_rows(ws):
        domain = domains.get((sector, domain_name))
        if not domain:
            continue

        for col_idx, level in LEVEL_COLUMNS.items():
            if len(row) <= col_idx:
                continue
            for raw_name in _split_names(row[col_idx]):
                emp = resolver.resolve(raw_name)
                if emp is None:
                    unmatched.add(raw_name)
                    continue

                pair = (emp.id, domain.id)
                if pair in seen:
                    continue
                seen.add(pair)
                db.add(
                    EmployeeSkill(
                        employee_id=emp.id,
                        capability_domain_id=domain.id,
                        competency_level=level,
                    )
                )
                added += 1

    db.flush()
    print(f"  Seeded {added} employee skills")
    if unmatched:
        preview = ", ".join(sorted(unmatched)[:8])
        print(f"  ({len(unmatched)} names in the capability map had no roster row: {preview}...)")
