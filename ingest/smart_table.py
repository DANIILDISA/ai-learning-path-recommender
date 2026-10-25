"""«Умный разбор» таблиц: читаем что дали, а не то, что ожидали.

Заказчик меняет не смысл выгрузки, а её упаковку: другой лист, лишние колонки,
заголовок не в первой строке, CSV вместо Excel, переименованные колонки. Этот
модуль превращает любую правдоподобную упаковку в одно и то же внутреннее
представление и честно говорит, насколько он уверен.

Уровни (от дешёвого к дорогому):

1. **Чтение** xlsx / xls / csv (формат определяется по содержимому файла, а не
   по расширению; разделитель и кодировка CSV угадываются), все листы.
2. **Строка заголовка** -- в первых ~15 строках, по числу совпадений со словарём.
3. **Роль таблицы по содержимому** -- история / реестр / служебная, а не по имени.
4. **Колонки**: словарь синонимов + нечёткое сравнение; у каждой своя уверенность.
   Если заголовка нет или колонка без названия, роль выводится по значениям
   (статусы, ППК/ЭК) и по порядку колонок.
5. **Профиль данных** (``data_profile.json``): сопоставление, которое человек
   поправил в интерфейсе, запоминается по «отпечатку» заголовков и применяется
   к следующей такой же выгрузке автоматически.
6. **ИИ-подстраховка** (необязательна): если обязательная колонка не найдена,
   модель получает заголовки и несколько строк и возвращает сопоставление в
   JSON. Ответ проверяется кодом и кэшируется на диск. Без модели шаг пропускается.

Модуль ничего не знает про курсы и людей -- это делают ``ingest.history`` и
``ingest.ek_registry``.
"""
from __future__ import annotations

import csv
import datetime as dt
import hashlib
import io
import json
import re
import threading
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from core.models import Delivery, Status
from ingest.normalize import is_blank, normalize_text
from ingest.passport import METHOD_LABELS, ROLE_LABELS, ColumnInfo, TableInfo

HEADER_SCAN_ROWS = 15
ACCEPT = 0.7                      # минимальная уверенность для принятия колонки по заголовку
CONTENT_SAMPLE = 300

try:  # rapidfuzz быстрее и точнее; встроенный difflib -- запасной вариант
    from rapidfuzz import fuzz as _fuzz

    def similarity(a: str, b: str) -> float:
        return max(_fuzz.ratio(a, b), _fuzz.token_sort_ratio(a, b)) / 100.0

except Exception:  # pragma: no cover - зависит от окружения
    from difflib import SequenceMatcher

    def similarity(a: str, b: str) -> float:
        return SequenceMatcher(None, a, b).ratio()


class ReadError(Exception):
    """Файл нельзя прочитать; сообщение -- готовая фраза для менеджера."""


# --------------------------------------------------------------------------
# 1. Чтение
# --------------------------------------------------------------------------
@dataclass
class RawTable:
    file: str
    sheet: str
    rows: list[list[Any]]
    notes: list[str] = field(default_factory=list)

    @property
    def key(self) -> str:
        return f"{self.file}::{self.sheet}"

    @property
    def width(self) -> int:
        return max((len(r) for r in self.rows), default=0)


def cell_text(value: Any) -> str:
    """Cell -> trimmed text. Whole floats lose '.0'; dates become ISO."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "да" if value else "нет"
    if isinstance(value, float):
        if value != value:  # NaN
            return ""
        if value == int(value):
            return str(int(value))
        return str(value)
    if isinstance(value, (dt.datetime, dt.date)):
        return value.date().isoformat() if isinstance(value, dt.datetime) and not (
            value.hour or value.minute or value.second
        ) else value.isoformat(sep=" ")
    text = str(value).replace("\xa0", " ").replace("​", "").replace("﻿", "")
    return text.strip()


def _row_blank(row: Sequence[Any]) -> bool:
    return all(is_blank(c) for c in row)


def _pad(rows: list[list[Any]]) -> list[list[Any]]:
    width = max((len(r) for r in rows), default=0)
    return [list(r) + [None] * (width - len(r)) for r in rows]


def sniff_format(path: Path) -> str:
    """'xlsx' | 'xls' | 'text' by magic bytes, extension only as a tiebreaker."""
    try:
        with open(path, "rb") as fh:
            head = fh.read(8)
    except OSError as exc:
        raise ReadError(f"Файл «{path.name}» не удалось открыть: {exc.strerror or exc}") from exc
    if not head:
        raise ReadError(f"Файл «{path.name}» пустой.")
    if head[:4] == b"PK\x03\x04":
        return "xlsx"
    if head[:8] == bytes.fromhex("D0CF11E0A1B11AE1"):
        return "xls"
    return "text"


def decode_text(data: bytes) -> tuple[str, str]:
    """bytes -> (text, encoding name). BOMs, utf-8, then cp1251."""
    if data.startswith(b"\xef\xbb\xbf"):
        return data[3:].decode("utf-8", errors="replace"), "utf-8-sig"
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16", errors="replace"), "utf-16"
    try:
        return data.decode("utf-8"), "utf-8"
    except UnicodeDecodeError:
        pass
    for enc in ("cp1251", "cp866"):
        try:
            text = data.decode(enc)
        except UnicodeDecodeError:
            continue
        if enc == "cp866" and not re.search(r"[А-Яа-я]", text):
            continue
        return text, enc
    return data.decode("cp1251", errors="replace"), "cp1251?"


def sniff_delimiter(text: str) -> str:
    lines = [ln for ln in text.splitlines()[:40] if ln.strip()]
    best, best_score = ";", -1.0
    for delim in (";", "\t", ",", "|"):
        counts = []
        for row in csv.reader(lines, delimiter=delim):
            counts.append(len(row))
        if not counts:
            continue
        modal = max(set(counts), key=counts.count)
        if modal < 2:
            continue
        score = counts.count(modal) * modal
        if score > best_score:
            best, best_score = delim, score
    return best


def _read_text(path: Path) -> list[RawTable]:
    data = path.read_bytes()
    text, enc = decode_text(data)
    delim = sniff_delimiter(text)
    rows = [list(r) for r in csv.reader(io.StringIO(text), delimiter=delim)]
    notes = [f"CSV: кодировка {enc}, разделитель {'табуляция' if delim == chr(9) else repr(delim)}"]
    return [RawTable(path.name, "", _pad(rows), notes)]


_MERGE_AWARE_MAX_BYTES = 25 * 1024 * 1024


def _expand_vertical_merges(sheet, rows: list[list[Any]]) -> int:
    """Copy the top-left value of every VERTICAL merge into the rest of the merged block.

    Horizontal-only merges (banners over the header) are left alone: copying them
    would make a banner look like a header row. Returns the number of cells filled.
    """
    filled = 0
    try:
        ranges = list(sheet.merged_cells.ranges)
    except Exception:  # noqa: BLE001
        return 0
    for rng in ranges:
        if rng.max_row <= rng.min_row:
            continue
        r0, c0 = rng.min_row - 1, rng.min_col - 1
        if r0 >= len(rows) or c0 >= len(rows[r0]):
            continue
        value = rows[r0][c0]
        if is_blank(value):
            continue
        for r in range(r0, min(rng.max_row, len(rows))):
            for c in range(c0, min(rng.max_col, len(rows[r]))):
                if (r, c) != (r0, c0) and is_blank(rows[r][c]):
                    rows[r][c] = value
                    filled += 1
    return filled


def _read_xlsx(path: Path) -> list[RawTable]:
    import openpyxl

    # Обычный режим нужен, чтобы видеть объединённые ячейки (в read_only их нет); для больших файлов -- потоковый.
    merged_ok = path.stat().st_size <= _MERGE_AWARE_MAX_BYTES
    try:
        workbook = openpyxl.load_workbook(path, data_only=True, read_only=not merged_ok)
    except Exception as exc:  # noqa: BLE001
        raise ReadError(f"Файл «{path.name}» не удалось открыть как Excel: {exc}") from exc
    tables: list[RawTable] = []
    try:
        for sheet in workbook.worksheets:
            try:
                sheet.reset_dimensions()  # иначе неверная «ссылка на диапазон» обрежет лист
            except Exception:  # noqa: BLE001
                pass
            try:
                rows = [list(r) for r in sheet.iter_rows(values_only=True)]
            except Exception as exc:  # noqa: BLE001
                tables.append(RawTable(path.name, sheet.title, [], [f"лист не читается: {exc}"]))
                continue
            rows = _pad(rows)
            notes: list[str] = []
            if merged_ok:
                filled = _expand_vertical_merges(sheet, rows)
                if filled:
                    notes.append(f"Объединённые ячейки раскрыты: {filled} пустых ячеек заполнено значением из объединения.")
            tables.append(RawTable(path.name, sheet.title, rows, notes))
    finally:
        workbook.close()  # иначе файл остаётся занят на Windows
    return tables


def _read_xls(path: Path) -> list[RawTable]:
    try:
        import xlrd
    except ImportError as exc:  # pragma: no cover - зависит от окружения
        raise ReadError(
            f"Файл «{path.name}» в старом формате .xls, а пакет xlrd не установлен. "
            "Сохраните файл как .xlsx (Файл -> Сохранить как) или установите xlrd."
        ) from exc
    try:
        book = xlrd.open_workbook(str(path))
    except Exception as exc:  # noqa: BLE001
        raise ReadError(f"Файл «{path.name}» не удалось открыть как .xls: {exc}") from exc
    tables = []
    for sheet in book.sheets():
        rows: list[list[Any]] = []
        for r in range(sheet.nrows):
            row: list[Any] = []
            for c in range(sheet.ncols):
                cell = sheet.cell(r, c)
                if cell.ctype == xlrd.XL_CELL_DATE:
                    try:
                        row.append(xlrd.xldate.xldate_as_datetime(cell.value, book.datemode))
                        continue
                    except Exception:  # noqa: BLE001
                        pass
                if cell.ctype in (xlrd.XL_CELL_EMPTY, xlrd.XL_CELL_BLANK):
                    row.append(None)
                else:
                    row.append(cell.value)
            rows.append(row)
        tables.append(RawTable(path.name, sheet.name, _pad(rows)))
    return tables


def read_file(path: str | Path) -> list[RawTable]:
    """All sheets of a file as raw tables. Raises ReadError with a ready message."""
    path = Path(path)
    kind = sniff_format(path)
    try:
        if kind == "xlsx":
            return _read_xlsx(path)
        if kind == "xls":
            return _read_xls(path)
        return _read_text(path)
    except ReadError:
        raise
    except Exception as exc:  # noqa: BLE001 -- любой сбой чтения -> понятная фраза
        raise ReadError(f"Файл «{path.name}» не удалось прочитать: {exc}") from exc


SUPPORTED_SUFFIXES = {".xlsx", ".xlsm", ".xls", ".csv", ".tsv", ".txt"}


def list_candidate_files(directory: Path) -> list[Path]:
    """Data files directly inside `directory` (no recursion), stable order."""
    if not directory.is_dir():
        return []
    result = []
    for path in sorted(directory.iterdir(), key=lambda p: p.name.casefold()):
        name = path.name
        if not path.is_file() or name.startswith(("~$", ".")) or name.lower().endswith(".tmp"):
            continue
        if path.suffix.lower() in SUPPORTED_SUFFIXES:
            result.append(path)
    return result


# --------------------------------------------------------------------------
# 2. Словари ролей
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class RoleSpec:
    key: str
    synonyms: tuple[str, ...]
    exact_only: tuple[str, ...] = ()      # слишком общие слова: «название», «имя» -- только целиком
    used: bool = True                     # False -- колонку узнаём, чтобы не спутать, но не читаем


@dataclass(frozen=True)
class TableSpec:
    kind: str
    roles: tuple[RoleSpec, ...]
    required: tuple[str, ...]
    needs_any: tuple[str, ...] = ()       # хотя бы одна из этих ролей должна быть найдена
    canonical_order: tuple[str, ...] = () # порядок колонок, если больше опереться не на что

    def role(self, key: str) -> RoleSpec | None:
        return next((r for r in self.roles if r.key == key), None)


def _norm_all(items: Iterable[str]) -> tuple[str, ...]:
    return tuple(normalize_text(i) for i in items)


HISTORY_SPEC = TableSpec(
    kind="history",
    required=("user", "title"),
    needs_any=("status", "position", "iogv"),
    canonical_order=("user", "position", "iogv", "title"),
    roles=(
        RoleSpec("user", (
            "фио", "ф и о", "фио сотрудника", "фио слушателя", "фио обучающегося", "фио работника",
            "фио пользователя", "фио участника", "фамилия имя отчество", "фамилия и имя", "сотрудник",
            "слушатель", "обучающийся", "пользователь", "участник", "работник", "гражданский служащий",
            "государственный служащий", "учетная запись", "user", "employee", "student", "full name",
            "сотрудник фио", "обучаемый", "ученик", "фамилия и о", "фамилия инициалы", "фамилия и инициалы",
            "фио краткое", "сотрудник фамилия и о",
        ), exact_only=("имя", "name", "фамилия", "логин")),
        RoleSpec("person_id", (
            "табельный номер", "табельный", "табельный n", "табельный номер сотрудника", "таб номер", "таб n",
            "personnel number", "employee id", "личный номер", "идентификатор сотрудника", "id сотрудника",
        )),
        RoleSpec("position", (
            "должность", "наименование должности", "название должности", "должность сотрудника",
            "занимаемая должность", "должность по штатному расписанию", "position", "job title", "post",
        ), exact_only=("позиция",)),
        RoleSpec("iogv", (
            "иогв", "исполнительный орган", "исполнительный орган государственной власти", "орган власти",
            "ведомство", "организация", "место работы", "учреждение", "работодатель", "iogv",
            "organization", "наименование иогв", "иогв организация",
        ), exact_only=("орган",)),
        RoleSpec("delivery", (
            "ппк эк", "ппк/эк", "формат", "формат обучения", "тип курса", "тип обучения", "вид обучения",
            "форма обучения", "вид программы", "тип программы", "формат курса", "delivery", "format",
            "ппк или эк", "вид курса",
        ), exact_only=("тип", "форма", "вид")),
        RoleSpec("title", (
            "курс", "название курса", "наименование курса", "программа", "название программы",
            "наименование программы", "образовательная программа", "учебная программа", "курс обучения",
            "наименование курса обучения", "мероприятие", "наименование мероприятия", "название мероприятия",
            "дисциплина", "course", "program", "название учебного курса", "наименование учебного курса",
            "тема обучения", "наименование обучения", "программа обучения", "название обучения",
            "курс программа",
        ), exact_only=("обучение", "тема")),
        RoleSpec("status", (
            "статус", "статус обучения", "статус прохождения", "статус курса", "результат", "результат обучения",
            "результат прохождения", "итог", "итог обучения", "состояние", "прохождение", "статус слушателя",
            "status", "result", "прохождение курса", "итоги обучения", "результат курса", "статус программы",
            "итог освоения", "итог освоения программы", "освоение", "освоение программы", "итог прохождения",
            "результат освоения", "статус освоения",
        )),
        RoleSpec("ignore", (
            "дата", "дата начала", "дата окончания", "дата завершения", "дата прохождения", "дата обучения",
            "email", "e mail", "почта", "электронная почта", "идентификатор", "телефон",
            "балл", "оценка", "количество часов", "часов", "часы", "продолжительность", "период", "год",
            "комментарий", "примечание", "снилс", "инн", "пол", "возраст", "номер", "№", "n", "id",
            "руководитель", "категория", "группа должностей", "уровень", "регион", "город",
        ), used=False),
    ),
)

REGISTRY_SPEC = TableSpec(
    kind="registry",
    required=("title",),
    needs_any=("annotation", "goal", "outcomes"),
    canonical_order=("title", "annotation", "goal", "outcomes"),
    roles=(
        RoleSpec("title", (
            "название курса", "наименование курса", "название программы", "наименование программы",
            "название электронного курса", "наименование электронного курса", "курс", "программа",
            "название учебного курса", "наименование учебного курса", "образовательная программа",
        ), exact_only=("название", "наименование", "тема")),
        RoleSpec("annotation", (
            "аннотация", "описание", "краткое описание", "описание курса", "аннотация курса",
            "содержание", "краткая аннотация", "annotation", "description", "о курсе", "описание программы",
        )),
        RoleSpec("goal", (
            "цель задачи", "цель", "цели", "цели и задачи", "цель программы", "цель курса", "задачи",
            "цель обучения", "цели обучения", "goal", "цель и задачи",
        )),
        RoleSpec("outcomes", (
            "результаты знать уметь владеть", "результаты", "результаты обучения", "ожидаемые результаты",
            "планируемые результаты", "результаты освоения", "результаты освоения курса",
            "outcomes", "знать уметь владеть",
        )),
        RoleSpec("ignore", (
            "часы", "часов", "количество часов", "трудоемкость", "ссылка", "url", "id", "идентификатор",
            "дата", "дата создания", "категория", "раздел", "статус", "примечание", "комментарий", "номер",
            "№", "n", "автор", "разработчик", "формат",
        ), used=False),
    ),
)

SPECS = {"history": HISTORY_SPEC, "registry": REGISTRY_SPEC}


@dataclass
class ColumnMatch:
    role: str
    index: int
    header: str
    confidence: float
    method: str          # exact | contains | fuzzy | content | order | profile | manual | ai


# --------------------------------------------------------------------------
# 3. Сопоставление заголовков
# --------------------------------------------------------------------------
def _tokens(text: str) -> list[str]:
    return text.split()


def _tok_eq(a: str, b: str) -> bool:
    if a == b:
        return True
    if min(len(a), len(b)) < 4:
        return False
    common = 0
    for x, y in zip(a, b):
        if x != y:
            break
        common += 1
    return common >= max(4, min(len(a), len(b)) - 2)


def _contains(cell_tokens: list[str], syn_tokens: list[str]) -> bool:
    n, m = len(cell_tokens), len(syn_tokens)
    for i in range(n - m + 1):
        if all(_tok_eq(cell_tokens[i + j], syn_tokens[j]) for j in range(m)):
            return True
    return False


def header_score(cell: str, role: RoleSpec, *, _cache: dict | None = None) -> tuple[float, str]:
    """(score 0..1, method) of `cell` (already normalised) against one role."""
    if not cell:
        return 0.0, ""
    best, method = 0.0, ""
    syns = _role_synonyms(role)
    if cell in syns[1]:
        return 1.0, "exact"
    if cell in syns[0]:
        return 1.0, "exact"
    cell_tokens = _tokens(cell)
    for syn in syns[0]:
        if len(syn) <= 2 and syn != cell:
            continue
        syn_tokens = _tokens(syn)
        if len(cell_tokens) > len(syn_tokens) and len(syn) >= 3 and _contains(cell_tokens, syn_tokens):
            score = 0.8 + 0.15 * (len(syn_tokens) / len(cell_tokens))
            if score > best:
                best, method = score, "contains"
        elif len(cell_tokens) == len(syn_tokens) and len(syn) >= 4 and len(cell) >= 4:
            if all(_tok_eq(a, b) for a, b in zip(cell_tokens, syn_tokens)):
                if 0.93 > best:
                    best, method = 0.93, "fuzzy"
                continue
            ratio = similarity(cell, syn)
            if len(syn) >= 5 and ratio >= 0.86 and ratio * 0.92 > best:
                best, method = ratio * 0.92, "fuzzy"
    return best, method


_SYN_CACHE: dict[tuple[str, tuple[str, ...]], tuple[tuple[str, ...], tuple[str, ...]]] = {}


def _role_synonyms(role: RoleSpec) -> tuple[tuple[str, ...], tuple[str, ...]]:
    key = (role.key, role.synonyms + role.exact_only)
    cached = _SYN_CACHE.get(key)
    if cached is None:
        cached = (_norm_all(role.synonyms), _norm_all(role.exact_only))
        _SYN_CACHE[key] = cached
    return cached


def assign_by_headers(headers: Sequence[str], spec: TableSpec) -> dict[str, ColumnMatch]:
    """Greedy one-to-one role<->column assignment by header text."""
    normed = [normalize_text(h) for h in headers]
    candidates: list[tuple[float, int, int, ColumnMatch]] = []
    for col, cell in enumerate(normed):
        if not cell:
            continue
        if cell.startswith("дата ") or cell.endswith(" дата") or cell in ("дата", "date"):
            ignore_role = spec.role("ignore")
            if ignore_role is not None:
                candidates.append((1.0, 0, -col, ColumnMatch("ignore", col, str(headers[col]), 1.0, "exact")))
            continue
        for order, role in enumerate(spec.roles):
            score, method = header_score(cell, role)
            if score >= ACCEPT:
                candidates.append(
                    (score, -order, -col, ColumnMatch(role.key, col, str(headers[col]), round(score, 3), method))
                )
    candidates.sort(key=lambda t: (t[0], t[1], t[2]), reverse=True)
    taken_cols: set[int] = set()
    result: dict[str, ColumnMatch] = {}
    ignored: dict[int, ColumnMatch] = {}
    for _score, _o, _c, match in candidates:
        if match.index in taken_cols:
            continue
        if match.role == "ignore":
            taken_cols.add(match.index)
            ignored[match.index] = match
            continue
        if match.role in result:
            continue
        taken_cols.add(match.index)
        result[match.role] = match
    # служебные колонки отдаём отдельными записями «ignore:<номер>»
    for idx, match in ignored.items():
        result[f"ignore:{idx}"] = ColumnMatch("ignore", idx, match.header, match.confidence, match.method)
    return result


def _row_headers(row: Sequence[Any]) -> list[str]:
    return [cell_text(c) for c in row]


_STATUS_WORDS = {normalize_text(w) for w in (
    "пройден", "не пройден", "пройдено", "не пройдено", "в процессе", "завершен", "не завершен",
    "завершено", "не завершено", "назначен", "отчислен", "не начат", "в работе", "успешно", "зачет",
    "освоил", "освоила", "не освоил", "не освоила", "не приступал", "не приступала", "зачтено", "не зачтено",
)}


def _is_status_value(text: str) -> bool:
    """A cell that is itself a status word («Не пройден») -- so its row is data, not a header."""
    return normalize_text(text) in _STATUS_WORDS


def find_header_row(rows: list[list[Any]], spec: TableSpec, scan: int = HEADER_SCAN_ROWS) -> tuple[int | None, dict[str, ColumnMatch], float]:
    """Best header row among the first `scan` rows -> (index, matches, score)."""
    best_idx: int | None = None
    best_matches: dict[str, ColumnMatch] = {}
    best_score = 0.0
    for idx, row in enumerate(rows[:scan]):
        if _row_blank(row):
            continue
        texts = _row_headers(row)
        filled = [t for t in texts if t]
        # строка с одной ячейкой -- баннер, не заголовок
        if len(filled) < 2:
            continue
        # длинные тексты-предложения в ячейках -- это данные, а не заголовок
        if sum(1 for t in filled if len(t) > 80) > len(filled) / 2:
            continue
        # Строка, в которой есть значение статуса («Не пройден») или «ППК»/«ЭК», -- это данные.
        if any(_is_status_value(t) or normalize_text(t) in ("ппк", "эк") for t in filled):
            continue
        matches = assign_by_headers(texts, spec)
        real = {k: m for k, m in matches.items() if m.role != "ignore"}
        score = sum(m.confidence for m in real.values())
        if score > best_score + 1e-9:
            best_idx, best_matches, best_score = idx, matches, score
    return best_idx, best_matches, best_score


def guess_header_row(rows: list[list[Any]], scan: int = HEADER_SCAN_ROWS) -> int | None:
    """Header row of a table whose headers we do not recognise at all.

    The first row (within `scan`) with >= 3 short, non-numeric, mostly distinct text
    cells that is followed by a row with content. The columns are then named by the
    customer in words we do not know -- the AI hint or the human on the «Данные» screen
    can still map them.
    """
    for idx, row in enumerate(rows[:scan]):
        texts = [t for t in _row_headers(row) if t]
        if len(texts) < 3 or len(set(texts)) < len(texts) * 0.8:
            continue
        if any(len(t) > 60 for t in texts) or any(_numeric_share([t]) == 1.0 for t in texts):
            continue
        if any(_is_status_value(t) for t in texts):
            continue
        if idx + 1 < len(rows) and not _row_blank(rows[idx + 1]):
            return idx
    return None


# --------------------------------------------------------------------------
# 4. Содержимое колонок
# --------------------------------------------------------------------------
def column_values(rows: list[list[Any]], start: int, col: int, limit: int = CONTENT_SAMPLE) -> list[str]:
    values: list[str] = []
    for row in rows[start:]:
        if col < len(row):
            text = cell_text(row[col])
            if text:
                values.append(text)
                if len(values) >= limit:
                    break
    return values


def _numeric_share(values: Sequence[str]) -> float:
    if not values:
        return 0.0
    ok = 0
    for v in values:
        try:
            float(v.replace(",", ".").replace("%", "").strip())
            ok += 1
        except ValueError:
            pass
    return ok / len(values)


def status_share(values: Sequence[str]) -> float:
    if not values:
        return 0.0
    ok = sum(1 for v in values if Status.parse(v) is not Status.UNKNOWN)
    return ok / len(values)


def delivery_share(values: Sequence[str]) -> float:
    if not values:
        return 0.0
    ok = sum(1 for v in values if Delivery.parse(v) is not None)
    return ok / len(values)


def looks_like_status(values: Sequence[str]) -> float:
    if len(set(values)) > 15:
        return 0.0
    return status_share(values)


def looks_like_delivery(values: Sequence[str]) -> float:
    if len(set(values)) > 8:
        return 0.0
    return delivery_share(values)


# --------------------------------------------------------------------------
# 5. Профиль данных (ручные поправки)
# --------------------------------------------------------------------------
def fingerprint(headers: Sequence[str], width: int) -> str:
    normed = sorted(n for n in (normalize_text(h) for h in headers) if n)
    raw = json.dumps([width if not normed else 0, normed], ensure_ascii=False)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


class DataProfile:
    """Remembered column assignments, keyed by header fingerprint and table kind."""

    VERSION = 1

    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path) if path else None
        self.entries: dict[str, dict[str, Any]] = {}
        self.preferred: dict[str, str] = {}      # вид таблицы -> «файл::лист», выбранный человеком
        self._lock = threading.Lock()
        self.load()

    @staticmethod
    def _key(kind: str, fp: str) -> str:
        return f"{kind}:{fp}"

    def load(self) -> None:
        self.entries = {}
        self.preferred = {}
        if self.path is None or not self.path.is_file():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(data, dict) and isinstance(data.get("tables"), dict):
                self.entries = data["tables"]
            if isinstance(data, dict) and isinstance(data.get("preferred"), dict):
                self.preferred = {str(k): str(v) for k, v in data["preferred"].items()}
        except (OSError, ValueError):
            self.entries = {}

    def save(self) -> None:
        if self.path is None:
            return
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            payload = {"version": self.VERSION, "tables": self.entries, "preferred": self.preferred}
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(self.path)

    def get(self, kind: str, fp: str) -> dict[str, Any] | None:
        return self.entries.get(self._key(kind, fp))

    def remember(self, kind: str, fp: str, columns: dict[str, ColumnMatch], header_row: int | None, label: str = "") -> None:
        self.entries[self._key(kind, fp)] = {
            "label": label,
            "saved_at": dt.datetime.now().isoformat(timespec="seconds"),
            "header_row": header_row,
            "columns": {
                role: {"index": m.index, "header": m.header}
                for role, m in columns.items()
                if m.role != "ignore"
            },
        }


def apply_saved_columns(saved: dict[str, Any], headers: Sequence[str]) -> dict[str, ColumnMatch]:
    """Turn a stored profile entry back into matches for a table with `headers`."""
    normed = [normalize_text(h) for h in headers]
    result: dict[str, ColumnMatch] = {}
    used: set[int] = set()
    for role, ref in (saved.get("columns") or {}).items():
        header = normalize_text(ref.get("header", ""))
        idx = None
        if header and header in normed:
            idx = normed.index(header)
        elif isinstance(ref.get("index"), int) and 0 <= ref["index"] < len(headers):
            idx = ref["index"]
        if idx is None or idx in used:
            continue
        used.add(idx)
        result[role] = ColumnMatch(role, idx, str(headers[idx]), 1.0, "profile")
    return result


# --------------------------------------------------------------------------
# 6. ИИ-подстраховка
# --------------------------------------------------------------------------
AI_SYSTEM_PROMPT = (
    "Ты помогаешь разбирать таблицы Excel из системы обучения государственных служащих. "
    "Тебе дают заголовки колонок (с номерами с нуля) и несколько строк-примеров. "
    "Определи, какая колонка какую роль играет. Каждая колонка может играть не более одной роли. "
    "Верни ТОЛЬКО JSON-объект, где ключ -- название роли, а значение -- номер колонки "
    "(целое число с нуля) или null, если такой колонки нет. Никаких пояснений, только JSON."
)

_AI_ROLE_HELP = {
    "user": "ФИО сотрудника / слушателя (или его псевдоним вроде «Пользователь 1»)",
    "person_id": "табельный номер / идентификатор сотрудника (не номер строки)",
    "position": "должность сотрудника",
    "iogv": "орган власти (ИОГВ), где работает сотрудник",
    "delivery": "формат обучения: ППК (очное, группа) или ЭК (электронный курс)",
    "title": "название курса / учебной программы",
    "status": "итог прохождения: пройден / не пройден / в процессе и т. п.",
    "annotation": "аннотация / описание курса",
    "goal": "цель и задачи курса",
    "outcomes": "ожидаемые результаты обучения (знать, уметь, владеть)",
}


_PII_HEADER_RE = re.compile(
    r"фио|ф\.и\.о|фамили|имя|отчеств|табел|снилс|инн|паспорт|e-?mail|почт|"
    r"телефон|тел|мобильн|phone|id|идентификатор|логин|login|"
    r"full.?name|first.?name|last.?name|^name$",
    re.IGNORECASE,
)
_PII_NAME_RE = re.compile(r"^[А-ЯЁ][а-яё]+(?:[\s\-][А-ЯЁ][а-яё]+){1,2}$|^[А-ЯЁ][а-яё]+\s+[А-ЯЁ]\.\s*[А-ЯЁ]?\.?$")
_PII_ID_RE = re.compile(r"^\d{6,}$")
_PII_CONTACT_RE = re.compile(r"@|^\+?\d[\d\s\-()]{8,}$")


def _is_pii_value(v: str) -> bool:
    v = v.strip()
    return bool(_PII_NAME_RE.match(v) or _PII_ID_RE.match(v) or _PII_CONTACT_RE.search(v))


def _pii_columns(headers: Sequence[str], sample_rows: Sequence[Sequence[str]]) -> set[int]:
    """Columns that look like person name / ID / email / phone, by header or by values.
    Header-based: always masked. Value-based: masked when most non-empty values look personal."""
    cols: set[int] = set()
    for i, h in enumerate(headers):
        if _PII_HEADER_RE.search(str(h)):
            cols.add(i)
    for i in range(len(headers)):
        vals = [str(r[i]) for r in sample_rows if i < len(r) and str(r[i]).strip()]
        if vals and sum(_is_pii_value(v) for v in vals) * 2 >= len(vals):
            cols.add(i)
    return cols


def _mask_cell(v: str) -> str:
    return "[скрыто]" if v and v.strip() else v


def build_ai_prompt(spec: TableSpec, headers: Sequence[str], sample_rows: Sequence[Sequence[str]]) -> str:
    pii = _pii_columns(headers, sample_rows)
    sample_rows = [
        [(_mask_cell(c) if j in pii or _is_pii_value(c) else c) for j, c in enumerate(row)]
        for row in sample_rows
    ]
    roles = {r.key: _AI_ROLE_HELP.get(r.key, r.key) for r in spec.roles if r.used}
    payload = {
        "роли": roles,
        "заголовки": {str(i): h for i, h in enumerate(headers)},
        "примеры_строк": [[(c[:80]) for c in row] for row in sample_rows],
    }
    return json.dumps(payload, ensure_ascii=False, indent=1)


def parse_ai_mapping(text: str, spec: TableSpec, width: int) -> dict[str, int]:
    """Validate the model's reply. Returns only well-formed role->column pairs."""
    match = re.search(r"\{.*\}", text or "", flags=re.DOTALL)
    if not match:
        return {}
    try:
        data = json.loads(match.group(0))
    except ValueError:
        return {}
    if not isinstance(data, dict):
        return {}
    allowed = {r.key for r in spec.roles if r.used}
    result: dict[str, int] = {}
    seen: set[int] = set()
    for role, idx in data.items():
        if isinstance(idx, str) and idx.strip().isdigit():
            idx = int(idx.strip())           # живая модель нередко отвечает "3" вместо 3
        elif isinstance(idx, float) and idx == int(idx):
            idx = int(idx)
        if role not in allowed or isinstance(idx, bool) or not isinstance(idx, int):
            continue
        if not 0 <= idx < width or idx in seen:
            continue
        seen.add(idx)
        result[role] = idx
    return result


def ai_suggest_mapping(
    llm,
    spec: TableSpec,
    headers: Sequence[str],
    rows: list[list[Any]],
    data_start: int,
    cache_dir: str | Path | None = None,
) -> dict[str, int]:
    """Ask the model for a mapping. Never raises; {} when it cannot help.

    `llm` is anything with ``complete(system, user) -> result`` where
    ``result.text`` is the reply (the product's ``LLMClient`` protocol).
    """
    if llm is None:
        return {}
    sample = [[cell_text(c) for c in row] for row in rows[data_start:data_start + 6] if not _row_blank(row)]
    user = build_ai_prompt(spec, headers, sample)
    cache_path = None
    if cache_dir is not None:
        digest = hashlib.sha256(f"{spec.kind}\x00{user}".encode("utf-8")).hexdigest()
        cache_path = Path(cache_dir) / f"{digest}.json"
        if cache_path.is_file():
            try:
                cached = json.loads(cache_path.read_text(encoding="utf-8"))
                return parse_ai_mapping(json.dumps(cached), spec, len(headers))
            except (OSError, ValueError):
                pass
    try:
        reply = llm.complete(AI_SYSTEM_PROMPT, user)
        text = getattr(reply, "text", reply)
    except Exception:  # noqa: BLE001 -- модель недоступна: просто без подсказки
        return {}
    mapping = parse_ai_mapping(str(text), spec, len(headers))
    # Проверка кодом: статус и формат должны быть похожи на статус и формат.
    for role, check in (("status", looks_like_status), ("delivery", looks_like_delivery)):
        if role in mapping:
            values = column_values(rows, data_start, mapping[role])
            if len(values) >= 3 and check(values) < 0.3:
                del mapping[role]
    if cache_path is not None and mapping:
        try:
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            cache_path.write_text(json.dumps(mapping, ensure_ascii=False), encoding="utf-8")
        except OSError:
            pass
    return mapping


# --------------------------------------------------------------------------
# 7. Разбор таблицы целиком
# --------------------------------------------------------------------------
@dataclass
class TableAnalysis:
    table: RawTable
    kind: str                              # history | registry | ignored
    reason: str
    header_row: int | None                 # индекс строки заголовка в table.rows (с 0)
    matches: dict[str, ColumnMatch]        # роль -> колонка (без служебных)
    headers: list[str]
    confidence: float
    data_rows: int
    score: float
    fingerprint: str
    missing: list[str] = field(default_factory=list)
    ignored_columns: list[ColumnMatch] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def key(self) -> str:
        return self.table.key

    @property
    def data_start(self) -> int:
        return 0 if self.header_row is None else self.header_row + 1

    def column(self, role: str) -> int | None:
        match = self.matches.get(role)
        return match.index if match else None


def _count_data_rows(rows: list[list[Any]], start: int) -> int:
    return sum(1 for r in rows[start:] if not _row_blank(r))


def _rescue_by_content(rows, start, spec: TableSpec, matches: dict[str, ColumnMatch], headers: list[str], headerless: bool) -> None:
    """Fill missing roles from cell values and column order. Mutates `matches`."""
    width = len(headers)
    taken = {m.index for m in matches.values()}
    real_taken = taken | {m.index for k, m in matches.items() if k.startswith("ignore")}
    free = [c for c in range(width) if c not in real_taken]

    def blank_header(col: int) -> bool:
        return not headers[col].strip() if col < len(headers) else True

    if spec.kind == "history":
        for role, check, floor in (("status", looks_like_status, 0.75), ("delivery", looks_like_delivery, 0.9)):
            if role in matches:
                continue
            best_col, best = None, 0.0
            for col in free:
                if not headerless and not blank_header(col) and role == "delivery":
                    # у колонки есть свой, незнакомый заголовок -- формат из неё не угадываем
                    continue
                values = column_values(rows, start, col)
                if len(values) < 3:
                    continue
                share = check(values)
                if share > best:
                    best_col, best = col, share
            if best_col is not None and best >= floor:
                matches[role] = ColumnMatch(role, best_col, headers[best_col], round(best * 0.9, 3), "content")
                free.remove(best_col)
    # порядок колонок: только если заголовка нет совсем или колонка безымянная
    pool = [c for c in free if headerless or blank_header(c)]
    missing = [r for r in spec.canonical_order if r not in matches]
    for role in missing:
        if not pool:
            break
        if not headerless and role not in spec.required:
            continue
        if headerless or role in spec.required:
            col = pool.pop(0)
            matches[role] = ColumnMatch(role, col, headers[col], 0.35, "order")


def _ignored(table: RawTable, reason: str, rows_count: int, fp: str, header_row: int | None = None) -> TableAnalysis:
    """An unused table still carries headers and a data start, so the UI can offer to use it."""
    rows = table.rows
    if header_row is None:
        header_row = next(
            (i for i, r in enumerate(rows[:HEADER_SCAN_ROWS]) if sum(1 for c in r if cell_text(c)) >= 2), None
        )
    width = table.width
    headers = _row_headers(rows[header_row]) if header_row is not None else []
    headers = headers + [""] * (width - len(headers))
    headers = [h or f"Колонка {i + 1}" for i, h in enumerate(headers)]
    return TableAnalysis(table, "ignored", reason, header_row, {}, headers, 0.0, rows_count, 0.0, fp)


def analyze_table(
    table: RawTable,
    *,
    profile: DataProfile | None = None,
    override: dict[str, Any] | None = None,
    llm=None,
    ai_cache_dir: str | Path | None = None,
    prefer_kind: str | None = None,
) -> TableAnalysis:
    """Decide what `table` is and which column plays which role. Never raises."""
    rows = table.rows
    width = table.width
    nonblank = [r for r in rows if not _row_blank(r)]
    if len(nonblank) < 2 or width < 2:
        reason = "пустой лист" if not nonblank else "слишком мало данных (нужна шапка и хотя бы одна строка)"
        return _ignored(table, reason, len(nonblank), "")

    # --- лучший вариант заголовка для каждого типа таблицы
    trial: dict[str, tuple[int | None, dict[str, ColumnMatch], float]] = {
        kind: find_header_row(rows, spec) for kind, spec in SPECS.items()
    }
    # отпечаток считаем по строке заголовка более «богатого» варианта
    for kind in list(trial):
        if trial[kind][2] <= 0 and trial[kind][0] is None:
            guess = guess_header_row(rows)
            if guess is not None and kind == "history":
                trial[kind] = (guess, {}, 0.0)
    fp_row = max(trial.values(), key=lambda t: t[2])[0]
    fp = fingerprint(_row_headers(rows[fp_row]) if fp_row is not None else [], width)

    best_kind = max(trial, key=lambda k: trial[k][2])
    ai_kind = prefer_kind if prefer_kind in SPECS else (best_kind if trial[best_kind][2] > 0 else "history")
    if override and override.get("kind") in SPECS:
        ai_kind = override["kind"]

    def evaluate(kind: str) -> TableAnalysis | None:
        spec = SPECS[kind]
        header_row, matches, score = trial[kind]
        ov = (override or {}) if (override or {}).get("kind") == kind else {}
        if ov.get("header_row") is not None:
            header_row = int(ov["header_row"])
            matches = assign_by_headers(_row_headers(rows[header_row]), spec) if 0 <= header_row < len(rows) else {}
        headerless = header_row is None
        headers = _row_headers(rows[header_row]) if header_row is not None else [f"Колонка {i + 1}" for i in range(width)]
        headers = headers + [""] * (width - len(headers))
        start = 0 if header_row is None else header_row + 1
        real = {k: m for k, m in matches.items() if not k.startswith("ignore")}
        ignored = [m for k, m in matches.items() if k.startswith("ignore")]
        notes: list[str] = []

        # профиль: ранее сохранённое сопоставление для такой же шапки
        if profile is not None and not ov.get("columns"):
            saved = profile.get(kind, fp)
            if saved:
                restored = apply_saved_columns(saved, headers)
                if restored:
                    real = restored
                    notes.append("Применён сохранённый профиль данных для такой шапки.")

        # «Результат, %» -- это баллы, а не статус: числовую колонку статуса убираем, настоящий ищем по значениям
        st = real.get("status")
        if st is not None and st.method not in ("manual", "profile") and not ov.get("columns"):
            vals = column_values(rows, start, st.index)
            if len(vals) >= 3 and _numeric_share(vals) >= 0.8:
                del real[ "status"]
                notes.append(f"Колонка «{st.header}» похожа на баллы (числа), а не на статус: не использована как статус.")
                ignored.append(ColumnMatch("ignore", st.index, st.header, 1.0, "content"))

        if headerless or len(real) < 2:
            # заголовка нет или он не опознан: всё по содержимому и порядку
            real = dict(real)
            _rescue_by_content(rows, start if not headerless else 0, spec, real, headers, headerless=True)
            if headerless:
                header_row = None
                start = 0
        else:
            _rescue_by_content(rows, start, spec, real, headers, headerless=False)

        # ручная правка перекрывает всё
        if ov.get("columns"):
            real = {k: v for k, v in real.items() if k not in ov["columns"]}
            for role, idx in ov["columns"].items():
                if idx is None or not isinstance(idx, int) or not 0 <= idx < width:
                    continue
                for other, m in list(real.items()):
                    if m.index == idx:
                        del real[other]
                real[role] = ColumnMatch(role, idx, headers[idx], 1.0, "manual")

        # ИИ-подстраховка: нет обязательной колонки, или уверенность низкая
        weak = [r for r in spec.required if r not in real or real[r].confidence < 0.5]
        if llm is not None and weak and kind == ai_kind and not ov.get("columns") and width >= 3 and len(nonblank) >= 4:
            # догадки «по порядку колонок» -- самые слабые: модель их перекрывает
            guesses = {k: v for k, v in real.items() if v.method == "order"}
            firm = {k: v for k, v in real.items() if v.method != "order"}
            suggestion = ai_suggest_mapping(llm, spec, headers, rows, start, ai_cache_dir)
            if suggestion:
                real = firm
                taken = {m.index for k, m in real.items() if k not in weak}
                for role, idx in suggestion.items():
                    if role in real and role not in weak:
                        continue
                    if idx in taken:
                        continue
                    real[role] = ColumnMatch(role, idx, headers[idx], 0.6, "ai")
                    taken.add(idx)
                for role, guess in guesses.items():          # что модель не покрыла -- возвращаем как было
                    if role not in real and guess.index not in {m.index for m in real.values()}:
                        real[role] = guess

        have = set(real)
        if kind == "registry" and header_row is None and not ov.get("columns"):
            return None  # реестр без шапки не угадать: у него нет «узнаваемого» содержимого
        if not all(r in have for r in spec.required):
            return None
        if spec.needs_any and not (have & set(spec.needs_any)) and not ov.get("columns"):
            return None
        if kind == "registry" and not ov.get("columns") and not any(
            real[r].method != "order" for r in spec.needs_any if r in real
        ):
            return None  # «Фильтр | все программы…» на служебном листе -- не реестр: описание угадано лишь по порядку
        # слишком слабо: одна из обязательных только «по порядку» без подтверждения содержимым
        conf_values = [real[r].confidence for r in spec.required]
        confidence = sum(conf_values) / len(conf_values)
        if headerless and kind == "history" and "status" not in real:
            return None
        missing = [r.key for r in spec.roles if r.used and r.key not in real]
        score_total = sum(m.confidence for m in real.values())
        return TableAnalysis(
            table=table,
            kind=kind,
            reason="",
            header_row=header_row,
            matches=real,
            headers=headers,
            confidence=round(confidence, 3),
            data_rows=_count_data_rows(rows, start),
            score=score_total,
            fingerprint=fp,
            missing=missing,
            ignored_columns=ignored,
            notes=notes,
        )

    order = [prefer_kind] if prefer_kind in SPECS else ["history", "registry"]
    if override and override.get("kind") in SPECS:
        order = [override["kind"]]
    if override and override.get("kind") == "ignored":
        return _ignored(table, "отключено вручную", len(nonblank), fp)
    results = [r for r in (evaluate(k) for k in order) if r is not None]
    if not results:
        header_row, matches, _score = max(trial.values(), key=lambda t: t[2])
        found = [ROLE_LABELS.get(k, k) for k, m in matches.items() if not k.startswith("ignore")]
        reason = (
            "не похоже ни на историю обучения, ни на реестр курсов"
            + (f" (узнаны только колонки: {', '.join(found)})" if found else " (нет знакомых заголовков)")
        )
        return _ignored(table, reason, len(nonblank), fp, header_row)
    # история и реестр одновременно: побеждает больший счёт, при равенстве -- история
    results.sort(key=lambda a: (a.score, a.kind == "history"), reverse=True)
    return results[0]


def column_samples(analysis: TableAnalysis, col: int, n: int = 3) -> list[str]:
    return column_values(analysis.table.rows, analysis.data_start, col, limit=n)


def to_table_info(analysis: TableAnalysis, used: bool, reason: str = "") -> TableInfo:
    from ingest.passport import KIND_LABELS

    columns: list[ColumnInfo] = []
    for role, m in sorted(analysis.matches.items(), key=lambda kv: kv[1].index):
        columns.append(
            ColumnInfo(
                role=role,
                role_label=ROLE_LABELS.get(role, role),
                index=m.index,
                header=m.header,
                confidence=m.confidence,
                method=m.method,
                method_label=METHOD_LABELS.get(m.method, m.method),
                samples=column_samples(analysis, m.index),
            )
        )
    return TableInfo(
        key=analysis.key,
        file=analysis.table.file,
        sheet=analysis.table.sheet,
        kind=analysis.kind,
        kind_label=KIND_LABELS.get(analysis.kind, analysis.kind),
        used=used,
        reason=reason or analysis.reason,
        confidence=analysis.confidence,
        header_row=None if analysis.header_row is None else analysis.header_row + 1,
        data_rows=analysis.data_rows,
        columns=columns,
        all_headers=analysis.headers,
        previews=[column_samples(analysis, i) for i in range(len(analysis.headers))],
        missing_roles=analysis.missing,
        notes=list(analysis.table.notes) + list(analysis.notes),
    )


def iter_data_rows(analysis: TableAnalysis) -> Iterable[tuple[int, list[Any]]]:
    """(1-based sheet row number, row) for every non-blank data row."""
    header_norm = None
    if analysis.header_row is not None:
        header_norm = [normalize_text(c) for c in analysis.table.rows[analysis.header_row]]
    for offset, row in enumerate(analysis.table.rows[analysis.data_start:], start=analysis.data_start):
        if _row_blank(row):
            continue
        if header_norm is not None and [normalize_text(c) for c in row] == header_norm:
            continue  # повторная шапка посреди данных (разрыв страницы)
        yield offset + 1, row
