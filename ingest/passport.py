"""«Паспорт данных» -- что именно мы прочитали из пакета выгрузок.

Менеджер должен видеть не «загружено 1314 строк», а: из какого файла и листа
взята история, какие колонки как распознаны (и насколько мы уверены), сколько
строк пропущено и почему, какие значения не поняли. Паспорт сериализуется в
словарь (REST, экран «Данные») и в русский текст (``--check-data``).
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

KIND_LABELS = {
    "history": "История обучения",
    "registry": "Реестр электронных курсов",
    "ignored": "Не используется",
}

ROLE_LABELS: dict[str, str] = {
    # история
    "user": "Сотрудник (ФИО)",
    "person_id": "Табельный номер",
    "position": "Должность",
    "iogv": "ИОГВ (орган власти)",
    "delivery": "Формат (ППК / ЭК)",
    "title": "Название курса",
    "status": "Статус",
    # реестр
    "annotation": "Аннотация",
    "goal": "Цель, задачи",
    "outcomes": "Результаты обучения",
    # служебные колонки, которые мы узнаём, но не используем
    "ignore": "Служебная колонка (не используется)",
}

METHOD_LABELS = {
    "exact": "по названию колонки",
    "contains": "по названию колонки (частичное совпадение)",
    "fuzzy": "по похожему названию колонки",
    "content": "по содержимому колонки",
    "order": "по порядку колонок",
    "profile": "по сохранённому профилю",
    "manual": "выбрано вручную",
    "ai": "подсказано ИИ",
}


@dataclass
class ColumnInfo:
    role: str
    role_label: str
    index: int | None
    header: str
    confidence: float
    method: str
    method_label: str = ""
    samples: list[str] = field(default_factory=list)


@dataclass
class TableInfo:
    key: str                      # «файл::лист» -- стабильный идентификатор
    file: str
    sheet: str
    kind: str                     # history | registry | ignored
    kind_label: str
    used: bool                    # именно эта таблица взята в работу
    reason: str                   # почему такая роль / почему не используется
    confidence: float
    header_row: int | None        # номер строки заголовка, с 1; None -- заголовка нет
    data_rows: int
    columns: list[ColumnInfo] = field(default_factory=list)
    all_headers: list[str] = field(default_factory=list)
    previews: list[list[str]] = field(default_factory=list)     # по 3 значения на каждую колонку
    missing_roles: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


@dataclass
class Passport:
    tables: list[TableInfo] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    counters: dict[str, int] = field(default_factory=dict)
    unknown_values: dict[str, dict[str, int]] = field(default_factory=dict)
    fuzzy_links: list[dict[str, Any]] = field(default_factory=list)
    summary: dict[str, Any] = field(default_factory=dict)
    # одинаковые ФИО в разных ИОГВ без табельного номера: посчитаны как двое, но это допущение
    homonym_cases: list[dict[str, Any]] = field(default_factory=list)
    # должности вне справочника: как определён грейд (keyword -- по слову «начальник» и т.п., default -- по умолчанию)
    position_resolution: list[dict[str, Any]] = field(default_factory=list)
    source_dir: str = ""

    # ---------------------------------------------------------------- helpers
    def warn(self, text: str) -> None:
        if text and text not in self.warnings:
            self.warnings.append(text)

    def error(self, text: str) -> None:
        if text and text not in self.errors:
            self.errors.append(text)

    def count(self, name: str, n: int = 1) -> None:
        self.counters[name] = self.counters.get(name, 0) + n

    def unknown(self, group: str, raw: str) -> None:
        bucket = self.unknown_values.setdefault(group, {})
        bucket[raw] = bucket.get(raw, 0) + 1

    @property
    def level(self) -> str:
        if self.errors:
            return "error"
        if self.warnings:
            return "warn"
        return "ok"

    def table(self, kind: str) -> TableInfo | None:
        for info in self.tables:
            if info.kind == kind and info.used:
                return info
        return None

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["level"] = self.level
        return data

    # ------------------------------------------------------------------ text
    def render(self) -> str:
        lines = ["Паспорт данных", "=" * 40]
        if self.source_dir:
            lines.append(f"Папка пакета: {self.source_dir}")
        lines.append("")
        for kind in ("history", "registry"):
            info = self.table(kind)
            title = KIND_LABELS[kind]
            if info is None:
                lines.append(f"{title}: " + ("НЕ НАЙДЕНА" if kind == "history" else "НЕ НАЙДЕН"))
                continue
            where = f"«{info.file}»" + (f", лист «{info.sheet}»" if info.sheet else "")
            header = (
                f"заголовок в строке {info.header_row}" if info.header_row else "заголовка нет (колонки по содержимому)"
            )
            lines.append(f"{title}: {where}; {header}; строк данных: {info.data_rows}")
            for col in info.columns:
                if col.role == "ignore":
                    continue
                name = f"«{col.header}»" if col.header else "(без названия)"
                lines.append(
                    f"    {col.role_label:<26} <- колонка {(col.index or 0) + 1}: {name}"
                    f"  [{col.method_label}, уверенность {col.confidence:.0%}]"
                )
            for role in info.missing_roles:
                lines.append(f"    {ROLE_LABELS.get(role, role):<26} <- не найдена")
        ignored = [t for t in self.tables if not t.used]
        if ignored:
            lines += ["", "Не использовано:"]
            for info in ignored:
                where = f"«{info.file}»" + (f", лист «{info.sheet}»" if info.sheet else "")
                lines.append(f"  - {where}: {info.reason}")
        if self.counters:
            lines += ["", "Счётчики:"]
            for name, value in self.counters.items():
                lines.append(f"  {COUNTER_LABELS.get(name, name)}: {value}")
        for group, values in self.unknown_values.items():
            if values:
                top = sorted(values.items(), key=lambda kv: -kv[1])[:8]
                shown = ", ".join(f"«{raw}» x{n}" for raw, n in top)
                lines.append(f"Не распознано ({GROUP_LABELS.get(group, group)}): {shown}")
        if self.fuzzy_links:
            lines += ["", f"Названия, связанные с каталогом по сходству ({len(self.fuzzy_links)}):"]
            for link in self.fuzzy_links[:10]:
                lines.append(f"  - «{link['from']}» -> «{link['to']}» ({link['score']:.0%})")
        if self.homonym_cases:
            lines += ["", f"Возможные однофамильцы / переводы — проверьте ({len(self.homonym_cases)}):"]
            for case in self.homonym_cases[:10]:
                lines.append(f"  - {case['name']}: {', '.join(case['iogvs'])} (строк: {case['rows']})")
        if self.position_resolution:
            lines += ["", f"Должности вне справочника, грейд определён автоматически ({len(self.position_resolution)}):"]
            for item in self.position_resolution[:15]:
                how = f"по слову «{item['matched']}»" if item["how"] == "keyword" else "по умолчанию"
                lines.append(f"  - «{item['position']}» -> {item['grade']} ({how}; чел.: {item['people']})")
        if self.errors:
            lines += ["", "ОШИБКИ:"] + [f"  ! {e}" for e in self.errors]
        if self.warnings:
            lines += ["", "Предупреждения:"] + [f"  - {w}" for w in self.warnings]
        if not self.errors and not self.warnings:
            lines += ["", "Замечаний нет."]
        return "\n".join(lines)


COUNTER_LABELS = {
    "rows_read": "прочитано строк",
    "rows_used": "принято строк",
    "rows_skipped": "пропущено строк",
    "blank_rows": "пустых строк (проигнорировано)",
    "status_unknown": "строк с нераспознанным статусом",
    "delivery_from_column": "формат (ППК/ЭК) взят из колонки",
    "delivery_from_title": "формат определён по «одежде» названия",
    "delivery_from_registry": "формат определён по реестру ЭК",
    "delivery_unknown": "формат не указан в выгрузке",
    "delivery_from_catalog": "формат определён по каталогу программ",
    "titles_cleaned": "названий очищено от дат и тегов",
    "filled_from_other_rows": "пустых должностей/ИОГВ заполнено по другим строкам человека",
    "registry_rows": "курсов в реестре",
    "footer_rows": "итоговых/служебных строк под данными (проигнорировано)",
    "homonyms_split": "одинаковых ФИО разведено на разных людей",
}
GROUP_LABELS = {"status": "статус", "delivery": "формат"}
