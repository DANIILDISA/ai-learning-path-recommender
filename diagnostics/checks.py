"""Self-diagnostics: data files, catalogue, position dictionary, model group.

Used by three callers: the CLI (`main.py --doctor`), `START.bat` (through
`--doctor --group <choice>` before it launches anything) and, later,
`/api/health`. All three need the exact same guarantee: a check never raises,
and every failure is a sentence a civil servant can act on -- never a stack
trace, never an errno, never English exception text.
"""
from __future__ import annotations

import sys
from dataclasses import dataclass
from enum import Enum

from core.config import AppConfig, load_config
from core.paths import CATALOG_DIR, PRODUCT_ROOT
from llm.base import LLMUnavailable
from llm.factory import load_env_once

# Load .env once, at import time -- before any test's monkeypatch.setenv /
# delenv runs, and before the CLI does anything else. `load_env_once` reads
# the file at most once per process, so this guarantees that later calls to
# `build_client` (inside `check_llm_group`) never re-read `.env` and silently
# undo a deliberately absent key -- the scenario `--doctor` exists to report.
load_env_once()

_MIN_PYTHON = (3, 10)

_GROUP_LABELS = {
    "russian": "российская (GigaChat)",
    "foreign": "зарубежная (OpenRouter)",
    "local": "локальная (Ollama)",
}


class Status(str, Enum):
    OK = "OK"
    WARN = "WARN"
    FAIL = "FAIL"


_MARKS = {
    Status.OK: "[OK]  ",
    Status.WARN: "[!!]  ",
    Status.FAIL: "[ОШИБКА]",
}


@dataclass(frozen=True)
class CheckResult:
    name: str
    status: Status
    detail: str = ""
    hint: str = ""


def _ok(name: str, detail: str) -> CheckResult:
    return CheckResult(name=name, status=Status.OK, detail=detail)


def _fail(name: str, detail: str, hint: str) -> CheckResult:
    return CheckResult(name=name, status=Status.FAIL, detail=detail, hint=hint)


def _safe_check(name: str, hint_on_crash: str, func) -> CheckResult:
    """Run one check, turning any exception into a readable FAIL.

    This is the single place that enforces "a check never raises" -- callers
    of `run_checks` never need their own try/except around a check.
    """
    try:
        return func()
    except Exception:  # noqa: BLE001 -- a broken check must degrade, not crash the menu
        return _fail(
            name,
            "Проверка не выполнена из-за внутренней ошибки.",
            hint_on_crash,
        )


def _check_runtime() -> CheckResult:
    name = "Рантайм Python"
    version = sys.version_info
    if version < _MIN_PYTHON:
        return CheckResult(
            name=name,
            status=Status.WARN,
            detail=f"Обнаружен Python {version.major}.{version.minor}; продукт рассчитан на {_MIN_PYTHON[0]}.{_MIN_PYTHON[1]}+.",
            hint="Используйте python.exe из папки runtime продукта, если она есть, или обновите Python.",
        )
    return _ok(name, f"Python {version.major}.{version.minor}.{version.micro}.")


def _check_data_files(config: AppConfig) -> CheckResult:
    """Data package check: files are found by CONTENT, names do not matter."""
    name = "Файлы данных"
    hint = (
        "Положите выгрузку истории обучения (и, по возможности, реестр электронных курсов) "
        "в папку data рядом с продуктом или загрузите их на экране «Данные». "
        "Подойдут Excel (.xlsx, .xls) и CSV; названия файлов не важны."
    )
    from core import paths
    from ingest.normalize import load_aliases
    from ingest.package import load_package

    aliases = load_aliases(CATALOG_DIR / config.aliases_file)
    package = load_package(paths.DATA_DIR, aliases, history_prefix=config.history_prefix,
                           registry_prefix=config.registry_prefix, history_sheet=config.history_sheet)
    passport = package.passport
    history = passport.table("history")
    registry = passport.table("registry")
    if history is None or not package.entries:
        return _fail(name, "В папке data не найдена таблица с историей обучения.", hint)
    where = f"{history.file}" + (f" / {history.sheet}" if history.sheet else "")
    detail = f"История: {where} ({len(package.entries)} строк)"
    if registry is None:
        return CheckResult(
            name=name,
            status=Status.WARN,
            detail=detail + "; реестр электронных курсов не найден.",
            hint="Без реестра продукт работает, но курсы из реестра не попадут в каталог.",
        )
    return _ok(name, detail + f"; реестр: {registry.file}.")


def _check_catalogue(config: AppConfig) -> CheckResult:
    name = "Каталог курсов"
    hint = (
        "Проверьте файлы catalog/aliases.yaml и catalog/programs.yaml, а также реестр "
        "электронных курсов в папке data -- один из них не читается."
    )
    from catalog.catalog import Catalog
    from catalog.programs import load_programmes
    from core import paths
    from ingest.normalize import load_aliases
    from ingest.package import load_package

    try:
        aliases = load_aliases(CATALOG_DIR / config.aliases_file)
        package = load_package(paths.DATA_DIR, aliases)
        catalog = Catalog()
        for course in package.registry:
            catalog.add(course)
        for course in load_programmes(paths.active_programs_path(), aliases):
            catalog.add(course)
    except Exception:
        return _fail(name, "Не удалось прочитать каталог курсов.", hint)

    if len(catalog) == 0:
        return _fail(name, "Каталог курсов пуст.", hint)
    return _ok(name, f"Загружено {len(catalog)} курсов (реестр + буклет программ).")


def _check_positions() -> CheckResult:
    name = "Словарь должностей"
    hint = "Проверьте файл catalog/positions.yaml -- он должен быть корректным YAML со списком должностей."
    from engine.grades import PositionIndex

    try:
        index = PositionIndex.load(CATALOG_DIR / "positions.yaml")
    except FileNotFoundError:
        return _fail(name, "Не найден файл catalog/positions.yaml.", hint)
    except Exception:
        return _fail(name, "Не удалось прочитать словарь должностей.", hint)

    return _ok(name, f"Словарь должностей загружен ({len(index.unconfirmed())} записей без подтверждения).")


def _display_base_url(base_url: str | None) -> str:
    if not base_url:
        return "не задан"
    return base_url[: -len("/v1")] if base_url.endswith("/v1") else base_url


def _is_local_url(base_url: str | None) -> bool:
    if not base_url:
        return False
    return "localhost" in base_url or "127.0.0.1" in base_url


def check_llm_group(group: str, probe: bool = True, timeout: float | None = None) -> CheckResult:
    """Is `group` ready to use -- key present, and (if `probe`) does the model answer?

    Kept independent from `run_checks` so `.bat` and the API health endpoint can
    check a single, freshly-chosen group without paying for the other two.
    Never raises: any failure, at construction or at the probe call, becomes a
    FAIL with a message that names what a person should do next -- never the
    raw transport exception (which on Windows carries WinError/errno numbers
    a civil servant cannot act on).
    """
    label = _GROUP_LABELS.get(group, group)
    name = f"Модель: {label}"

    try:
        config = load_config(PRODUCT_ROOT / "config.yaml")
    except Exception:
        return _fail(
            name,
            "Не удалось прочитать config.yaml.",
            "Проверьте файл config.yaml на опечатки или восстановите его из поставки.",
        )

    group_config = config.llm_groups.get(group)
    if group_config is None:
        return _fail(
            name,
            f"Группа моделей «{group}» не настроена в config.yaml.",
            "Проверьте раздел llm.groups в config.yaml или обратитесь к разработчику продукта.",
        )

    overrides: dict = {"model_group": group, "llm_enabled": True}
    if timeout is not None:
        overrides["timeout_seconds"] = timeout
    probe_config = config.model_copy(update=overrides)

    try:
        from llm.factory import build_client

        client = build_client(probe_config)
    except LLMUnavailable:
        key_hint_var = group_config.api_key_env or "соответствующую переменную"
        return _fail(
            name,
            f"Ключ доступа для группы «{label}» не найден или пуст ({key_hint_var}).",
            f"Укажите {key_hint_var} в файле .env в корне продукта и запустите проверку снова.",
        )
    except Exception:
        return _fail(
            name,
            "Не удалось подготовить подключение к модели из-за внутренней ошибки.",
            "Обратитесь к разработчику продукта, приложив шаг, который к этому привёл.",
        )

    if client is None:
        return _fail(
            name,
            "Использование моделей отключено в config.yaml (llm.enabled: false).",
            "Включите llm.enabled в config.yaml, если хотите использовать модель.",
        )

    if not probe:
        return _ok(name, f"Ключ для группы «{label}» найден.")

    try:
        client.complete(system="ping", user="ping")
    except LLMUnavailable:
        if _is_local_url(group_config.base_url):
            display_url = _display_base_url(group_config.base_url)
            return _fail(
                name,
                f"Локальная модель: сервер по адресу {display_url} не отвечает.",
                "Проверьте, установлена ли Ollama (или другой локальный сервер) и запущена ли она, "
                "затем повторите проверку.",
            )
        return _fail(
            name,
            f"Модель «{group_config.model}» не ответила на тестовый запрос.",
            f"Проверьте ключ {group_config.api_key_env or ''}, доступ в интернет и повторите попытку позже.".strip(),
        )
    except Exception:
        return _fail(
            name,
            "Не удалось обратиться к модели из-за внутренней ошибки.",
            "Повторите попытку позже; если ошибка повторяется, обратитесь к разработчику продукта.",
        )

    return _ok(name, f"Модель «{group_config.model}» ответила на тестовый запрос.")


def run_checks(group: str | None) -> list[CheckResult]:
    """The full readiness picture: runtime, data, catalogue, positions, and
    (only when `group` is given) that group's key and model availability.

    `group=None` is the shape used before any model choice has been made
    (e.g. a plain `--check-data`-style call); the model checks are skipped
    rather than reported as failing, since no group was asked for.
    """
    config = load_config(PRODUCT_ROOT / "config.yaml")
    results = [
        _safe_check("Рантайм Python", "Обратитесь к разработчику продукта.", _check_runtime),
        _safe_check(
            "Файлы данных",
            "Проверьте папку data рядом с продуктом.",
            lambda: _check_data_files(config),
        ),
        _safe_check(
            "Каталог курсов",
            "Проверьте файлы каталога в папке catalog.",
            lambda: _check_catalogue(config),
        ),
        _safe_check(
            "Словарь должностей",
            "Проверьте файл catalog/positions.yaml.",
            _check_positions,
        ),
    ]
    if group:
        results.append(
            _safe_check(
                f"Модель: {_GROUP_LABELS.get(group, group)}",
                "Обратитесь к разработчику продукта.",
                lambda: check_llm_group(group, probe=True),
            )
        )
    return results


def render(results: list[CheckResult]) -> str:
    """Human-readable table for the console, safe under cp1251/cp866/utf-8."""
    lines = ["Проверка готовности продукта", "=" * 40]
    for result in results:
        mark = _MARKS.get(result.status, "[?]")
        lines.append(f"{mark} {result.name}: {result.detail}")
        if result.status is not Status.OK and result.hint:
            lines.append(f"       -> {result.hint}")
    return "\n".join(lines)
