"""Locate the AMANA source spreadsheets the seeders read.

The files were originally kept one level above the service directory, in the
combined working folder. That path does not exist inside a container image,
where the build context is this repository alone, so a copy now lives in
``data/`` and is resolved first. The legacy location is still searched so a
checkout sitting next to the original spreadsheets keeps working, and
``COMPANY_DATA_DIR`` overrides everything for deployments that mount the files
from a volume instead of baking them into the image.
"""
import os

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))


def data_dirs() -> list:
    """Directories searched for a source file, in priority order."""
    candidates = []
    override = os.getenv("COMPANY_DATA_DIR")
    if override:
        candidates.append(override)
    candidates.append(os.path.join(REPO_ROOT, "data"))
    candidates.append(os.path.dirname(REPO_ROOT))
    return [os.path.abspath(path) for path in candidates]


def resolve_data_file(filename: str) -> str:
    """Return the path to ``filename``.

    Falls back to the preferred location when the file is missing everywhere,
    so callers can report a single sensible path in their "not found" message.
    """
    searched = data_dirs()
    for directory in searched:
        candidate = os.path.join(directory, filename)
        if os.path.exists(candidate):
            return candidate
    return os.path.join(searched[0], filename)
