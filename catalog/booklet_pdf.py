"""Карточки программ и «Навигатор» из PDF-буклета («Линейка образовательных программ»).

Чтение «кодом»: PDF приходит из PowerPoint, текстовый слой в нём дырявый -- пробелы
пропадают внутри слов, подпись футера бывает задвоена («ааккттууааллььнноо»), правая колонка
(ТРУДОЁМКОСТЬ / ФОРМА ОБУЧЕНИЯ / КАТЕГОРИЯ СЛУШАТЕЛЕЙ) вперемешку с левой (темы) в обычном
`extract_text()`. Поэтому слова берутся с координатами (pdfplumber), строки склеиваются по
вертикали и размеру шрифта, а колонки разделяются по горизонтальной координате.

Раскладка, на которую рассчитан разбор (так выглядит буклет 2025 года):
  * карточка начинается строкой «<Группа> / <Подгруппа>»; название программы -- блок строк
    ЗАГЛАВНЫМИ БУКВАМИ над ней;
  * затем аннотация, «КЛЮЧЕВЫЕ ТЕМЫ:» и правая колонка с трудоёмкостью, формой, категорией;
  * в конце буклета -- «Навигатор»: списки «Программы для <категория>» с нумерованными названиями.
Страницы с карточками и «Навигатор» ищутся сами, номера страниц в коде не зашиты.
Если раскладка другая -- карточек не будет найдено, и импорт скажет об этом по-русски
(см. `catalog.import_booklet`); текущий каталог при этом не меняется.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

GROUP_NAMES = [
    "Личностные компетенции",
    "Инструменты управления",
    "Профильные hard-skills",
    "Профессиональные компетенции",
    "Базовые компетенции",
]

KNOWN_FORM_STARTS = ("очная", "заочная", "очно-заочная", "дистанционная", "электронная", "очная с")

NAVIGATOR_TRACKS = [
    "Впервые поступившие на государственную гражданскую службу",
    "Специалисты",
    "Впервые назначенные руководителем",
    "Руководители главной и ведущей групп должностей",
    "Руководители высшей группы должностей",
    "Команды изменений",
    "Рейтинг цифровой зрелости ИОГВ",
]

HOURS_RE = re.compile(r"(\d+)\s*ак\.?\s*час")
ITEM_START_RE = re.compile(r"^\d+\.\s*\S")
PAGE_REF_RE = re.compile(r"с\.\s*(\d+)\s*$")

# Имена собственные и аббревиатуры, которые нельзя опускать в нижний регистр, если название
# не подтверждено «Навигатором» и не восстановлено моделью.
CASING_EXCEPTIONS = {
    "санкт-петербурга": "Санкт-Петербурга", "санкт-петербурге": "Санкт-Петербурге",
    "санкт-петербург": "Санкт-Петербург", "санкт-петербургской": "Санкт-Петербургской",
    "гис": "ГИС", "есэдд": "ЕСЭДД", "гост": "ГОСТ", "госта": "ГОСТа", "иогв": "ИОГВ",
    "scrum": "Scrum", "kanban": "Kanban", "agile": "Agile", "эо": "ЭО", "дот": "ДОТ", "фз": "ФЗ",
    "223-фз": "223-ФЗ", "ппк": "ППК", "эк": "ЭК", "сми": "СМИ", "swot": "SWOT", "свот": "SWOT",
    "ms": "MS", "excel": "Excel", "word": "Word", "it": "IT", "ит": "ИТ",
}


def nospace(text: str) -> str:
    return re.sub(r"\s+", "", text)


def alnum_casefold(text: str) -> str:
    return re.sub(r"[^0-9a-zA-Zа-яёА-ЯЁ]", "", text).casefold()


def match_key(text: str) -> str:
    """Ключ сопоставления названия карточки и строки «Навигатора».

    Гасит только орфографические различия (е/ё, и/й, номер страницы, вклеенный в слово текстовым
    слоем). Различия в формулировке НЕ гасятся: это настоящие расхождения в документе заказчика,
    они должны всплыть как «не сопоставлено», а не быть замазаны нечётким совпадением.
    """
    folded = text.replace("ё", "е").replace("Ё", "Е").replace("й", "и").replace("Й", "И")
    folded = re.sub(r"(?<=[^\W\d_])\d+(?=[^\W\d_])", "", folded)
    return alnum_casefold(folded)


def collapse_doubled_letters(text: str) -> str:
    prev, current = None, text
    while current != prev:
        prev = current
        current = re.sub(r"(.)\1", r"\1", current)
    return current


_FOOTER_PHRASES = ("актуальнодляменя", "актуальнодлякоманды", "неактуально")


def is_footer_row_text(text: str) -> bool:
    t = nospace(text).casefold()
    folded = collapse_doubled_letters(t)
    return any(p in t or p in folded for p in _FOOTER_PHRASES)


@dataclass
class RawCard:
    page: int
    group: str
    subgroup: str
    title: str                       # "" -- артефакт, карточка отбрасывается
    annotation: str
    topics: list[str] = field(default_factory=list)
    hours: int | None = None
    study_form: str | None = None
    audience: str | None = None
    right_text: str = ""             # сырой текст правой колонки -- для подсказки модели


RIGHT_COLUMN_HEADINGS = {
    alnum_casefold(h) for h in ("ТРУДОЁМКОСТЬ:", "ФОРМА ОБУЧЕНИЯ:", "КАТЕГОРИЯ СЛУШАТЕЛЕЙ:")
}


def _is_right_column_heading(row_text: str) -> bool:
    return alnum_casefold(row_text) in RIGHT_COLUMN_HEADINGS


def _is_title_like(row_text: str) -> bool:
    letters = [c for c in row_text if c.isalpha()]
    return bool(letters) and all(not c.islower() for c in letters)


def _group_line_match(row_text: str, lookup: dict[str, str]) -> tuple[str, str] | None:
    """Строка «<Группа> / <Подгруппа>» -> (группа, текст со слэша)."""
    if "/" not in row_text:
        return None
    slash_idx = row_text.index("/")
    group = lookup.get(alnum_casefold(row_text[:slash_idx]))
    if group is None:
        return None
    return group, row_text[slash_idx:]


def _extract_words_rows(page):
    """Слова страницы -> строки по близкому `top` И размеру шрифта.

    Размер шрифта входит в ключ: на странице с «осиротевшей» копией шапки предыдущей карточки
    две шапки лежат почти на одной базовой линии, и кластеризация по позиции смешала бы буквы.
    x_tolerance=1.5 (не 3): в буклете часть пробелов уже 3 pt, иначе слова слипаются.
    """
    words = page.extract_words(use_text_flow=False, keep_blank_chars=False, extra_attrs=["size"], x_tolerance=1.5)
    words.sort(key=lambda w: (w["top"], w["x0"]))
    rows: list[dict] = []
    for w in words:
        size = round(w.get("size", 0.0), 1)
        for row in rows:
            if abs(row["top"] - w["top"]) <= 2.0 and row["size"] == size:
                row["words"].append(w)
                break
        else:
            rows.append({"top": w["top"], "size": size, "words": [w]})
    for row in rows:
        row["words"].sort(key=lambda w: w["x0"])
        row["top"] = min(w["top"] for w in row["words"])
        row["bottom"] = max(w["bottom"] for w in row["words"])
        row["text"] = " ".join(w["text"] for w in row["words"])
    rows.sort(key=lambda r: r["top"])
    return rows


def _parse_topics(left_text: str) -> list[str]:
    marker = "КЛЮЧЕВЫЕ ТЕМЫ:"
    idx = left_text.find(marker)
    body = left_text[idx + len(marker):] if idx != -1 else left_text
    return [p for p in (x.strip(" ;.") for x in body.split("•")) if p]


def _parse_right_column(right_text: str) -> tuple[int | None, str | None, str | None]:
    hours = None
    m = HOURS_RE.search(right_text)
    if m:
        hours = int(m.group(1))
    study_form = audience = None
    form_idx = right_text.find("ФОРМА ОБУЧЕНИЯ:")
    cat_idx = right_text.find("КАТЕГОРИЯ СЛУШАТЕЛЕЙ:")
    if form_idx != -1:
        end = cat_idx if cat_idx != -1 and cat_idx > form_idx else len(right_text)
        raw_form = re.sub(r"\s+", " ", right_text[form_idx + len("ФОРМА ОБУЧЕНИЯ:"):end]).strip()
        if raw_form and raw_form.casefold().startswith(KNOWN_FORM_STARTS):
            study_form = raw_form
    if cat_idx != -1:
        audience = re.sub(r"\s+", " ", right_text[cat_idx + len("КАТЕГОРИЯ СЛУШАТЕЛЕЙ:"):]).strip() or None
    return hours, study_form, audience


def _extract_cards_from_page(page, page_number: int, lookup: dict[str, str]) -> list[RawCard]:
    rows = _extract_words_rows(page)
    boundaries = []
    for idx, row in enumerate(rows):
        m = _group_line_match(row["text"], lookup)
        if m:
            boundaries.append((idx, m[0], m[1]))

    cards: list[RawCard] = []
    for b_i, (idx, group, subgroup_head) in enumerate(boundaries):
        group_row = rows[idx]
        subgroup_text = subgroup_head[1:].strip()
        last_row = group_row
        j = idx + 1
        while j < len(rows):
            gap = rows[j]["top"] - last_row["bottom"]
            if gap <= 6.0 and not _group_line_match(rows[j]["text"], lookup):
                subgroup_text = (subgroup_text + " " + rows[j]["text"]).strip()
                last_row = rows[j]
                j += 1
            else:
                break

        title_rows, title_size, k = [], None, idx - 1
        while k >= 0:
            cand = rows[k]
            if is_footer_row_text(cand["text"]):
                break
            if _group_line_match(cand["text"], lookup):
                k -= 1
                continue
            if not _is_title_like(cand["text"]):
                break
            if title_size is None:
                title_size = cand["size"]
            elif cand["size"] != title_size:
                break
            title_rows.insert(0, cand["text"])
            k -= 1
        title = " ".join(title_rows).strip()

        key_idx = next(
            (m for m in range(j, len(rows)) if "КЛЮЧЕВЫЕ" in rows[m]["text"] and "ТЕМЫ" in rows[m]["text"]), None
        )
        if key_idx is None:
            continue
        key_top = rows[key_idx]["top"]
        next_boundary_idx = boundaries[b_i + 1][0] if b_i + 1 < len(boundaries) else len(rows)
        footer_top = None
        for m in range(key_idx, min(next_boundary_idx, len(rows))):
            if is_footer_row_text(rows[m]["text"]):
                footer_top = rows[m]["top"]
                break
        if footer_top is None:
            footer_top = rows[next_boundary_idx - 1]["bottom"] if next_boundary_idx > 0 else page.height

        span_words = [
            w for row in rows[key_idx:next_boundary_idx] for w in row["words"]
            if key_top - 1 <= w["top"] < footer_top - 0.5
        ]
        marker_x0s = [
            w["x0"] for w in span_words
            if _is_title_like(w["text"])
            and nospace(w["text"]).rstrip(":").casefold() in {"трудоёмкость", "форма", "обучения", "категория", "слушателей"}
        ]
        split_x = (min(marker_x0s) - 15) if marker_x0s else 350.0

        annotation_rows = [
            row for row in rows[j:key_idx]
            if not is_footer_row_text(row["text"]) and not _is_right_column_heading(row["text"])
        ]
        annotation = re.sub(r"\s+", " ", " ".join(row["text"] for row in annotation_rows)).strip()

        left_words = sorted((w for w in span_words if w["x0"] < split_x), key=lambda w: (w["top"], w["x0"]))
        right_words = sorted((w for w in span_words if w["x0"] >= split_x), key=lambda w: (w["top"], w["x0"]))
        left_text = " ".join(w["text"] for w in left_words)
        right_text = " ".join(w["text"] for w in right_words)
        hours, study_form, audience = _parse_right_column(right_text)
        cards.append(RawCard(
            page=page_number, group=group, subgroup=subgroup_text, title=title, annotation=annotation,
            topics=_parse_topics(left_text), hours=hours, study_form=study_form, audience=audience,
            right_text=right_text,
        ))
    return cards


def learn_group_names(pdf) -> list[str]:
    """Группы компетенций: известные + найденные в самом документе.

    Левая часть строки «X / Y» с X из 1-4 слов, начинающимся с заглавной буквы, не КАПСОМ, которая
    встречается на 3+ страницах, считается именем группы (буклет другого года может назвать группы иначе).
    """
    found: dict[str, set[int]] = {}
    for number, page in enumerate(pdf.pages, start=1):
        try:
            text = page.extract_text() or ""
        except Exception:  # noqa: BLE001
            continue
        for line in text.splitlines():
            if "/" not in line:
                continue
            left = line.split("/", 1)[0].strip()
            words = left.split()
            if 1 <= len(words) <= 4 and left[:1].isupper() and not _is_title_like(left) and len(left) >= 8:
                found.setdefault(left, set()).add(number)
    learned = [name for name, pages in found.items() if len(pages) >= 3 and alnum_casefold(name) not in
               {alnum_casefold(g) for g in GROUP_NAMES}]
    return GROUP_NAMES + learned


def extract_raw_cards(pdf, group_names: list[str] | None = None) -> list[RawCard]:
    names = group_names or GROUP_NAMES
    lookup = {alnum_casefold(g): g for g in names}
    cards: list[RawCard] = []
    for number, page in enumerate(pdf.pages, start=1):
        try:
            cards.extend(_extract_cards_from_page(page, number, lookup))
        except Exception:  # noqa: BLE001 -- одна кривая страница не должна ронять весь импорт
            continue
    return cards


# --------------------------------------------------------------------------
# Навигатор
# --------------------------------------------------------------------------
def resolve_track(header_text: str) -> str:
    """Название категории слушателей -> каноническое имя трека (или очищенный заголовок)."""
    norm = alnum_casefold(header_text)
    if "рейтинг" in norm:
        return "Рейтинг цифровой зрелости ИОГВ"
    if "высш" in norm and "должност" in norm:
        return "Руководители высшей группы должностей"
    if "главн" in norm and "ведущ" in norm:
        return "Руководители главной и ведущей групп должностей"
    if "команд" in norm and "изменен" in norm:
        return "Команды изменений"
    if "назначен" in norm:
        return "Впервые назначенные руководителем"
    if "специалист" in norm:
        return "Специалисты"
    if "поступ" in norm:
        return "Впервые поступившие на государственную гражданскую службу"
    cleaned = re.sub(r"(?i)^программы\s+(для\s+)?", "", header_text.strip()).strip(" :.")
    return cleaned[:1].upper() + cleaned[1:] if cleaned else header_text.strip()


def find_navigator_pages(pdf, last_card_page: int) -> list[int]:
    """Страницы «Навигатора»: после последней карточки, со ссылками «с. NN» на конце строк."""
    pages = []
    for number in range(last_card_page + 1, len(pdf.pages) + 1):
        try:
            text = pdf.pages[number - 1].extract_text() or ""
        except Exception:  # noqa: BLE001
            continue
        refs = sum(1 for line in text.splitlines() if PAGE_REF_RE.search(line.strip()))
        if refs >= 2 or "НАВИГАТОР" in text:
            pages.append(number)
    return pages


def extract_navigator(pdf, pages: list[int]) -> dict[str, list[str]]:
    lines: list[str] = []
    for number in pages:
        text = pdf.pages[number - 1].extract_text() or ""
        for line in text.splitlines():
            line = line.strip()
            if not line or line == "НАВИГАТОР" or re.fullmatch(r"\d+", line):
                continue
            lines.append(line)

    navigator: dict[str, list[str]] = {}
    current_track: str | None = None
    item_lines: list[str] = []

    def flush_item():
        nonlocal item_lines
        if item_lines and current_track:
            joined = re.sub(r"\s+", " ", " ".join(item_lines)).strip()
            joined = re.sub(r"^\d+\.\s*", "", joined)
            page_m = PAGE_REF_RE.search(joined)
            if page_m:
                joined = joined[: page_m.start()].strip()
            title = joined.strip(" .")
            if title:
                navigator.setdefault(current_track, []).append(title)
        item_lines = []

    i = 0
    while i < len(lines):
        line = lines[i]
        if alnum_casefold(line).startswith("программы") and not ITEM_START_RE.match(line):
            flush_item()
            header_lines = [line]
            i += 1
            while i < len(lines) and not ITEM_START_RE.match(lines[i]):
                header_lines.append(lines[i])
                i += 1
            current_track = resolve_track(" ".join(header_lines))
            continue
        if ITEM_START_RE.match(line):
            flush_item()
            item_lines = [line]
            i += 1
            continue
        item_lines.append(line)
        i += 1
    flush_item()
    return navigator


def fallback_case(all_caps_title: str) -> str:
    out = []
    for i, w in enumerate(all_caps_title.split(" ")):
        key = w.casefold()
        if key in CASING_EXCEPTIONS:
            out.append(CASING_EXCEPTIONS[key])
            continue
        lowered = w.lower()
        if i == 0 and lowered:
            lowered = lowered[0].upper() + lowered[1:]
        out.append(lowered)
    result = " ".join(out)
    result = re.sub(r"([.:]\s+)([a-zа-яё])", lambda m: m.group(1) + m.group(2).upper(), result)
    return result[:1].upper() + result[1:] if result else result


def read_pdf(path) -> dict:
    """PDF -> {'cards': [RawCard...], 'navigator': {track: [title...]}, 'discarded': n, 'pages': n}.

    Бросает `ImportError` без pdfplumber и `ValueError` с готовой фразой, если файл не открылся.
    """
    import pdfplumber

    try:
        pdf = pdfplumber.open(str(path))
    except Exception as exc:  # noqa: BLE001
        raise ValueError(f"PDF не удалось открыть: {exc}") from exc
    try:
        names = learn_group_names(pdf)
        raw = extract_raw_cards(pdf, names)
        real = [c for c in raw if c.title.strip() and c.hours is not None and c.topics]
        last_card_page = max((c.page for c in real), default=0)
        nav_pages = find_navigator_pages(pdf, last_card_page) if real else []
        navigator = extract_navigator(pdf, nav_pages) if nav_pages else {}
        return {
            "cards": real,
            "discarded": len(raw) - len(real),
            "navigator": navigator,
            "pages": len(pdf.pages),
            "group_names": names,
        }
    finally:
        pdf.close()
