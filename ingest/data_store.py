"""Приём нового пакета данных: загрузка, проверка, применение, откат.

Новый пакет сначала лежит в «карантине» (output/uploads/<id>/) -- его можно
разобрать, посмотреть паспорт и поправить колонки, не трогая рабочие данные.
Применение переносит прежние файлы папки data/ в резерв (output/data_backup/),
кладёт на их место новые и сохраняет профиль данных; откат возвращает резерв.
"""
from __future__ import annotations

import json
import re
import shutil
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from core import paths
from ingest.normalize import load_aliases
from ingest.package import LoadedPackage, load_package, remember_profile
from ingest.smart_table import SUPPORTED_SUFFIXES, DataProfile, list_candidate_files

MAX_FILE_BYTES = 80 * 1024 * 1024
_SAFE_NAME = re.compile(r"[^\w .()\-№]+", re.UNICODE)


class DataStoreError(ValueError):
    """User-facing problem (bad file name, unknown upload ...)."""


def uploads_root() -> Path:
    return paths.OUTPUT_DIR / "uploads"


def backups_root() -> Path:
    return paths.OUTPUT_DIR / "data_backup"


def safe_filename(name: str) -> str:
    base = Path(str(name).replace("\\", "/")).name.strip()
    base = _SAFE_NAME.sub("_", base).strip(" ._") or "файл"
    suffix = Path(base).suffix.lower()
    if suffix not in SUPPORTED_SUFFIXES:
        raise DataStoreError(
            f"Файл «{base}» не подходит: принимаются Excel (.xlsx, .xls) и CSV (.csv, .txt)."
        )
    return base[-120:] if len(base) > 120 else base


def upload_dir(upload_id: str) -> Path:
    if not re.fullmatch(r"[0-9a-f]{8,32}", upload_id or ""):
        raise DataStoreError("Неизвестный идентификатор загрузки.")
    directory = uploads_root() / upload_id
    if not directory.is_dir():
        raise DataStoreError("Загрузка не найдена (возможно, уже применена или удалена).")
    return directory


def new_upload() -> str:
    upload_id = uuid.uuid4().hex[:12]
    (uploads_root() / upload_id / "files").mkdir(parents=True, exist_ok=True)
    return upload_id


def add_file(upload_id: str, name: str, data: bytes) -> str:
    directory = upload_dir(upload_id) / "files"
    if len(data) > MAX_FILE_BYTES:
        raise DataStoreError("Файл слишком большой (больше 80 МБ).")
    if not data:
        raise DataStoreError(f"Файл «{name}» пустой.")
    filename = safe_filename(name)
    target = directory / filename
    target.write_bytes(data)
    _drop_overrides(upload_id)        # набор файлов изменился -- прежняя ручная правка не годится
    return filename


def remove_file(upload_id: str, name: str) -> None:
    target = upload_dir(upload_id) / "files" / safe_filename(name)
    if target.is_file():
        target.unlink()
    _drop_overrides(upload_id)


def list_files(upload_id: str) -> list[dict[str, Any]]:
    directory = upload_dir(upload_id) / "files"
    return [{"name": p.name, "size": p.stat().st_size} for p in list_candidate_files(directory)]


def discard(upload_id: str) -> None:
    shutil.rmtree(upload_dir(upload_id), ignore_errors=True)


# ------------------------------------------------------------ ручная правка
def _overrides_path(upload_id: str) -> Path:
    return upload_dir(upload_id) / "overrides.json"


def _drop_overrides(upload_id: str) -> None:
    try:
        _overrides_path(upload_id).unlink()
    except (OSError, DataStoreError):
        pass


def load_overrides(upload_id: str) -> dict[str, Any]:
    path = _overrides_path(upload_id)
    if path.is_file():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pass
    return {}


def save_overrides(upload_id: str, overrides: dict[str, Any]) -> dict[str, Any]:
    cleaned = clean_overrides(overrides)
    _overrides_path(upload_id).write_text(json.dumps(cleaned, ensure_ascii=False, indent=1), encoding="utf-8")
    return cleaned


def clean_overrides(raw: dict[str, Any]) -> dict[str, Any]:
    """Keep only well-formed manual corrections (the API takes this from a browser)."""
    tables: dict[str, Any] = {}
    for key, ov in (raw.get("tables") or {}).items():
        if not isinstance(ov, dict):
            continue
        entry: dict[str, Any] = {}
        if ov.get("kind") in ("history", "registry", "ignored"):
            entry["kind"] = ov["kind"]
        if isinstance(ov.get("header_row"), int) and ov["header_row"] >= 0:
            entry["header_row"] = ov["header_row"]
        if isinstance(ov.get("columns"), dict):
            cols = {}
            for role, idx in ov["columns"].items():
                if isinstance(role, str) and (idx is None or (isinstance(idx, int) and not isinstance(idx, bool) and idx >= 0)):
                    cols[role] = idx
            entry["columns"] = cols
        if entry:
            tables[str(key)] = entry
    use = {k: str(v) for k, v in (raw.get("use") or {}).items() if k in ("history", "registry") and v}
    return {"tables": tables, "use": use}


def analyze_upload(upload_id: str, overrides: dict[str, Any] | None = None, llm=None) -> LoadedPackage:
    directory = upload_dir(upload_id) / "files"
    config = _config()
    aliases = load_aliases(paths.CATALOG_DIR / config.aliases_file)
    return load_package(
        directory,
        aliases,
        profile=DataProfile(paths.data_profile_path()),
        overrides=overrides if overrides is not None else load_overrides(upload_id),
        llm=llm,
        ai_cache_dir=paths.OUTPUT_DIR / "ai_mapping_cache",
        history_prefix=config.history_prefix,
        registry_prefix=config.registry_prefix,
        history_sheet=config.history_sheet,
    )


def _config():
    from core.config import load_config

    return load_config(paths.PRODUCT_ROOT / "config.yaml")


# --------------------------------------------------------------- применение
def _data_files() -> list[Path]:
    directory = paths.DATA_DIR
    if not directory.is_dir():
        return []
    return [p for p in directory.iterdir() if p.is_file() and not p.name.startswith(".")]


def _stamp() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S-%f")[:-3]


def _move_current_to_backup(note: str = "") -> Path | None:
    files = _data_files()
    if not files:
        return None
    target = backups_root() / _stamp()
    (target / "files").mkdir(parents=True, exist_ok=True)
    for path in files:
        shutil.move(str(path), str(target / "files" / path.name))
    profile = paths.data_profile_path()
    if profile.is_file():
        shutil.copy2(profile, target / "data_profile.json")
    (target / "meta.json").write_text(
        json.dumps({"created": datetime.now().isoformat(timespec="seconds"), "note": note}, ensure_ascii=False),
        encoding="utf-8",
    )
    return target


def apply_upload(upload_id: str, llm=None) -> dict[str, Any]:
    """Make an upload the working data. Returns {"backup": id|None, "files": [...]}.

    Refuses (DataStoreError) when the upload holds no usable history: the
    working data must never be replaced by something that cannot be loaded.
    """
    overrides = load_overrides(upload_id)
    package = analyze_upload(upload_id, overrides, llm)
    if not package.entries:
        raise DataStoreError(
            "В загруженных файлах не найдена история обучения, применять нечего. "
            "Проверьте сопоставление колонок или загрузите другие файлы."
        )
    staged = upload_dir(upload_id) / "files"
    paths.DATA_DIR.mkdir(parents=True, exist_ok=True)
    backup = _move_current_to_backup(note="перед применением новой загрузки")
    copied = []
    for path in list_candidate_files(staged):
        shutil.copy2(path, paths.DATA_DIR / path.name)
        copied.append(path.name)
    profile = DataProfile(paths.data_profile_path())
    profile.preferred = {}
    remember_profile(package, profile, prefer_tables=bool(overrides.get("use")))
    discard(upload_id)
    return {"backup": backup.name if backup else None, "files": copied}


def list_backups() -> list[dict[str, Any]]:
    root = backups_root()
    if not root.is_dir():
        return []
    result = []
    for directory in sorted((d for d in root.iterdir() if d.is_dir()), reverse=True):
        meta = {}
        try:
            meta = json.loads((directory / "meta.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pass
        files = [p.name for p in (directory / "files").glob("*")] if (directory / "files").is_dir() else []
        result.append({"id": directory.name, "created": meta.get("created", ""), "note": meta.get("note", ""), "files": files})
    return result


def revert(backup_id: str | None = None) -> dict[str, Any]:
    """Put a backup (default: the latest) back into data/. The data being
    replaced is itself backed up, so a revert can be reverted."""
    backups = list_backups()
    if not backups:
        raise DataStoreError("Нет сохранённой предыдущей версии данных.")
    chosen = next((b for b in backups if b["id"] == backup_id), None) if backup_id else backups[0]
    if chosen is None:
        raise DataStoreError("Такой резервной копии нет.")
    source = backups_root() / chosen["id"]
    previous = _move_current_to_backup(note="перед откатом")
    for path in (source / "files").glob("*"):
        shutil.copy2(path, paths.DATA_DIR / path.name)
    saved_profile = source / "data_profile.json"
    if saved_profile.is_file():
        shutil.copy2(saved_profile, paths.data_profile_path())
    shutil.rmtree(source, ignore_errors=True)
    return {"restored": chosen["id"], "files": chosen["files"], "backup": previous.name if previous else None}
