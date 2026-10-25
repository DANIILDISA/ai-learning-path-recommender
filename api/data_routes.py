"""Экран «Данные»: приём нового пакета выгрузок без правки кода и без перезапуска.

Поток: загрузить файлы (карантин) -> паспорт данных -> при необходимости поправить
колонки -> применить (старые файлы уходят в резерв, данные перечитываются в
работающем приложении) -> при желании откатить.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field

from api import state as state_module
from ingest import data_store
from ingest.passport import KIND_LABELS, ROLE_LABELS
from ingest.smart_table import HISTORY_SPEC, REGISTRY_SPEC

router = APIRouter()


# Служебные файлы Excel: `~$Книга.xlsx` -- пометка «файл открыт», 165 байт
# мусора. Их нельзя ни читать, ни, главное, сохранять под очищенным именем:
# `~$Ведомость.xlsx` превращался в `Ведомость.xlsx` и затирал настоящую
# ведомость в той же загрузке.
def _base_name(name: str) -> str:
    return name.replace(chr(92), "/").rsplit("/", 1)[-1]


def _is_service_file(name: str) -> bool:
    base = _base_name(name)
    return base.startswith(("~$", ".")) or base.lower().endswith(".tmp")


def _role_options() -> dict[str, list[dict[str, Any]]]:
    def opts(spec):
        return [
            {"role": r.key, "label": ROLE_LABELS.get(r.key, r.key), "required": r.key in spec.required}
            for r in spec.roles
            if r.used
        ]

    return {"history": opts(HISTORY_SPEC), "registry": opts(REGISTRY_SPEC)}


def _payload(passport) -> dict[str, Any]:
    data = passport.to_dict()
    data["role_options"] = _role_options()
    data["kind_labels"] = KIND_LABELS
    return data


def _guard(call):
    try:
        return call()
    except data_store.DataStoreError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


class MappingIn(BaseModel):
    tables: dict[str, Any] = Field(default_factory=dict)
    use: dict[str, str] = Field(default_factory=dict)
    ai: bool = False


@router.get("/passport")
def current_passport():
    """Паспорт данных, с которыми приложение работает сейчас."""
    passport = state_module.get_state().passport
    if passport is None:
        raise HTTPException(status_code=404, detail="Паспорт данных недоступен.")
    body = _payload(passport)
    body["backups"] = data_store.list_backups()
    return body


@router.post("/uploads/files")
async def upload_file(request: Request, name: str = Query(..., min_length=1), upload_id: str | None = None):
    """Один файл пакета (тело запроса -- сам файл). Первый вызов создаёт загрузку.

    Служебные файлы Excel (`~$...`) не принимаются, но и не молчат: в ответе
    они перечислены в `skipped`, и экран говорит об этом человеку.
    """
    data = await request.body()

    def run():
        uid = upload_id or data_store.new_upload()
        skipped = []
        if _is_service_file(name):
            skipped.append(_base_name(name))
        else:
            data_store.add_file(uid, name, data)
        return {"upload_id": uid, "files": data_store.list_files(uid), "skipped": skipped}

    return _guard(run)


@router.delete("/uploads/{upload_id}/files/{name}")
def delete_file(upload_id: str, name: str):
    _guard(lambda: data_store.remove_file(upload_id, name))
    return {"upload_id": upload_id, "files": _guard(lambda: data_store.list_files(upload_id))}


def _full_analysis(upload_id: str, overrides=None, llm=None) -> dict[str, Any]:
    """Разбор загрузки ровно тем же путём, каким данные читаются после «Применить».

    Раньше предпросмотр шёл через `load_package` без каталога: методист не видел
    ни связей по сходству, ни форматов, определённых по каталогу, ни
    предупреждений о должностях -- они появлялись только после применения.
    """
    import main as main_module

    directory = data_store.upload_dir(upload_id) / "files"
    if overrides is None:
        overrides = data_store.load_overrides(upload_id)
    saved = main_module.last_passport()
    try:
        profiles, _catalog, _report, passport = main_module.load_everything_ex(
            directory, llm=llm, overrides=overrides
        )
    finally:
        main_module._LAST_PASSPORT = saved
    body = _payload(passport)
    body.update(
        {
            "upload_id": upload_id,
            "files": data_store.list_files(upload_id),
            "overrides": overrides,
            "can_apply": bool(profiles),
            "preview": _change_preview(profiles, passport),
        }
    )
    return body


_STATUS_RU = {
    "passed": "пройден",
    "not_passed": "не пройден",
    "in_progress": "в процессе",
    "unknown": "статус не распознан",
}


def _entries_count(profiles) -> int:
    return sum(len(p.entries) for p in profiles.values())


def _change_preview(new_profiles, passport) -> dict[str, Any]:
    """Что изменится после «Применить»: было / станет, кто пропадёт, пример разбора."""
    state = state_module.get_state()
    current = state.profiles
    gone = [uid for uid in current if uid not in new_profiles]
    with_data = state.feedback.user_ids() | set(state.preferences.all())
    gone_with_data = [uid for uid in gone if uid in with_data]
    kept_with_data = [uid for uid in new_profiles if uid in with_data]

    sample = []
    for profile in new_profiles.values():
        if profile.entries:
            entry = profile.entries[0]
            sample.append(
                {
                    "user": profile.user_id,
                    "position": profile.position,
                    "iogv": profile.iogv,
                    "title": entry.title,
                    "status": _STATUS_RU.get(entry.status.value, entry.status.value),
                    "delivery": entry.delivery.value,
                }
            )
        if len(sample) >= 5:
            break

    used = next((t for t in passport.tables if t.used and t.kind == "history"), None)
    unused_columns = []
    if used is not None:
        taken = {c.index for c in used.columns if c.role != "ignore"}
        unused_columns = [h for i, h in enumerate(used.all_headers) if i not in taken and h]
    return {
        "old_people": len(current),
        "old_rows": _entries_count(current),
        "new_people": len(new_profiles),
        "new_rows": _entries_count(new_profiles),
        "gone_total": len(gone),
        "gone_examples": [
            {"user_id": uid, "position": current[uid].position} for uid in gone[:8]
        ],
        "gone_with_marks": len(gone_with_data),
        "kept_with_marks": len(kept_with_data),
        "unknown_delivery": passport.counters.get("delivery_unknown", 0),
        "delivery_from_catalog": passport.counters.get("delivery_from_catalog", 0),
        "sample": sample,
        "unused_columns": unused_columns,
    }


@router.get("/uploads/{upload_id}")
def analyse_upload(upload_id: str):
    return _guard(lambda: _full_analysis(upload_id))


@router.post("/uploads/{upload_id}/mapping")
def set_mapping(upload_id: str, body: MappingIn):
    """Ручная правка: какая таблица что значит и какая колонка какую роль играет."""

    def run():
        overrides = data_store.save_overrides(upload_id, {"tables": body.tables, "use": body.use})
        client = state_module.get_state().client
        llm = client if body.ai else None
        out = _full_analysis(upload_id, overrides, llm=llm)
        # Флажок «ИИ» раньше не давал никакой реакции: ни результата, ни причины.
        if body.ai:
            out["ai"] = {
                "requested": True,
                "available": client is not None,
                "message": (
                    "ИИ просмотрел таблицу; его подсказки применены там, где колонка не нашлась "
                    "по названию (в столбце «Уверенность» они помечены «подсказано ИИ»)."
                    if client is not None
                    else "ИИ недоступен: ключ языковой модели не настроен или запуск идёт без модели. "
                    "Колонки определены без него — выберите нужные вручную."
                ),
            }
        return out

    return _guard(run)


@router.post("/uploads/{upload_id}/apply")
def apply_upload(upload_id: str):
    def run():
        before = state_module.get_state()
        result = data_store.apply_upload(upload_id)
        state = state_module.reload_state()
        out = _payload(state.passport)
        out.update(result)
        out["backups"] = data_store.list_backups()
        out["aftermath"] = _aftermath(before, state)
        return out

    return _guard(run)


def _aftermath(before, after) -> dict[str, Any]:
    """Что осталось «висеть» после замены данных: отметки и ответы людей, которых больше нет."""
    with_data = after.feedback.user_ids() | set(after.preferences.all())
    orphans = sorted(uid for uid in with_data if uid not in after.profiles)
    return {
        "people_before": len(before.profiles),
        "people_after": len(after.profiles),
        "orphans_with_marks": len(orphans),
    }


@router.delete("/uploads/{upload_id}")
def discard_upload(upload_id: str):
    _guard(lambda: data_store.discard(upload_id))
    return {"ok": True}


@router.get("/backups")
def backups():
    return {"backups": data_store.list_backups()}


@router.get("/revert/preview")
def revert_preview():
    """К какой версии вернёмся: файлы, дата, сколько людей. Для окна подтверждения."""
    import main as main_module

    backups = data_store.list_backups()
    if not backups:
        raise HTTPException(status_code=400, detail="Нет сохранённой предыдущей версии данных.")
    chosen = backups[0]
    folder = data_store.backups_root() / chosen["id"] / "files"
    saved = main_module.last_passport()
    try:
        profiles, _c, _r, _p = main_module.load_everything_ex(folder)
    finally:
        main_module._LAST_PASSPORT = saved
    current = state_module.get_state().profiles
    return {
        "id": chosen["id"],
        "created": chosen["created"],
        "note": chosen["note"],
        "files": chosen["files"],
        "people": len(profiles),
        "rows": _entries_count(profiles),
        "current_people": len(current),
        "current_rows": _entries_count(current),
    }


@router.post("/revert")
def revert(backup_id: str | None = None):
    def run():
        before = state_module.get_state()
        result = data_store.revert(backup_id)
        state = state_module.reload_state()
        out = _payload(state.passport)
        out.update(result)
        out["backups"] = data_store.list_backups()
        out["aftermath"] = _aftermath(before, state)
        return out

    return _guard(run)


# ---------------------------------------------------------------- каталог программ (буклет)
import re as _re
import shutil as _shutil
import uuid as _uuid
from pathlib import Path as _Path

from catalog import import_booklet as booklet
from core import paths as _paths
from ingest.normalize import load_aliases as _load_aliases


def _booklet_root() -> _Path:
    return _paths.OUTPUT_DIR / "booklet_uploads"


def _booklet_dir(upload_id: str) -> _Path:
    if not _re.fullmatch(r"[0-9a-f]{8,32}", upload_id or ""):
        raise HTTPException(status_code=400, detail="Неизвестный идентификатор загрузки.")
    folder = _booklet_root() / upload_id
    if not folder.is_dir():
        raise HTTPException(status_code=400, detail="Загрузка не найдена (возможно, уже применена или удалена).")
    return folder


def _booklet_llm(use_ai: bool):
    return state_module.get_state().client if use_ai else None


def _aliases():
    from core.config import load_config

    config = load_config(_paths.PRODUCT_ROOT / "config.yaml")
    return _load_aliases(_paths.CATALOG_DIR / config.aliases_file)


def _reference_titles() -> list[str]:
    """Названия из истории и реестра: по ним импорт буклета чинит слитно написанные слова."""
    state = state_module.get_state()
    return [
        *(entry.title for profile in state.profiles.values() for entry in profile.entries),
        *(course.title for course in state.catalog),
    ]


def _booklet_report(upload_id: str, file: _Path, use_ai: bool) -> dict[str, Any]:
    try:
        result = booklet.import_source(
            file, llm=_booklet_llm(use_ai), cache_dir=_paths.OUTPUT_DIR / "booklet_cache",
            reference_titles=_reference_titles(),
        )
    except booklet.BookletError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    new_doc = booklet.to_yaml_document(result)
    diff = booklet.diff_catalogs(booklet.load_catalog_doc(_paths.active_programs_path()), new_doc)
    return {
        "upload_id": upload_id,
        "source": result.source,
        "kind": result.kind,
        "diff": diff,
        "text": booklet.render_diff(diff, result),
        "warnings": result.warnings,
        "stats": {k: v for k, v in result.stats.items() if k != "navigator_unmatched"},
        "navigator_unmatched": result.stats.get("navigator_unmatched", []),
        "llm_calls": result.llm_calls,
        "llm_tokens": result.llm_tokens,
        "can_apply": bool(result.programmes),
    }


def _booklet_backups() -> list[dict[str, Any]]:
    """Резервы каталога с числом программ -- чтобы откат не был прыжком в неизвестность."""
    result = []
    for meta in booklet.list_catalog_backups():
        item = dict(meta)
        try:
            saved = _paths.catalog_backup_root() / meta["id"] / "programs.yaml"
            # Резерв без файла -- это был поставляемый каталог: откат к нему
            # возвращает его, а не «0 программ».
            default = not meta.get("had_override") or not saved.is_file()
            doc = booklet.load_catalog_doc(_paths.CATALOG_DIR / "programs.yaml" if default else saved)
            item["programmes"] = len(doc.get("programmes") or [])
            item["is_default"] = default
        except Exception:  # noqa: BLE001 -- описание резерва не должно ломать экран
            item["programmes"] = None
        result.append(item)
    return result


@router.get("/booklet/current")
def booklet_current():
    return {**booklet.current_catalog_info(), "backups": _booklet_backups()}


@router.post("/booklet/upload")
async def booklet_upload(request: Request, name: str = Query(..., min_length=1), ai: bool = False):
    """Загрузить буклет / каталог: файл ждёт в карантине, в ответ -- отчёт «было / стало». Каталог не меняется."""
    data = await request.body()
    base = _Path(str(name).replace("\\", "/")).name
    suffix = _Path(base).suffix.lower()
    if suffix not in booklet.SUPPORTED:
        raise HTTPException(status_code=400, detail="Принимаются PDF-буклет, Excel (.xlsx, .xls), CSV и Word (.docx с таблицей).")
    if not data or len(data) > booklet.MAX_BOOKLET_BYTES:
        raise HTTPException(status_code=400, detail="Файл пустой или больше 60 МБ.")
    upload_id = _uuid.uuid4().hex[:12]
    folder = _booklet_root() / upload_id
    folder.mkdir(parents=True, exist_ok=True)
    safe = "booklet" + suffix
    (folder / safe).write_bytes(data)
    (folder / "name.txt").write_text(base, encoding="utf-8")
    try:
        report = _booklet_report(upload_id, folder / safe, ai)
    except HTTPException:
        _shutil.rmtree(folder, ignore_errors=True)
        raise
    report["name"] = base
    return report


@router.post("/booklet/{upload_id}/apply")
def booklet_apply(upload_id: str, ai: bool = False):
    folder = _booklet_dir(upload_id)
    file = next(p for p in folder.iterdir() if p.name.startswith("booklet"))
    try:
        result = booklet.import_source(
            file, llm=_booklet_llm(ai), cache_dir=_paths.OUTPUT_DIR / "booklet_cache",
            reference_titles=_reference_titles(),
        )
        done = booklet.apply_import(result, _aliases())
    except booklet.BookletError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    state_module.reload_state()
    _shutil.rmtree(folder, ignore_errors=True)
    return {**done, **booklet.current_catalog_info(), "backups": _booklet_backups()}


@router.delete("/booklet/{upload_id}")
def booklet_discard(upload_id: str):
    _shutil.rmtree(_booklet_dir(upload_id), ignore_errors=True)
    return {"ok": True}


@router.post("/booklet/revert")
def booklet_revert(backup_id: str | None = None):
    try:
        done = booklet.revert_catalog(backup_id)
    except booklet.BookletError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    state_module.reload_state()
    return {**done, **booklet.current_catalog_info(), "backups": _booklet_backups()}
