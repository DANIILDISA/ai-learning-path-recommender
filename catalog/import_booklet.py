"""Импорт нового буклета / каталога программ в `programs.yaml`.

Три шага, как и во всём продукте: сначала код, потом модель на неясное, потом человек.

1. **Код.** PDF-буклет (карточки программ + «Навигатор», см. `catalog.booklet_pdf`) или таблица
   (xlsx / xls / csv / docx с таблицей) -> список программ: название, часы, форма, категория
   слушателей (грейд), аннотация, темы, треки «Навигатора».
2. **Модель (GigaChat) -- только на неясное**, ответ строго JSON, проверяется кодом, кэш на диске:
   регистр названий, не подтверждённых «Навигатором»; поля формы/категории, которые код не разобрал;
   сопоставление строк «Навигатора» с карточками при расхождении в формулировке. Без ключа шаг
   пропускается -- результат хуже, но импорт работает.
3. **Человек.** Отчёт «было / стало» (что добавилось, пропало, изменилось), потом «Применить».
   Прежний каталог уходит в резерв (`output/catalog_backup/`), откат возвращает его.

При любой ошибке рабочий каталог остаётся прежним: новый файл сначала проверяется загрузчиком
`load_programmes`, и только потом атомарно заменяет рабочий.

Поставляемый `catalog/programs.yaml` не перезаписывается никогда: принятый каталог лежит в
`output/programs.yaml` и перекрывает его, откат удаляет перекрытие.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import zipfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

from catalog.booklet_pdf import alnum_casefold, fallback_case, match_key, resolve_track
from catalog.titles import known_spellings, load_title_spellings, repair_glued

PDF_SUFFIXES = {".pdf"}
TABLE_SUFFIXES = {".xlsx", ".xlsm", ".xls", ".csv", ".tsv", ".txt"}
DOCX_SUFFIXES = {".docx"}
SUPPORTED = PDF_SUFFIXES | TABLE_SUFFIXES | DOCX_SUFFIXES
MAX_BOOKLET_BYTES = 60 * 1024 * 1024


class BookletError(ValueError):
    """Проблема, понятная менеджеру (готовая русская фраза)."""


@dataclass
class ImportResult:
    source: str
    kind: str                                   # pdf | table
    programmes: list[dict[str, Any]] = field(default_factory=list)
    navigator: dict[str, list[str]] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    stats: dict[str, Any] = field(default_factory=dict)
    llm_calls: int = 0
    llm_tokens: int = 0

    def warn(self, text: str) -> None:
        if text not in self.warnings:
            self.warnings.append(text)


# ------------------------------------------------------------------ модель
SYSTEM_JSON = (
    "Ты помогаешь разбирать буклет образовательных программ для государственных служащих. "
    "Отвечай ТОЛЬКО JSON-объектом без пояснений и без markdown."
)


class _Ask:
    """Вызов модели со строгой проверкой и кэшем на диске. Никогда не бросает исключение."""

    def __init__(self, llm, cache_dir: Path | None, result: ImportResult):
        self.llm = llm
        self.cache_dir = Path(cache_dir) if cache_dir else None
        self.result = result

    def json(self, system: str, user: str) -> dict | None:
        if self.llm is None:
            return None
        digest = hashlib.sha256(f"{system}\x00{user}".encode("utf-8")).hexdigest()
        path = self.cache_dir / f"{digest}.json" if self.cache_dir else None
        if path is not None and path.is_file():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    return data
            except (OSError, ValueError):
                pass
        try:
            reply = self.llm.complete(system, user)
        except Exception:  # noqa: BLE001 -- модель недоступна: идём дальше без неё
            self.result.warn("Модель недоступна: неясные поля заполнены кодом, без подсказки ИИ.")
            return None
        self.result.llm_calls += 1
        self.result.llm_tokens += int(getattr(reply, "prompt_tokens", 0) or 0) + int(
            getattr(reply, "completion_tokens", 0) or 0
        )
        text = str(getattr(reply, "text", reply) or "")
        match = re.search(r"\{.*\}", text, flags=re.DOTALL)
        if not match:
            return None
        try:
            data = json.loads(match.group(0))
        except ValueError:
            return None
        if not isinstance(data, dict):
            return None
        if path is not None:
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            except OSError:
                pass
        return data


def _restore_case(ask: _Ask, programmes: list[dict[str, Any]], result: ImportResult) -> None:
    """Названия капсом, которых нет в «Навигаторе», -> нормальный регистр (проверка: те же буквы)."""
    todo = [(i, p) for i, p in enumerate(programmes) if p.get("_caps_title")]
    if not todo or ask.llm is None:
        return
    fixed = 0
    for start in range(0, len(todo), 12):
        chunk = todo[start:start + 12]
        user = json.dumps(
            {
                "задача": "Верни каждое название программы в обычном регистре: заглавная только первая буква, "
                          "а имена собственные и аббревиатуры (ИОГВ, ГИС, Санкт-Петербург, Excel) оставь как принято. "
                          "Буквы, слова и порядок слов менять НЕЛЬЗЯ.",
                "названия": [{"i": i, "caps": p["_caps_title"]} for i, p in chunk],
                "формат_ответа": {"items": [{"i": 0, "title": "..."}]},
            },
            ensure_ascii=False,
        )
        data = ask.json(SYSTEM_JSON, user)
        for item in (data or {}).get("items", []) if isinstance(data, dict) else []:
            if not isinstance(item, dict) or not isinstance(item.get("i"), int) or not isinstance(item.get("title"), str):
                continue
            idx = item["i"]
            prog = next((p for i, p in chunk if i == idx), None)
            if prog is None:
                continue
            candidate = " ".join(item["title"].split())
            # проверка кодом: те же буквы в том же порядке, и это уже не капс
            if match_key(candidate) == match_key(prog["_caps_title"]) and candidate != candidate.upper():
                prog["title"] = candidate
                fixed += 1
    if fixed:
        result.stats["titles_case_restored_by_ai"] = fixed


def _fill_card_fields(ask: _Ask, programmes: list[dict[str, Any]], result: ImportResult) -> None:
    """Форма обучения / категория слушателей, которые код не вытащил из правой колонки карточки."""
    if ask.llm is None:
        return
    filled = 0
    for p in programmes:
        raw = p.get("_right_text") or ""
        if not raw or (p.get("study_form") and p.get("audience")):
            continue
        user = json.dumps(
            {
                "задача": "Из текста правой колонки карточки программы достань форму обучения и категорию слушателей. "
                          "Значения копируй из текста дословно. Если в тексте их нет -- null.",
                "текст": raw[:600],
                "формат_ответа": {"study_form": "строка или null", "audience": "строка или null"},
            },
            ensure_ascii=False,
        )
        data = ask.json(SYSTEM_JSON, user)
        if not isinstance(data, dict):
            continue
        flat = alnum_casefold(raw)
        for key in ("study_form", "audience"):
            value = data.get(key)
            if p.get(key) or not isinstance(value, str) or not value.strip():
                continue
            value = " ".join(value.split())
            if alnum_casefold(value) and alnum_casefold(value) in flat:     # дословно из текста
                p[key] = value
                filled += 1
    if filled:
        result.stats["fields_filled_by_ai"] = filled


# ------------------------------------------------------------------ PDF
def _from_pdf(path: Path, ask: _Ask, result: ImportResult) -> None:
    from catalog import booklet_pdf as bp

    try:
        raw = bp.read_pdf(path)
    except ImportError as exc:
        raise BookletError("Для чтения PDF нужна библиотека pdfplumber (pip install pdfplumber).") from exc
    except ValueError as exc:
        raise BookletError(str(exc)) from exc
    cards = raw["cards"]
    result.stats.update(pages=raw["pages"], cards_found=len(cards), artefacts_discarded=raw["discarded"])
    if not cards:
        raise BookletError(
            "В PDF не найдено ни одной карточки программы. Разбор рассчитан на буклет, где у каждой программы есть "
            "строка «Группа / Подгруппа», блок «КЛЮЧЕВЫЕ ТЕМЫ» и поля «ТРУДОЁМКОСТЬ / ФОРМА ОБУЧЕНИЯ / КАТЕГОРИЯ "
            "СЛУШАТЕЛЕЙ». Если буклет оформлен иначе, пришлите каталог таблицей Excel (колонки: название, часы, "
            "форма, категория слушателей, описание). Текущий каталог программ остался без изменений."
        )
    by_key = {match_key(c.title): c for c in cards}
    display: dict[str, str] = {}
    card_tracks: dict[str, list[str]] = {}
    navigator: dict[str, list[str]] = {t: [] for t in raw["navigator"]}
    unmatched: list[tuple[str, str]] = []
    for track, entries in raw["navigator"].items():
        for entry in entries:
            key = match_key(entry)
            if key not in by_key:
                unmatched.append((track, entry))
                continue
            display.setdefault(key, entry)
            navigator[track].append(entry)
            tracks = card_tracks.setdefault(key, [])
            if track not in tracks:
                tracks.append(track)

    # строки «Навигатора» без карточки: сходство в коде, при сомнении -- модель (выбор из кандидатов)
    still_unmatched: list[str] = []
    if unmatched:
        from rapidfuzz import fuzz

        titles = [(k, c.title) for k, c in by_key.items()]
        for track, entry in unmatched:
            scored = sorted(((fuzz.ratio(match_key(entry), k) / 100.0, k) for k, _t in titles), reverse=True)[:3]
            best_score, best_key = scored[0]
            chosen = best_key if best_score >= 0.93 else None
            if chosen is None and best_score >= 0.6 and ask.llm is not None:
                options = [by_key[k].title for _s, k in scored]
                data = ask.json(
                    SYSTEM_JSON,
                    json.dumps(
                        {
                            "задача": "Строка из раздела «Навигатор» -- это название одной из программ-кандидатов "
                                      "(формулировка могла немного отличаться)? Верни номер кандидата или null, "
                                      "если ни одна не подходит.",
                            "строка": entry,
                            "кандидаты": {str(i): t for i, t in enumerate(options)},
                            "формат_ответа": {"candidate": "номер или null"},
                        },
                        ensure_ascii=False,
                    ),
                )
                pick = (data or {}).get("candidate")
                if isinstance(pick, str) and pick.strip().isdigit():
                    pick = int(pick.strip())
                if isinstance(pick, int) and not isinstance(pick, bool) and 0 <= pick < len(scored):
                    chosen = scored[pick][1]
                    result.stats["navigator_links_by_ai"] = result.stats.get("navigator_links_by_ai", 0) + 1
            if chosen is None:
                still_unmatched.append(f"{track}: {entry}")
                continue
            display.setdefault(chosen, entry)
            navigator[track].append(entry)
            tracks = card_tracks.setdefault(chosen, [])
            if track not in tracks:
                tracks.append(track)
    if still_unmatched:
        result.stats["navigator_unmatched"] = still_unmatched
        result.warn(
            f"Строк «Навигатора» без карточки программы: {len(still_unmatched)} "
            "(формулировка расходится с названием в карточке). Список в отчёте."
        )

    seen: dict[str, int] = {}
    for c in cards:
        key = match_key(c.title)
        if key in seen:
            result.warn(f"Программа «{display.get(key) or fallback_case(c.title)}» встречается в буклете дважды (стр. {c.page}): взята первая.")
            continue
        seen[key] = 1
        confirmed = key in display
        title = display.get(key) or fallback_case(c.title)
        result.programmes.append({
            "title": title,
            "page": c.page,
            "competency_group": c.group,
            "competency_subgroup": c.subgroup,
            "hours": c.hours,
            "study_form": c.study_form,
            "audience": c.audience,
            "annotation": c.annotation,
            "topics": c.topics,
            "navigator_tracks": card_tracks.get(key, []),
            "_caps_title": None if confirmed else c.title,
            "_right_text": c.right_text,
        })
    result.navigator = navigator
    result.stats["navigator_entries"] = sum(len(v) for v in navigator.values())
    _restore_case(ask, result.programmes, result)
    _fill_card_fields(ask, result.programmes, result)
    no_nav = [p["title"] for p in result.programmes if not p["navigator_tracks"]]
    if no_nav:
        result.warn(f"Программ без трека «Навигатора»: {len(no_nav)} (они не попадут в треки по грейдам): {no_nav[0]}" + ("…" if len(no_nav) > 1 else ""))
    if not navigator or not result.stats["navigator_entries"]:
        result.warn("Раздел «Навигатор» не найден: у программ нет треков, подборка по грейдам работать не будет.")


# ------------------------------------------------------------------ таблицы
def _docx_tables(path: Path):
    """Таблицы Word без внешних библиотек: document.xml -> RawTable."""
    from ingest.smart_table import RawTable

    ns = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
    import xml.etree.ElementTree as ET

    try:
        with zipfile.ZipFile(path) as z:
            root = ET.fromstring(z.read("word/document.xml"))
    except Exception as exc:  # noqa: BLE001
        raise BookletError(f"Файл «{path.name}» не удалось открыть как документ Word: {exc}") from exc
    tables = []
    for n, tbl in enumerate(root.iter(f"{ns}tbl"), start=1):
        rows = []
        for tr in tbl.findall(f"{ns}tr"):
            row = []
            for tc in tr.findall(f"{ns}tc"):
                paras = ["".join(t.text or "" for t in p.iter(f"{ns}t")) for p in tc.iter(f"{ns}p")]
                row.append(" ".join(x for x in paras if x).strip() or None)
            rows.append(row)
        width = max((len(r) for r in rows), default=0)
        rows = [r + [None] * (width - len(r)) for r in rows]
        tables.append(RawTable(path.name, f"таблица {n}", rows))
    return tables


def _catalog_spec():
    from ingest.normalize import normalize_text
    from ingest.smart_table import RoleSpec, TableSpec

    return TableSpec(
        kind="catalog",
        required=("title",),
        needs_any=("hours", "annotation", "study_form", "audience"),
        canonical_order=("title",),
        roles=(
            RoleSpec("title", (
                "название программы", "наименование программы", "программа", "название", "наименование",
                "название курса", "наименование курса", "курс", "образовательная программа", "учебная программа",
                "название образовательной программы", "наименование образовательной программы",
                "наименование электронного курса", "название электронного курса",
            ), exact_only=("тема",)),
            RoleSpec("hours", (
                "трудоемкость", "трудоемкость ч", "трудоемкость часы", "часы", "часов", "количество часов",
                "объем часов", "объем", "продолжительность", "продолжительность ч", "ак часов", "академические часы",
                "hours",
            )),
            RoleSpec("study_form", (
                "форма обучения", "форма", "формат обучения", "формат", "форма проведения", "вид обучения",
            )),
            RoleSpec("audience", (
                "категория слушателей", "категория", "целевая аудитория", "аудитория", "для кого", "слушатели",
                "категория обучающихся", "категория работников", "кому адресована",
            )),
            RoleSpec("annotation", (
                "аннотация", "описание", "краткое описание", "описание программы", "описание курса",
                "о программе", "содержание",
            )),
            RoleSpec("topics", ("ключевые темы", "темы", "основные темы", "содержание программы", "тематика")),
            RoleSpec("goal", ("цель", "цели", "цель обучения", "цель программы", "цель задачи")),
            RoleSpec("group", (
                "группа компетенций", "компетенция", "группа", "направление", "раздел", "блок",
            )),
            RoleSpec("subgroup", ("подгруппа", "подгруппа компетенций", "подраздел", "тематика направления")),
            RoleSpec("tracks", (
                "навигатор", "трек", "треки", "треки навигатора", "траектория", "траектории", "для категорий",
            )),
            RoleSpec("type", ("тип", "вид программы", "тип программы", "ппк эк", "ппк/эк")),
            RoleSpec("ignore", ("код", "id", "идентификатор", "номер", "№", "n", "п п", "стр", "страница", "ссылка"), used=False),
        ),
    )


_TRUTHY = {"+", "да", "x", "х", "v", "✓", "✔", "1", "true", "yes", "есть", "y"}


def _split_values(text: str) -> list[str]:
    return [p.strip() for p in re.split(r"[;\n|]+|,\s(?=[А-ЯA-Z])", text) if p.strip()]


def _hours_of(text: str) -> int | None:
    m = re.search(r"\d+(?:[.,]\d+)?", text or "")
    if not m:
        return None
    value = float(m.group(0).replace(",", "."))
    return int(round(value)) if 0 < value <= 2000 else None


def _from_tables(tables, ask: _Ask, result: ImportResult) -> None:
    from ingest.smart_table import (
        ACCEPT, _row_blank, assign_by_headers, ai_suggest_mapping, cell_text, find_header_row,
    )

    spec = _catalog_spec()
    best = None
    for table in tables:
        if len([r for r in table.rows if not _row_blank(r)]) < 2:
            continue
        header_row, matches, score = find_header_row(table.rows, spec)
        if header_row is None or "title" not in matches:
            continue
        data = sum(1 for r in table.rows[header_row + 1:] if not _row_blank(r))
        key = (score, data)
        if best is None or key > best[0]:
            best = (key, table, header_row, matches)
    if best is None:
        # последняя надежда -- модель по заголовкам первой непустой таблицы
        for table in tables:
            rows = [r for r in table.rows if not _row_blank(r)]
            if len(rows) >= 3 and ask.llm is not None:
                headers = [cell_text(c) for c in rows[0]]
                mapping = ai_suggest_mapping(ask.llm, spec, headers, rows, 1, ask.cache_dir)
                if "title" in mapping:
                    from ingest.smart_table import ColumnMatch

                    matches = {r: ColumnMatch(r, i, headers[i], 0.6, "ai") for r, i in mapping.items()}
                    best = (None, table, table.rows.index(rows[0]), matches)
                    result.warn("Колонки каталога определены подсказкой ИИ: проверьте отчёт.")
                    break
    if best is None:
        raise BookletError(
            "В файле не найдена таблица каталога программ: нужна колонка с названием программы "
            "(«Название программы», «Наименование», «Программа») и хотя бы одна из колонок «Часы», «Форма обучения», "
            "«Категория слушателей», «Описание». Текущий каталог программ остался без изменений."
        )
    _key, table, header_row, matches = best
    headers = [cell_text(c) for c in table.rows[header_row]]
    col = {role: m.index for role, m in matches.items() if not role.startswith("ignore")}
    result.stats["table"] = f"{table.file} / {table.sheet}"
    result.stats["columns"] = {role: headers[i] for role, i in col.items()}

    # колонки-«галочки» по трекам: заголовок разрешается в имя трека, в ячейках +/да/1
    track_cols: dict[int, str] = {}
    for i, h in enumerate(headers):
        if not h or col.get("tracks") == i:
            continue
        track = resolve_track(h)
        canon = {"Специалисты", "Руководители главной и ведущей групп должностей", "Руководители высшей группы должностей",
                 "Команды изменений", "Впервые назначенные руководителем",
                 "Впервые поступившие на государственную гражданскую службу", "Рейтинг цифровой зрелости ИОГВ"}
        if track in canon:
            track_cols[i] = track
    if track_cols:
        col = {role: i for role, i in col.items() if i not in track_cols or role == "title"}     # заголовок-трек не роль
        result.stats["track_columns"] = {headers[i]: t for i, t in track_cols.items()}

    def get(row, role):
        i = col.get(role)
        return cell_text(row[i]) if i is not None and i < len(row) else ""

    skipped_ek = 0
    seen: set[str] = set()
    navigator: dict[str, list[str]] = {}
    for offset, row in enumerate(table.rows[header_row + 1:], start=header_row + 2):
        if _row_blank(row):
            continue
        title = " ".join(get(row, "title").split())
        if not title or len(title) < 4:
            if sum(1 for c in row if cell_text(c)) > 1:
                result.warn(f"Строка {offset}: нет названия программы -- пропущена.")
            continue
        kind = alnum_casefold(get(row, "type"))
        if kind.startswith(("эк", "электрон")):
            skipped_ek += 1
            continue
        key = match_key(title)
        if key in seen:
            result.warn(f"Программа «{title}» повторяется (строка {offset}): взята первая.")
            continue
        seen.add(key)
        tracks: list[str] = []
        for part in _split_values(get(row, "tracks")):
            t = resolve_track(part)
            if t and t not in tracks:
                tracks.append(t)
        for i, t in track_cols.items():
            if i < len(row) and cell_text(row[i]).casefold() in _TRUTHY and t not in tracks:
                tracks.append(t)
        for t in tracks:
            navigator.setdefault(t, []).append(title)
        topics_raw = get(row, "topics")
        annotation = get(row, "annotation") or get(row, "goal")
        result.programmes.append({
            "title": title,
            "page": offset,
            "competency_group": get(row, "group") or None,
            "competency_subgroup": get(row, "subgroup") or None,
            "hours": _hours_of(get(row, "hours")),
            "study_form": get(row, "study_form") or None,
            "audience": get(row, "audience") or None,
            "annotation": annotation or None,
            "topics": [t.strip(" •-") for t in re.split(r"[\n;•]+", topics_raw) if t.strip(" •-")] if topics_raw else [],
            "navigator_tracks": tracks,
            "_caps_title": title if title == title.upper() and any(c.isalpha() for c in title) else None,
            "_right_text": "",
        })
    result.navigator = navigator
    if skipped_ek:
        result.warn(
            f"Пропущено строк с типом «ЭК» (электронные курсы): {skipped_ek}. Электронные курсы загружаются как реестр "
            "на экране «Данные», а не как каталог программ."
        )
    if not result.programmes:
        raise BookletError(
            "В таблице нет ни одной программы" + (" (все строки -- электронные курсы: это реестр ЭК, его загружают как данные)." if skipped_ek else ".")
            + " Текущий каталог программ остался без изменений."
        )
    for p in result.programmes:
        if p["title"] == p["title"].upper() and p["_caps_title"]:
            p["title"] = fallback_case(p["title"])
    if not any(p["navigator_tracks"] for p in result.programmes):
        result.warn("В таблице нет колонки с треками «Навигатора»: у программ не будет треков, подборка по грейдам работать не будет.")
    if not any(p["hours"] for p in result.programmes):
        result.warn("В таблице не найдены часы (трудоёмкость).")
    _restore_case(ask, result.programmes, result)


# ------------------------------------------------------------------ вход
def repair_glued_titles(result: ImportResult, reference_titles=None) -> None:
    """Вернуть пробелы в слитных названиях («Интенсивпо ...»), если раздельное написание подтверждено.

    Подтверждение -- `catalog/title_spellings.yaml` и `reference_titles` (названия из выгрузки истории и
    реестра, которые передаёт приложение). Название меняется только если отличается от подтверждённого
    одними пробелами; всё остальное остаётся как в буклете. Без `reference_titles` результат зависит
    только от буклета и поставляемого файла, поэтому импорт остаётся воспроизводимым.
    """
    from core import paths as _paths

    known = known_spellings(
        [*load_title_spellings(_paths.CATALOG_DIR / "title_spellings.yaml"), *(reference_titles or [])]
    )
    renamed: dict[str, str] = {}
    for programme in result.programmes:
        old = programme["title"]
        new = repair_glued(old, known)
        if new != old:
            programme["title"] = new
            renamed[old] = new
            result.warn(f"Название «{old}» записано слитно; исправлено на «{new}» (так оно написано в выгрузке).")
    if renamed:
        for track, titles in result.navigator.items():
            result.navigator[track] = [renamed.get(title, title) for title in titles]


def import_source(
    path: str | Path, llm=None, cache_dir: str | Path | None = None, reference_titles=None
) -> ImportResult:
    """Файл -> результат импорта. Бросает `BookletError` с русской фразой; рабочий каталог не трогает.

    `reference_titles` -- названия из выгрузки истории и реестра: по ним чинятся слитные слова в названиях.
    """
    from ingest.smart_table import ReadError, read_file

    path = Path(path)
    if not path.is_file():
        raise BookletError(f"Файл не найден: {path.name}")
    if path.stat().st_size > MAX_BOOKLET_BYTES:
        raise BookletError("Файл слишком большой (больше 60 МБ).")
    suffix = path.suffix.lower()
    result = ImportResult(source=path.name, kind="pdf" if suffix in PDF_SUFFIXES else "table")
    ask = _Ask(llm, cache_dir, result)
    if suffix in PDF_SUFFIXES:
        _from_pdf(path, ask, result)
    elif suffix in DOCX_SUFFIXES:
        tables = _docx_tables(path)
        if not tables:
            raise BookletError("В документе Word нет таблиц с программами. Пришлите буклет PDF или каталог таблицей Excel.")
        _from_tables(tables, ask, result)
    elif suffix in TABLE_SUFFIXES:
        try:
            tables = read_file(path)
        except ReadError as exc:
            raise BookletError(str(exc)) from exc
        _from_tables(tables, ask, result)
    else:
        raise BookletError(
            f"Формат «{suffix or 'без расширения'}» не поддерживается. Принимаются: PDF-буклет, Excel (.xlsx, .xls), CSV, Word (.docx с таблицей)."
        )
    repair_glued_titles(result, reference_titles)
    result.stats["programmes"] = len(result.programmes)
    return result


# ------------------------------------------------------------------ yaml
_YAML_HEADER = (
    "# Каталог программ, загруженный из файла «{source}» ({when}).\n"
    "# Создан импортом буклета (catalog/import_booklet.py). Поставляемый catalog/programs.yaml не изменён;\n"
    "# откат на экране «Данные» возвращает прежний каталог.\n"
)

_PUBLIC_FIELDS = ("title", "page", "competency_group", "competency_subgroup", "hours", "study_form",
                  "audience", "annotation", "topics", "navigator_tracks")


def to_yaml_document(result: ImportResult) -> dict[str, Any]:
    programmes = [{k: p.get(k) for k in _PUBLIC_FIELDS} for p in result.programmes]
    pages = [p["page"] for p in programmes if isinstance(p.get("page"), int)]
    navigator: dict[str, list[str]] = {}
    for p in programmes:
        for t in p["navigator_tracks"] or []:
            navigator.setdefault(t, []).append(p["title"])
    return {
        "source": result.source,
        "extracted_pages": [min(pages), max(pages)] if pages and result.kind == "pdf" else None,
        "programmes": programmes,
        "navigator": navigator or result.navigator,
    }


_SHIPPED_HEADER = (
    "# Каталог программ, полученный из буклета «{source}» импортом (catalog/import_booklet.py, без ИИ).\n"
    "# НЕ РЕДАКТИРОВАТЬ ВРУЧНУЮ: источник правды -- буклет; при расхождении чинить импортёр.\n"
    "# Пересборка: python main.py --import-booklet <буклет.pdf> --apply  (результат детерминирован).\n"
)


def dump_yaml(result: ImportResult, shipped: bool = False) -> str:
    """YAML каталога. `shipped=True` -- детерминированный вид для поставляемого catalog/programs.yaml (без даты)."""
    body = yaml.safe_dump(to_yaml_document(result), allow_unicode=True, sort_keys=False, width=100)
    if shipped:
        return _SHIPPED_HEADER.format(source=result.source) + body
    return _YAML_HEADER.format(source=result.source, when=datetime.now().strftime("%Y-%m-%d %H:%M")) + body


# ------------------------------------------------------------------ сравнение
def load_catalog_doc(path: str | Path | None) -> dict[str, Any]:
    if path is None or not Path(path).is_file():
        return {"programmes": [], "navigator": {}}
    return yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {"programmes": [], "navigator": {}}


def _tracks_by_key(doc: dict[str, Any]) -> dict[str, set[str]]:
    out: dict[str, set[str]] = {}
    for track, titles in (doc.get("navigator") or {}).items():
        for t in titles or []:
            out.setdefault(match_key(t), set()).add(track)
    for p in doc.get("programmes") or []:
        for track in p.get("navigator_tracks") or []:
            out.setdefault(match_key(p["title"]), set()).add(track)
    return out


_COMPARED = ("hours", "study_form", "audience", "annotation", "competency_group")


def diff_catalogs(old: dict[str, Any], new: dict[str, Any]) -> dict[str, Any]:
    """Было / стало: добавлено, пропало, изменено (по полям и трекам) + точность и полнота."""
    old_p = {match_key(p["title"]): p for p in old.get("programmes") or []}
    new_p = {match_key(p["title"]): p for p in new.get("programmes") or []}
    old_t, new_t = _tracks_by_key(old), _tracks_by_key(new)
    # переформулированное название (пара «было-стало» с сходством >= 0.9) -- та же программа, а не «добавлена + пропала»
    from rapidfuzz import fuzz

    pair: dict[str, str] = {k: k for k in new_p.keys() & old_p.keys()}      # ключ нового -> ключ старого
    left_new = [k for k in new_p if k not in old_p]
    left_old = [k for k in old_p if k not in new_p]
    for kn in left_new:
        best = max(left_old, key=lambda ko: fuzz.ratio(kn, ko), default=None)
        if best is not None and fuzz.ratio(kn, best) >= 90 and best not in pair.values():
            pair[kn] = best
    added = [new_p[k]["title"] for k in new_p if k not in pair]
    removed = [old_p[k]["title"] for k in old_p if k not in pair.values()]
    changed: list[dict[str, Any]] = []
    same = 0
    for kn, ko in pair.items():
        a, b = old_p[ko], new_p[kn]
        fields = {}
        if a["title"] != b["title"] and kn != ko:
            fields["title"] = {"было": a["title"], "стало": b["title"]}
        for f in _COMPARED:
            va = " ".join(str(a.get(f) or "").split())
            vb = " ".join(str(b.get(f) or "").split())
            if va != vb:
                fields[f] = {"было": a.get(f), "стало": b.get(f)}
        ta, tb = old_t.get(ko, set()), new_t.get(kn, set())
        if ta != tb:
            fields["tracks"] = {"было": sorted(ta), "стало": sorted(tb)}
        if fields:
            changed.append({"title": b["title"], "fields": fields})
        else:
            same += 1
    inter = pair
    precision = len(inter) / len(new_p) if new_p else 0.0
    recall = len(inter) / len(old_p) if old_p else 0.0
    pairs_old = {(ko, t) for ko in old_p for t in old_t.get(ko, ())}
    pairs_new = {(pair[kn], t) for kn in new_p if kn in pair for t in new_t.get(kn, ())}
    tp = len(pairs_old & pairs_new)
    pairs_new_all = sum(len(new_t.get(kn, ())) for kn in new_p)
    field_match = {}
    for f in _COMPARED:
        eq = sum(
            1 for kn, ko in inter.items()
            if " ".join(str(old_p[ko].get(f) or "").split()) == " ".join(str(new_p[kn].get(f) or "").split())
        )
        field_match[f] = round(eq / len(inter), 4) if inter else 0.0
    return {
        "old_count": len(old_p), "new_count": len(new_p),
        "added": added, "removed": removed, "changed": changed, "unchanged": same,
        "title_precision": round(precision, 4), "title_recall": round(recall, 4),
        "track_precision": round(tp / pairs_new_all, 4) if pairs_new_all else 0.0,
        "track_recall": round(tp / len(pairs_old), 4) if pairs_old else 0.0,
        "field_match": field_match,
    }


def render_diff(diff: dict[str, Any], result: ImportResult | None = None) -> str:
    lines = [
        "Импорт каталога программ: было / стало",
        "=" * 40,
        f"Было программ: {diff['old_count']}   Стало: {diff['new_count']}",
        f"Без изменений: {diff['unchanged']}   Изменено: {len(diff['changed'])}   Добавлено: {len(diff['added'])}   Пропало: {len(diff['removed'])}",
        f"Совпадение названий: точность {diff['title_precision']:.1%}, полнота {diff['title_recall']:.1%}",
        f"Совпадение пар «программа-трек»: точность {diff['track_precision']:.1%}, полнота {diff['track_recall']:.1%}",
    ]
    for title in diff["added"][:15]:
        lines.append(f"  + {title}")
    for title in diff["removed"][:15]:
        lines.append(f"  - {title}")
    for item in diff["changed"][:15]:
        names = ", ".join(item["fields"])
        lines.append(f"  ~ {item['title']}  ({names})")
    if result is not None:
        for w in result.warnings:
            lines.append(f"! {w}")
        if result.llm_calls:
            lines.append(f"Обращений к модели: {result.llm_calls}, токенов: {result.llm_tokens}")
    return "\n".join(lines)


# ------------------------------------------------------------------ применение и откат
def _stamp() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S-%f")[:-3]


def validate_candidate(path: Path, aliases: dict[str, str | None]) -> int:
    """Загрузчик продукта читает кандидата без ошибок, названия не пусты и не повторяются."""
    from catalog.programs import load_programmes

    try:
        courses = load_programmes(path, aliases)
    except Exception as exc:  # noqa: BLE001
        raise BookletError(f"Новый каталог не прошёл проверку: {exc}. Текущий каталог остался без изменений.") from exc
    if not courses:
        raise BookletError("В новом каталоге нет программ. Текущий каталог остался без изменений.")
    keys = [c.course_key for c in courses]
    if len(set(keys)) != len(keys):
        raise BookletError("В новом каталоге есть программы с одинаковыми названиями. Текущий каталог остался без изменений.")
    return len(courses)


def apply_import(result: ImportResult, aliases: dict[str, str | None]) -> dict[str, Any]:
    """Проверить, сделать резерв прежнего каталога, атомарно подменить рабочий. Откат: `revert_catalog`."""
    from core import paths

    text = dump_yaml(result)
    out_dir = paths.OUTPUT_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    candidate = out_dir / f".programs.candidate.{_stamp()}.yaml"
    try:
        candidate.write_text(text, encoding="utf-8")
        count = validate_candidate(candidate, aliases)
        backup_id = _backup_current(note=f"перед импортом «{result.source}»")
        os.replace(candidate, paths.catalog_override_path())
    finally:
        if candidate.exists():
            try:
                candidate.unlink()
            except OSError:
                pass
    return {"programmes": count, "backup": backup_id, "path": str(paths.catalog_override_path())}


def _backup_current(note: str = "") -> str:
    from core import paths

    root = paths.catalog_backup_root()
    backup_id = _stamp()
    folder = root / backup_id
    folder.mkdir(parents=True, exist_ok=True)
    override = paths.catalog_override_path()
    meta = {"id": backup_id, "created": datetime.now().isoformat(timespec="seconds"), "note": note,
            "had_override": override.is_file()}
    if override.is_file():
        shutil.copy2(override, folder / "programs.yaml")
    (folder / "meta.json").write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
    return backup_id


def list_catalog_backups() -> list[dict[str, Any]]:
    from core import paths

    root = paths.catalog_backup_root()
    out = []
    if root.is_dir():
        for folder in sorted(root.iterdir(), reverse=True):
            meta_path = folder / "meta.json"
            if meta_path.is_file():
                try:
                    out.append(json.loads(meta_path.read_text(encoding="utf-8")))
                except (OSError, ValueError):
                    continue
    return out


def revert_catalog(backup_id: str | None = None) -> dict[str, Any]:
    """Вернуть каталог, каким он был перед импортом (последний резерв или указанный). Откат сам откатывается."""
    from core import paths

    backups = list_catalog_backups()
    if not backups:
        raise BookletError("Резервных копий каталога нет: откатывать нечего (используется поставляемый каталог).")
    meta = next((b for b in backups if b["id"] == backup_id), None) if backup_id else backups[0]
    if meta is None:
        raise BookletError("Резервная копия каталога не найдена.")
    folder = paths.catalog_backup_root() / meta["id"]
    new_backup = _backup_current(note=f"перед откатом к {meta['id']}")
    override = paths.catalog_override_path()
    if meta.get("had_override") and (folder / "programs.yaml").is_file():
        tmp = override.with_suffix(".restore.yaml")
        shutil.copy2(folder / "programs.yaml", tmp)
        os.replace(tmp, override)
    elif override.exists():
        override.unlink()
    # использованный резерв убираем, чтобы повторный откат шёл дальше вглубь истории
    shutil.rmtree(folder, ignore_errors=True)
    return {"restored": meta["id"], "backup": new_backup, "using": "загруженный каталог" if override.exists() else "поставляемый каталог"}


def current_catalog_info() -> dict[str, Any]:
    from core import paths

    path = paths.active_programs_path()
    doc = load_catalog_doc(path)
    return {
        "path": str(path),
        "is_default": path != paths.catalog_override_path(),
        "source": doc.get("source"),
        "programmes": len(doc.get("programmes") or []),
    }
