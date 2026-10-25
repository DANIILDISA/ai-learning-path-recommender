"""Reader for the customer's learning-history export.

Built on `ingest.smart_table`: the sheet, the header row and the columns are
found by content, not by position or by name. A bad row is skipped with a
warning in the data passport; nothing here raises on malformed values.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from core.models import Delivery, HistoryEntry, Profile, Status
from ingest.normalize import course_key, normalize_text
from ingest.passport import Passport
from ingest.smart_table import (
    DataProfile,
    TableAnalysis,
    analyze_table,
    cell_text,
    iter_data_rows,
    read_file,
)
from ingest.titles import parse_title

SHEET_NAME = "ГГС 2024-2025"   # подсказка по умолчанию; лист всё равно ищется по содержимому

NO_POSITION = "Должность не указана"
NO_IOGV = "ИОГВ не указан"
_MAX_EXAMPLES = 5


def _cell(row: list[Any], idx: int | None) -> str:
    if idx is None or idx >= len(row):
        return ""
    return cell_text(row[idx])


def _split_homonyms(drafts: list[dict[str, Any]], has_pid: bool, passport: Passport) -> None:
    """One name, several different people -> distinct `user` keys.

    With a personnel-number column the number decides. Without it, the same name in
    different ИОГВ is read as different people (a transfer between ИОГВ is rarer than a
    namesake). Mutates `drafts[*]["user"]`; the visible name gets a short qualifier.
    """
    by_name: dict[str, dict[str, int]] = {}
    if has_pid:
        names = {d["user"] for d in drafts}
        pids = {d["pid"] for d in drafts if d["pid"]}
        if len(pids) > 1.15 * len(names) + 3:
            # номеров заметно больше, чем людей: это номер строки, а не человека
            passport.warn("Колонка «Табельный номер» не похожа на номер человека (уникальна почти в каждой строке): не использована.")
            has_pid = False
    key_field = "pid" if has_pid else "iogv"
    for d in drafts:
        k = d[key_field]
        if k:
            by_name.setdefault(d["user"], {})
            by_name[d["user"]][k] = by_name[d["user"]].get(k, 0) + 1
    split = {name: ks for name, ks in by_name.items() if len(ks) > 1}
    if not split:
        return
    for d in drafts:
        ks = split.get(d["user"])
        if not ks or not d[key_field]:
            continue
        label = f"таб. № {d['pid']}" if has_pid else d["iogv"]
        d["user"] = f"{d['user']} ({label})"
    passport.count("homonyms_split", len(split))
    if not has_pid:
        # без табельного номера «двое разных людей» -- наше допущение, а не факт: показываем менеджеру
        for name, ks in split.items():
            passport.homonym_cases.append(
                {"name": name, "iogvs": sorted(ks), "rows": sum(ks.values())}
            )
    names = ", ".join(f"«{n}»" for n in list(split)[:5])
    passport.warn(
        f"Одинаковые ФИО у разных людей ({len(split)}): {names}"
        + ("; различены по табельному номеру." if has_pid else "; различены по ИОГВ (табельного номера в файле нет).")
    )


def parse_history(
    analysis: TableAnalysis,
    aliases: dict[str, str | None],
    passport: Passport | None = None,
    registry_keys: set[str] | None = None,
) -> list[HistoryEntry]:
    """Turn an analysed history table into validated entries (never raises)."""
    passport = passport if passport is not None else Passport()
    c_user = analysis.column("user")
    c_pos = analysis.column("position")
    c_iogv = analysis.column("iogv")
    c_pid = analysis.column("person_id")
    c_title = analysis.column("title")
    c_status = analysis.column("status")
    c_delivery = analysis.column("delivery")
    where = f"«{analysis.table.sheet}»" if analysis.table.sheet else f"«{analysis.table.file}»"

    if c_status is None:
        passport.warn(
            "Колонка статуса (пройден / не пройден) не найдена: все записи считаются без итога. "
            "Укажите её на экране «Данные»."
        )
    if c_pos is None:
        passport.warn("Колонка «Должность» не найдена: должности не указаны, подборка менее точная.")
    if c_iogv is None:
        passport.warn("Колонка «ИОГВ» не найдена: сравнение с коллегами из своего ИОГВ недоступно.")

    drafts: list[dict[str, Any]] = []
    skipped_examples: list[str] = []
    last: dict[str, str] = {"user": "", "position": "", "iogv": "", "pid": ""}

    for number, row in iter_data_rows(analysis):
        passport.count("rows_read")
        user = _cell(row, c_user)
        position = _cell(row, c_pos)
        iogv = _cell(row, c_iogv)
        title_raw = _cell(row, c_title)
        pid = _cell(row, c_pid)

        if not user and sum(1 for c in row if cell_text(c)) <= 1 and (
            not title_raw or re.match(r"(?i)\s*(всего|итого|количество записей)\b", title_raw)
        ):
            passport.count("footer_rows")   # «Всего записей: 679», подпись, номер страницы -- не данные
            continue
        if not title_raw:
            passport.count("rows_skipped")
            if len(skipped_examples) < _MAX_EXAMPLES:
                skipped_examples.append(f"строка {number}: нет названия курса")
            continue
        if not user:
            # Объединённые ячейки: ФИО/должность/ИОГВ пусты, курс есть -- это продолжение человека выше.
            if not position and not iogv and last["user"]:
                user, position, iogv, pid = last["user"], last["position"], last["iogv"], pid or last["pid"]
                passport.count("filled_from_row_above")
            else:
                passport.count("rows_skipped")
                if len(skipped_examples) < _MAX_EXAMPLES:
                    skipped_examples.append(f"строка {number}: нет ФИО")
                continue
        last = {"user": user, "position": position, "iogv": iogv, "pid": pid}

        parts = parse_title(title_raw)
        if parts.dressed and parts.title != " ".join(title_raw.split()):
            passport.count("titles_cleaned")
        drafts.append(
            {
                "user": user,
                "position": position,
                "iogv": iogv,
                "pid": pid,
                "parts": parts,
                "status_raw": _cell(row, c_status),
                "delivery_raw": _cell(row, c_delivery),
            }
        )

    _split_homonyms(drafts, c_pid is not None, passport)

    # Должность/ИОГВ -- свойство человека: пустую ячейку в одной строке берём из других его строк.
    known_pos: dict[str, str] = {}
    known_iogv: dict[str, str] = {}
    for d in drafts:
        if d["position"]:
            known_pos.setdefault(d["user"], d["position"])
        if d["iogv"]:
            known_iogv.setdefault(d["user"], d["iogv"])

    entries: list[HistoryEntry] = []
    for d in drafts:
        parts = d["parts"]
        position = d["position"]
        iogv = d["iogv"]
        if not position:
            position = known_pos.get(d["user"], "")
            if position and c_pos is not None:
                passport.count("filled_from_other_rows")
        if not iogv:
            iogv = known_iogv.get(d["user"], "")
            if iogv and c_iogv is not None:
                passport.count("filled_from_other_rows")
        position = position or NO_POSITION
        iogv = iogv or NO_IOGV

        key = course_key(parts.title, aliases)
        if not key:
            passport.count("rows_skipped")
            continue

        delivery = Delivery.parse(d["delivery_raw"]) if c_delivery is not None else None
        if delivery is not None:
            passport.count("delivery_from_column")
        else:
            if c_delivery is not None and d["delivery_raw"]:
                passport.unknown("delivery", d["delivery_raw"])
            delivery = parts.delivery_hint
            if delivery is not None:
                passport.count("delivery_from_title")
            elif registry_keys and key in registry_keys:
                delivery = Delivery.EK
                passport.count("delivery_from_registry")
            else:
                # Не «принимаем за ЭК», а честно «не указан»: дальше формат
                # попробуют определить по каталогу программ (`main`), а что
                # не определится -- попадёт в паспорт отдельной строкой.
                delivery = Delivery.UNKNOWN
                passport.count("delivery_unknown")

        status = Status.parse(d["status_raw"])
        if status is Status.UNKNOWN:
            passport.count("status_unknown")
            if d["status_raw"]:
                passport.unknown("status", d["status_raw"])

        entries.append(
            HistoryEntry(
                user_id=d["user"],
                position=position,
                iogv=iogv,
                delivery=delivery,
                title=parts.title,
                course_key=key,
                status=status,
            )
        )
        passport.count("rows_used")

    if skipped_examples:
        total = passport.counters.get("rows_skipped", 0)
        passport.warn(
            f"Пропущено строк: {total} (лист {where}). Примеры: " + "; ".join(skipped_examples) + "."
        )
    unknown_share = passport.counters.get("status_unknown", 0) / max(1, len(entries))
    if entries and unknown_share > 0:
        raw = passport.unknown_values.get("status", {})
        names = ", ".join(f"«{k}»" for k in list(raw)[:5]) or "пустой статус"
        passport.warn(
            f"Статус не распознан у {passport.counters['status_unknown']} записей ({unknown_share:.0%}): {names}. "
            "Такие записи не считаются пройденными."
        )
    return entries


def read_history(
    path: str | Path,
    aliases: dict[str, str | None],
    sheet_name: str | None = None,
    *,
    passport: Passport | None = None,
    registry_keys: set[str] | None = None,
    profile: DataProfile | None = None,
    llm=None,
) -> list[HistoryEntry]:
    """Read the history table out of one file (any format, any sheet name).

    `sheet_name` is only a hint: if that sheet is absent or is not a history
    table, the best history-looking sheet is used instead. Raises `ValueError`
    only when the file contains no history table at all.
    """
    tables = read_file(path)
    analyses = [analyze_table(t, profile=profile, llm=llm, prefer_kind="history") for t in tables]
    candidates = [a for a in analyses if a.kind == "history"]
    if not candidates:
        raise ValueError(
            f"В файле {Path(path).name} не найдена таблица с историей обучения "
            f"(лист «{sheet_name or SHEET_NAME}» или любой другой с колонками ФИО / Курс / Статус). "
            f"Листы файла: {[t.sheet for t in tables]}"
        )
    hint = normalize_text(sheet_name or "")
    candidates.sort(
        key=lambda a: (a.data_rows, normalize_text(a.table.sheet) == hint, a.score), reverse=True
    )
    return parse_history(candidates[0], aliases, passport, registry_keys)


def build_profiles(entries: list[HistoryEntry]) -> dict[str, Profile]:
    """Group entries into one profile per person.

    Position and ИОГВ are taken from the person's first row; the export repeats
    them identically on every row of the same user.
    """
    profiles: dict[str, Profile] = {}
    for entry in entries:
        profile = profiles.get(entry.user_id)
        if profile is None:
            profile = Profile(
                user_id=entry.user_id,
                position=entry.position,
                iogv=entry.iogv,
                position_norm=normalize_text(entry.position),
            )
            profiles[entry.user_id] = profile
        profile.entries.append(entry)
    return profiles
