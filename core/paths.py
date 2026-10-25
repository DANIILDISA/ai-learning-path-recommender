"""Self-locating paths for the product folder.

The folder ships as a zip the customer unpacks anywhere, so every path is
derived from this file's own location -- never from the current working
directory.
"""
from __future__ import annotations

import unicodedata
from pathlib import Path

PRODUCT_ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = PRODUCT_ROOT / "data"
OUTPUT_DIR = PRODUCT_ROOT / "output"
CATALOG_DIR = PRODUCT_ROOT / "catalog"
WEB_DIR = PRODUCT_ROOT / "web"


def data_profile_path() -> Path:
    """Where the saved column assignments live (git-ignored, not shipped)."""
    return OUTPUT_DIR / "data_profile.json"


def nfc(text: str) -> str:
    """Compose text to Unicode NFC."""
    return unicodedata.normalize("NFC", text)


def find_data_file(prefix: str, data_dir: Path | None = None) -> Path:
    """Locate a data file whose name starts with `prefix`.

    Customer exports arrive with inconsistent Unicode normalisation: the
    history file spells 'й' decomposed (NFD) while the registry uses the
    composed form (NFC). A literal filename in source code is written NFC by
    every editor, so it silently fails to match the NFD file on disk. Comparing
    normalised, case-folded forms is the only reliable way to find them.
    """
    directory = data_dir if data_dir is not None else DATA_DIR
    target = nfc(prefix).casefold()
    matches = [
        path
        for path in sorted(directory.iterdir())
        if path.is_file() and nfc(path.name).casefold().startswith(target)
    ]
    if not matches:
        raise FileNotFoundError(f"No file starting with {prefix!r} in {directory}")
    if len(matches) > 1:
        names = [m.name for m in matches]
        raise ValueError(f"Ambiguous prefix {prefix!r} matches {names}")
    return matches[0]


def catalog_override_path() -> Path:
    """Каталог программ, загруженный из нового буклета (git-ignored, не поставляется)."""
    return OUTPUT_DIR / "programs.yaml"


def active_programs_path() -> Path:
    """Рабочий каталог программ: загруженный из буклета, а если его нет -- поставляемый `catalog/programs.yaml`."""
    override = catalog_override_path()
    return override if override.is_file() else CATALOG_DIR / "programs.yaml"


def catalog_backup_root() -> Path:
    return OUTPUT_DIR / "catalog_backup"
