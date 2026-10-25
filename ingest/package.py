"""Загрузка пакета выгрузок целиком: найти файлы, понять, что есть что, прочитать.

Пакет -- это папка с файлами заказчика (история обучения, реестр электронных
курсов, что-то ещё). Имена файлов и листов ничего не значат: роль определяется
по содержимому (`ingest.smart_table`). Результат -- записи истории, курсы
реестра и «паспорт данных» с объяснением, что и как распознано.

Никогда не падает: нечитаемый файл или отсутствующий реестр -- это
предупреждение; отсутствие истории -- ошибка в паспорте (приложение при этом
запускается и ждёт новые данные на экране «Данные»).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from core.models import Course, HistoryEntry
from ingest.ek_registry import parse_registry
from ingest.history import parse_history
from ingest.normalize import course_key, normalize_text
from ingest.passport import Passport
from ingest.smart_table import (
    DataProfile,
    ReadError,
    TableAnalysis,
    analyze_table,
    list_candidate_files,
    read_file,
    to_table_info,
)


@dataclass
class LoadedPackage:
    entries: list[HistoryEntry] = field(default_factory=list)
    registry: list[Course] = field(default_factory=list)
    passport: Passport = field(default_factory=Passport)
    analyses: list[TableAnalysis] = field(default_factory=list)

    @property
    def has_history(self) -> bool:
        return bool(self.entries)


def _rows_word(count: int) -> str:
    if 11 <= count % 100 <= 14:
        return "строк"
    if count % 10 == 1:
        return "строка"
    if 2 <= count % 10 <= 4:
        return "строки"
    return "строк"


def _suffix_rank(analysis: TableAnalysis) -> int:
    """xlsx beats csv beats the rest when two files hold the same table."""
    suffix = Path(analysis.table.file).suffix.lower()
    return {".xlsx": 3, ".xlsm": 3, ".xls": 2, ".csv": 1}.get(suffix, 0)


def _choose(
    candidates: list[TableAnalysis],
    forced_key: str | None,
    name_hint: str,
    sheet_hint: str,
) -> TableAnalysis | None:
    if not candidates:
        return None
    if forced_key:
        for cand in candidates:
            if cand.key == forced_key:
                return cand
    hint = normalize_text(name_hint)
    sheet = normalize_text(sheet_hint)

    def rank(a: TableAnalysis):
        name_match = bool(hint) and normalize_text(a.table.file).startswith(hint)
        sheet_match = bool(sheet) and normalize_text(a.table.sheet) == sheet
        return (a.data_rows, name_match or sheet_match, a.score, _suffix_rank(a))

    return sorted(candidates, key=rank, reverse=True)[0]


def load_package(
    data_dir: str | Path,
    aliases: dict[str, str | None],
    *,
    profile: DataProfile | None = None,
    overrides: dict[str, Any] | None = None,
    llm=None,
    ai_cache_dir: str | Path | None = None,
    history_prefix: str = "",
    registry_prefix: str = "",
    history_sheet: str = "",
) -> LoadedPackage:
    """Read a whole folder. `overrides` is the manual correction from the UI:
    ``{"tables": {"файл::лист": {"kind": ..., "columns": {...}}}, "use": {"history": key, "registry": key}}``.
    """
    overrides = overrides or {}
    table_overrides: dict[str, Any] = overrides.get("tables") or {}
    use: dict[str, str] = dict(getattr(profile, "preferred", None) or {})
    use.update(overrides.get("use") or {})
    directory = Path(data_dir)
    passport = Passport(source_dir=str(directory))
    package = LoadedPackage(passport=passport)

    files = list_candidate_files(directory)
    if not files:
        passport.error(
            f"В папке данных нет файлов Excel или CSV ({directory}). Загрузите выгрузку на экране «Данные»."
        )
        return package

    analyses: list[TableAnalysis] = []
    for path in files:
        try:
            tables = read_file(path)
        except ReadError as exc:
            passport.warn(str(exc))
            continue
        except Exception as exc:  # noqa: BLE001 -- чужой файл не должен валить приложение
            passport.warn(f"Файл «{path.name}» пропущен: {exc}")
            continue
        for table in tables:
            try:
                analysis = analyze_table(
                    table,
                    profile=profile,
                    override=table_overrides.get(table.key),
                    llm=llm,
                    ai_cache_dir=ai_cache_dir,
                )
            except Exception as exc:  # noqa: BLE001
                passport.warn(f"Лист «{table.sheet or table.file}» не разобран: {exc}")
                continue
            analyses.append(analysis)
    package.analyses = analyses

    history_candidates = [a for a in analyses if a.kind == "history"]
    registry_candidates = [a for a in analyses if a.kind == "registry"]
    chosen_registry = _choose(registry_candidates, use.get("registry"), registry_prefix, "")
    chosen_history = _choose(history_candidates, use.get("history"), history_prefix, history_sheet)

    # реестр читаем первым: по нему определяется формат курсов без пометки ППК/ЭК
    if chosen_registry is not None:
        package.registry = parse_registry(chosen_registry, aliases, passport)
    else:
        passport.warn(
            "Реестр электронных курсов не найден. Приложение работает без него: "
            "курсы из реестра не попадут в каталог, формат обучения определяется по названиям."
        )
    registry_keys = {c.course_key for c in package.registry}

    if chosen_history is not None:
        package.entries = parse_history(chosen_history, aliases, passport, registry_keys or None)
        if not package.entries:
            passport.error("В таблице истории не оказалось ни одной пригодной строки.")
    else:
        seen = [
            f"«{a.table.file}»{' / «' + a.table.sheet + '»' if a.table.sheet else ''} — "
            f"{a.data_rows} {_rows_word(a.data_rows)}: {a.reason}"
            for a in analyses
            if a.kind == "ignored"
        ][:3]
        hint = (
            " Что я увидел в файлах: " + "; ".join(seen) + "."
            if seen
            else ""
        )
        passport.error(
            "Не найдена таблица с историей обучения (нужны колонки вроде «ФИО», «Курс», «Статус»)."
            + hint
            + " Если нужный лист есть в блоке «Остальные листы и файлы», выберите для него «Это история "
            "обучения» и укажите колонки. Если в файле объединённые ячейки, а лист читается как пустой, "
            "разверните их в Excel (Главная → Объединить → Отменить объединение) и загрузите файл снова."
        )

    if len(history_candidates) > 1 and chosen_history is not None:
        others = [a for a in history_candidates if a is not chosen_history]
        passport.warn(
            f"Найдено несколько таблиц с историей; использована «{chosen_history.table.file}»"
            f"{' / ' + chosen_history.table.sheet if chosen_history.table.sheet else ''} "
            f"({chosen_history.data_rows} строк). Не использованы: "
            + ", ".join(
                f"«{a.table.file}»{' / ' + a.table.sheet if a.table.sheet else ''} ({a.data_rows})" for a in others
            )
            + "."
        )
    if len(registry_candidates) > 1 and chosen_registry is not None:
        passport.warn(
            f"Найдено несколько реестров; использован «{chosen_registry.table.file}»"
            f"{' / ' + chosen_registry.table.sheet if chosen_registry.table.sheet else ''}."
        )

    # паспорт: таблицы (используемые первыми)
    chosen = {id(chosen_history), id(chosen_registry)} - {id(None)}
    for analysis in analyses:
        is_used = id(analysis) in chosen
        if analysis.kind == "ignored":
            info = to_table_info(analysis, False)
        elif is_used:
            info = to_table_info(analysis, True)
        else:
            info = to_table_info(analysis, False, "Подходит по форме, но для этой роли выбрана другая таблица.")
        passport.tables.append(info)
    passport.tables.sort(key=lambda t: (not t.used, t.kind == "ignored"))

    for info in passport.tables:
        if info.used:
            for col in info.columns:
                if col.confidence < 0.6 and col.role != "ignore":
                    passport.warn(
                        f"Колонка «{col.header or '(без названия)'}» отнесена к роли «{col.role_label}» "
                        f"неуверенно ({col.confidence:.0%}, {col.method_label}). Проверьте на экране «Данные»."
                    )
            for role in info.missing_roles:
                if role in ("annotation", "goal", "outcomes") and info.kind == "registry":
                    from ingest.passport import ROLE_LABELS

                    passport.warn(f"В реестре не найдена колонка «{ROLE_LABELS.get(role, role)}».")

    passport.summary = {
        "history_rows": len(package.entries),
        "people": len({e.user_id for e in package.entries}),
        "registry_courses": len(package.registry),
    }
    return package


def relink_entries(entries: list[HistoryEntry], mapping: dict[str, str]) -> list[HistoryEntry]:
    """Replace course_key per `mapping` (fuzzy catalogue links)."""
    if not mapping:
        return entries
    return [
        e.model_copy(update={"course_key": mapping[e.course_key]}) if e.course_key in mapping else e
        for e in entries
    ]


def resolve_unknown_delivery(entries: list[HistoryEntry], catalog) -> tuple[list[HistoryEntry], int]:
    """Определить формат записей «не указан» по каталогу.

    Если название есть в каталоге в одном формате, берём его оттуда.
    Возвращает записи и число определённых.
    """
    from core.models import Delivery

    resolved = 0
    result: list[HistoryEntry] = []
    for entry in entries:
        if entry.delivery is Delivery.UNKNOWN:
            course = catalog.get(entry.course_key)
            # Только когда каталог однозначен. Девять курсов идут в обоих
            # форматах: выбрать за человека один из них значило бы снова
            # угадывать, и такие записи остаются «не указан».
            if course is not None and len(course.deliveries) == 1:
                (value,) = course.deliveries
                entry = entry.model_copy(update={"delivery": value})
                resolved += 1
        result.append(entry)
    return result, resolved


def remember_profile(package: LoadedPackage, profile: DataProfile, prefer_tables: bool = False) -> None:
    """Persist the final column assignment of the used tables.

    With `prefer_tables` also remembers WHICH table was chosen for each role,
    so the same choice is made when the files are loaded again from data/.
    """
    used = {(t.file, t.sheet) for t in package.passport.tables if t.used}
    if prefer_tables:
        for info in package.passport.tables:
            if info.used and info.kind in ("history", "registry"):
                profile.preferred[info.kind] = info.key
    for analysis in package.analyses:
        if (analysis.table.file, analysis.table.sheet) in used and analysis.kind in ("history", "registry"):
            profile.remember(
                analysis.kind,
                analysis.fingerprint,
                analysis.matches,
                analysis.header_row,
                label=f"{analysis.table.file} / {analysis.table.sheet}",
            )
    profile.save()
