"""Command-line entry point.

The ТЗ requires the agent to be runnable from the command line as well as from
the web UI; this module is the CLI half.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from benchmark.report import write_benchmark_workbook
from benchmark.runner import build_benchmark_client, run_group, sample_user_ids
from catalog.catalog import Catalog
from catalog.interests import InterestIndex
from catalog.programs import load_programmes
from catalog.titles import known_spellings, load_title_spellings
from core.config import AppConfig, load_config
from core.models import Delivery, Profile, Track
from core.preferences import Preferences
from core.paths import CATALOG_DIR, PRODUCT_ROOT
from engine.grades import PositionIndex
from engine.peers import PeerIndex
from agents.reranker import rerank
from engine.track import build_track
from llm.base import LLMClient, LLMUnavailable
from llm.factory import build_client
from ingest.history import build_profiles
from ingest.normalize import load_aliases
from quality.report import DataQualityReport, build_report
from report.tracks import write_excel_report, write_json_report

# Some consoles this runs in (a plain cmd.exe with a Cyrillic ANSI codepage,
# or a non-Windows-console terminal that Python falls back to locale encoding
# for) cannot encode every character this product prints -- combining marks
# from decomposed Unicode in the source data, for instance. Without this, that
# turns into a raw UnicodeEncodeError traceback, which is exactly the kind of
# message a civil servant must never see. `errors="replace"` means the worst
# case is a stray "?" character, never a crash. The full switch (plus a test)
# is tracked as its own piece of work; this is the minimal guard so `--doctor`
# itself cannot crash on output today.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass


def assemble_catalog(package, aliases, passport) -> Catalog:
    """Каталог = реестр электронных курсов из пакета + программы буклета."""
    from core import paths as _paths

    catalog = Catalog()
    for course in package.registry:
        catalog.add(course)
    try:
        # Слитные названия («Интенсивпо ...») чинятся по написаниям, которые
        # заказчик сам использует в выгрузке и в реестре.
        known = known_spellings(
            [
                *load_title_spellings(CATALOG_DIR / "title_spellings.yaml"),
                *(course.title for course in package.registry),
                *(entry.title for entry in package.entries),
            ]
        )
        programmes = load_programmes(_paths.active_programs_path(), aliases, known)
    except Exception as exc:  # noqa: BLE001 -- битый буклет не должен валить приложение
        passport.warn(f"Каталог программ (programs.yaml) не прочитан: {exc}")
        programmes = []
    for course in programmes:
        catalog.add(course)
    return catalog


def link_and_resolve(raw_entries, catalog, fuzzy_threshold, passport):
    """Привязать названия истории к каталогу и определить формат записей.

    Возвращает (записи, связи по сходству). Общий путь для рабочих данных и для
    предпросмотра загрузки на экране «Данные»: то, что методист видит до
    «Применить», должно совпадать с тем, что получится после.
    """
    from catalog.linking import fuzzy_link
    from ingest.package import relink_entries, resolve_unknown_delivery

    entries = raw_entries
    unmatched = {e.course_key for e in entries} - catalog.keys()
    links = fuzzy_link(unmatched, catalog.keys(), fuzzy_threshold) if unmatched else []
    if links:
        titles = {e.course_key: e.title for e in entries}
        entries = relink_entries(entries, {l.source_key: l.target_key for l in links})
        for link in links:
            passport.fuzzy_links.append(
                {
                    "from": titles.get(link.source_key, link.source_key),
                    "to": catalog.get(link.target_key).title if catalog.get(link.target_key) else link.target_key,
                    "score": link.score,
                }
            )
        passport.warn(
            f"{len(links)} {_plural_ru(len(links), ('название курса связано', 'названия курсов связаны', 'названий курсов связаны'))} "
            "с каталогом по сходству, а не точно. Список — в блоке «Курсы, связанные по сходству» ниже; "
            "проверьте, что это те же курсы."
        )

    entries, resolved = resolve_unknown_delivery(entries, catalog)
    unknown = sum(1 for e in entries if e.delivery is Delivery.UNKNOWN)
    passport.counters["delivery_from_catalog"] = resolved
    passport.counters["delivery_unknown"] = unknown
    if unknown:
        passport.warn(
            f"Формат обучения (ППК или электронный курс) не указан в выгрузке и не определился ни по названию, "
            f"ни по реестру, ни по каталогу у {unknown} {_plural_ru(unknown, ('записи', 'записей', 'записей'))}. "
            "Они НЕ засчитаны как электронные курсы: в «Моём обучении» у них стоит «формат не указан». "
            "На то, что человек прошёл, и на подборку это не влияет — они считаются по названию курса. "
            "Чтобы подписи были полными, добавьте в выгрузку колонку «Формат» (ППК / ЭК)."
        )
    return entries, links


def _plural_ru(count: int, forms: tuple[str, str, str]) -> str:
    one, few, many = forms
    if 11 <= count % 100 <= 14:
        return many
    if count % 10 == 1:
        return one
    if 2 <= count % 10 <= 4:
        return few
    return many


def load_everything_ex(
    data_dir: Path | None = None,
    llm: LLMClient | None = None,
    overrides: dict | None = None,
    use_profile: bool = True,
):
    """Load config, the data package and the catalogue; return the passport too.

    Never raises on bad data: a missing history gives empty profiles and an
    error in the passport, so the app still starts and can accept a new
    package on the «Данные» screen.
    """
    from core import paths as _paths
    from ingest.package import load_package
    from ingest.smart_table import DataProfile

    config = load_config(PRODUCT_ROOT / "config.yaml")
    aliases = load_aliases(CATALOG_DIR / config.aliases_file)
    directory = Path(data_dir) if data_dir is not None else _paths.DATA_DIR
    profile = DataProfile(_paths.data_profile_path()) if use_profile else None

    package = load_package(
        directory,
        aliases,
        profile=profile,
        overrides=overrides,
        llm=llm,
        ai_cache_dir=_paths.OUTPUT_DIR / "ai_mapping_cache",
        history_prefix=config.history_prefix,
        registry_prefix=config.registry_prefix,
        history_sheet=config.history_sheet,
    )
    passport = package.passport

    catalog = assemble_catalog(package, aliases, passport)
    entries, links = link_and_resolve(package.entries, catalog, config.fuzzy_threshold, passport)

    report = build_report(entries, catalog, fuzzy_links=len(links))
    history_keys = {e.course_key for e in entries}
    passport.summary.update(
        {
            "catalogue_courses": len(catalog),
            "history_keys": len(history_keys),
            "linked_keys": len(history_keys & catalog.keys()),
            "unlinked_keys": len(report.unmatched_keys),
            "registry_courses": len(package.registry),
            "booklet_programmes": sum(1 for c in catalog if c.competency_group is not None or c.navigator_tracks),
            "unlinked_titles": _unlinked_titles(entries, report.unmatched_keys),
        }
    )
    if entries and history_keys and len(report.unmatched_keys) / len(history_keys) > 0.5:
        passport.warn(
            f"Больше половины курсов истории ({len(report.unmatched_keys)} из {len(history_keys)}) "
            "не найдено в каталоге: возможно, выгрузка и каталог относятся к разным периодам."
        )
    profiles = build_profiles(entries)
    _flag_unknown_positions(profiles, report, passport)
    _flag_position_variants(profiles, passport)
    global _LAST_PASSPORT
    _LAST_PASSPORT = passport
    return profiles, catalog, report, passport


def _unlinked_titles(entries, unmatched_keys) -> list[str]:
    """Названия из истории, которых нет в каталоге, -- чтобы «114 из 137» не оставляло вопроса «а остальные?»."""
    wanted = set(unmatched_keys)
    titles: dict[str, str] = {}
    for entry in entries:
        if entry.course_key in wanted and entry.course_key not in titles:
            titles[entry.course_key] = entry.title
    return sorted(titles.values())[:60]


def _flag_position_variants(profiles, passport) -> None:
    """Должности, записанные по-разному, но, судя по всему, одна и та же.

    Регистр и пробелы подбор сглаживает сам (когорты считаются по нормализованной
    должности). Опечатки он сгладить не может: «Начальник отдела» и «Начальник
    отдела-главный бухгалтер» -- разные должности, а «инспеткор» вместо
    «инспектор» -- ошибка в данных. Методисту стоит про такие варианты знать.
    """
    import difflib
    from collections import Counter

    spelled: dict[str, Counter] = {}
    for profile in profiles.values():
        if profile.position and profile.position != "Должность не указана":
            spelled.setdefault(profile.position_norm, Counter())[profile.position] += 1
    case_variants = {norm: c for norm, c in spelled.items() if len(c) > 1}
    if case_variants:
        examples = "; ".join(" / ".join(f"«{v}»" for v in c) for c in list(case_variants.values())[:3])
        passport.warn(
            f"Одна и та же должность записана по-разному (регистр, пробелы) в {len(case_variants)} "
            f"{_plural_ru(len(case_variants), ('случае', 'случаях', 'случаях'))}: {examples}. "
            "Для подбора они объединены, но в списке выбора профиля видны как есть."
        )
    norms = sorted(spelled)
    typos: list[tuple[str, str]] = []
    for i, a in enumerate(norms):
        for b in norms[i + 1 :]:
            if abs(len(a) - len(b)) > 2 or a[:4] != b[:4]:
                continue
            if difflib.SequenceMatcher(None, a, b).ratio() >= 0.93:
                typos.append((a, b))
    if typos:
        shown = "; ".join(f"«{a}» — «{b}»" for a, b in typos[:4])
        passport.warn(
            f"Похожие должности, возможно опечатка ({len(typos)}): {shown}. "
            "Люди с такими должностями не считаются коллегами друг друга — проверьте написание в выгрузке."
        )


def _flag_unknown_positions(profiles, report, passport) -> None:
    """Должности, которых нет в positions.yaml, получают грейд по умолчанию -- говорим об этом вслух."""
    try:
        index = PositionIndex.load(CATALOG_DIR / "positions.yaml")
    except Exception:  # noqa: BLE001 -- проверка файла есть в --doctor
        return
    unknown: dict[str, int] = {}
    for profile in profiles.values():
        if profile.position and profile.position != "Должность не указана" and not index.is_known(profile.position):
            unknown[profile.position] = unknown.get(profile.position, 0) + 1
    report.unknown_positions = sorted(unknown, key=lambda p: -unknown[p])
    report.unknown_position_people = sum(unknown.values())
    report.default_grade = index.resolve("").grade.value
    passport.summary["unknown_positions"] = len(unknown)
    resolved = []
    for position in report.unknown_positions:
        r = index.resolve(position)
        resolved.append(
            {"position": position, "grade": r.grade.value, "how": r.how, "matched": r.matched, "people": unknown[position]}
        )
    report.resolved_positions = resolved
    passport.position_resolution = resolved
    report.possible_homonyms = list(passport.homonym_cases)
    if unknown:
        shown = ", ".join(f"«{p}»" for p in report.unknown_positions[:4])
        passport.warn(
            f"Должностей, которых нет в справочнике positions.yaml: {len(unknown)} (у {sum(unknown.values())} чел.): "
            f"{shown}{'…' if len(unknown) > 4 else ''}. Для них принят грейд по умолчанию "
            f"«{report.default_grade}» (или руководитель, если в названии есть «начальник», «заместитель» и т.п.; "
            f"список в паспорте данных); добавьте их в catalog/positions.yaml, если это не так."
        )


_LAST_PASSPORT = None


def last_passport():
    """Passport of the most recent `load_everything*` call (None before the first)."""
    return _LAST_PASSPORT


def load_everything() -> tuple[dict[str, Profile], Catalog, DataQualityReport]:
    """Load config, both exports and the catalogue in one call."""
    profiles, catalog, report, _passport = load_everything_ex()
    return profiles, catalog, report


def check_data(data_dir: str | None = None) -> int:
    """Print the data passport and the quality report. Returns a process exit code.

    Exit code 1 only when the history could not be read at all.
    """
    _, _, report, passport = load_everything_ex(Path(data_dir) if data_dir else None)
    print(passport.render())
    print()
    print(report.render())
    return 1 if passport.errors else 0


def import_booklet_cmd(path: str, apply: bool, group: str | None, no_llm: bool) -> int:
    """Импорт нового буклета / каталога программ. Без --apply только показывает отчёт «было / стало»."""
    from catalog.import_booklet import (
        BookletError, apply_import, diff_catalogs, import_source, load_catalog_doc, render_diff, to_yaml_document,
    )
    from core import paths as _paths

    config = load_config(PRODUCT_ROOT / "config.yaml")
    aliases = load_aliases(CATALOG_DIR / config.aliases_file)
    client = _client_for(config, group, no_llm=no_llm)
    try:
        profiles, catalog, _quality = load_everything()
        reference = [
            *(entry.title for profile in profiles.values() for entry in profile.entries),
            *(course.title for course in catalog),
        ]
        result = import_source(
            path, llm=client, cache_dir=_paths.OUTPUT_DIR / "booklet_cache", reference_titles=reference
        )
    except BookletError as exc:
        print(f"Импорт не выполнен: {exc}", file=sys.stderr)
        return 1
    old = load_catalog_doc(_paths.active_programs_path())
    print(render_diff(diff_catalogs(old, to_yaml_document(result)), result))
    if not apply:
        print("\nЭто предпросмотр: каталог не менялся. Чтобы применить, добавьте --apply.")
        return 0
    try:
        done = apply_import(result, aliases)
    except BookletError as exc:
        print(f"Импорт не применён: {exc}", file=sys.stderr)
        return 1
    print(f"\nПрименено: {done['programmes']} программ. Прежний каталог в резерве {done['backup']} (откат: --revert-catalog).")
    return 0


def revert_catalog_cmd() -> int:
    from catalog.import_booklet import BookletError, revert_catalog

    try:
        done = revert_catalog()
    except BookletError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(f"Каталог возвращён к состоянию до {done['restored']}; сейчас используется: {done['using']}.")
    return 0


def build_all_tracks(
    profiles: dict[str, Profile],
    catalog: Catalog,
    config: AppConfig,
    client: LLMClient | None = None,
    preferences_by_user: dict[str, Preferences] | None = None,
    interests: InterestIndex | None = None,
) -> dict[str, Track]:
    """Build one track per profile, sharing a single peer index and position
    index across the whole run -- this is what keeps `--track-all` and the
    single-profile `--track` reproducible with each other.

    When `client` is given, each track is passed through the model for ordering
    and wording. The model works inside the pool the engine produced and cannot
    widen it; if it is unreachable the deterministic track is used as-is.
    """
    position_index = PositionIndex.load(CATALOG_DIR / "positions.yaml")
    peer_index = PeerIndex.build(list(profiles.values()))
    stored = preferences_by_user or {}
    tracks = {
        user_id: build_track(
            profile, catalog, peer_index, position_index, config,
            preferences=stored.get(user_id),
            interests=interests,
        )
        for user_id, profile in profiles.items()
    }
    if client is None:
        return tracks
    return {user_id: rerank(track, client, config) for user_id, track in tracks.items()}


def _client_for(config: AppConfig, group: str | None, no_llm: bool = False) -> "LLMClient | None":
    """Build the configured LLM client, or None if the run should stay offline.

    A missing key or an unreachable model is a reason to fall back to the
    deterministic track with a warning, never a reason to fail: the person in
    front of the screen still needs their recommendations.
    """
    if no_llm:
        # One flag, one meaning everywhere: --no-llm skips the model for this
        # run whatever the command. It used to apply only to --serve, so
        # `--track --no-llm` still built a client and warned about a missing
        # key while claiming to run without one.
        return None
    if group:
        config = config.model_copy(update={"model_group": group})
    try:
        return build_client(config)
    except LLMUnavailable as exc:
        print(f"Модель недоступна ({exc}); трек построен без неё.", file=sys.stderr)
        return None


def render_track(track: Track) -> str:
    """Numbered, reasoned track as printed text, in the terms the plan
    requires: должность, ИОГВ, грейд, then each recommendation with why it is
    there."""
    lines = [
        f"{track.user_id}: {track.position}, {track.iogv} (грейд: {track.grade})",
    ]
    if not track.recommendations:
        lines.append("  Трек пуст: нет доступных курсов для этого профиля.")
        return "\n".join(lines)
    for number, rec in enumerate(track.recommendations, start=1):
        lines.append(f"{number}. {rec.title} [{rec.score:.2f}]")
        for reason in rec.reasons:
            lines.append(f"   - {reason}")
    return "\n".join(lines)


_AUTO_CLIENT = object()   # sentinel: build the client from config


def track_one(user_id: str, group: str | None = None, client=_AUTO_CLIENT, no_llm: bool = False) -> int:
    """Print the track for a single profile. Returns a process exit code."""
    profiles, catalog, _ = load_everything()
    profile = profiles.get(user_id)
    if profile is None:
        print(f"Профиль «{user_id}» не найден среди {len(profiles)} загруженных.")
        return 1
    config = load_config(PRODUCT_ROOT / "config.yaml")
    if client is _AUTO_CLIENT:
        client = _client_for(config, group, no_llm)
    # Deterministic tracks for everyone (the peer index needs the whole corpus),
    # but the model is asked about this one profile only. Reranking all 323 to
    # display one track would mean 323 API calls per lookup -- slow, expensive,
    # and enough to trip provider rate limits on its own.
    tracks = build_all_tracks(profiles, catalog, config)
    print(render_track(rerank(tracks[user_id], client, config)))
    return 0


def track_all(out_path: str, group: str | None = None, client=_AUTO_CLIENT, no_llm: bool = False) -> int:
    """Build every profile's track and write them all to a JSON file.

    Feeds Фаза 6 (export); kept here rather than deferred so the whole corpus
    can be inspected without loading each profile through the CLI one by one.
    """
    profiles, catalog, _ = load_everything()
    config = load_config(PRODUCT_ROOT / "config.yaml")
    if client is _AUTO_CLIENT:
        client = _client_for(config, group, no_llm)
    tracks = build_all_tracks(profiles, catalog, config, client)
    payload = [tracks[user_id].model_dump() for user_id in sorted(tracks)]
    Path(out_path).write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"Сохранено {len(payload)} треков в {out_path}")
    return 0


def report_cmd(out_path: str, group: str | None = None, client=_AUTO_CLIENT, no_llm: bool = False) -> int:
    """Write the track report -- JSON or Excel, chosen by `out_path`'s
    extension. Covers the ТЗ requirement "форма отчёта: JSON и Excel"
    (Фаза 6 / Задача 1); `report/tracks.py` does the actual writing.

    `client` follows the same override sentinel as `track_one`/`track_all`
    above: tests pass `client=None` (or a fake) so this never has to touch
    the network to be exercised.
    """
    profiles, catalog, quality = load_everything()
    config = load_config(PRODUCT_ROOT / "config.yaml")
    if client is _AUTO_CLIENT:
        client = _client_for(config, group, no_llm)
    tracks = build_all_tracks(profiles, catalog, config, client)
    path = Path(out_path)
    if path.suffix.lower() == ".json":
        write_json_report(tracks, path)
    else:
        write_excel_report(tracks, catalog, quality, path)
    print(f"Отчёт сохранён в {out_path}")
    return 0


def benchmark_cmd(out_path: str, client_factory=None) -> int:
    """Run the configured sample through every configured model group and
    write the comparison workbook -- the ТЗ-required "Excel файл с
    результатами анализа работы группы ИИ-агентов" (Фаза 6 / Задача 3).

    Every group in `config.yaml`'s `benchmark.groups` is attempted; one that
    cannot be reached comes back `not_measured` (`benchmark.runner.run_group`)
    rather than dropped or zero-filled, so the workbook always accounts for
    every configured group.

    `client_factory` defaults to `build_benchmark_client`, which forces the
    on-disk cache off so a repeat's latency is real; tests pass a fake
    factory here for the same reason `report_cmd` accepts a `client`
    override -- so this path can be exercised without a network call.
    """
    profiles, catalog, _quality = load_everything()
    config = load_config(PRODUCT_ROOT / "config.yaml")
    position_index = PositionIndex.load(CATALOG_DIR / "positions.yaml")
    peer_index = PeerIndex.build(list(profiles.values()))
    sample = sample_user_ids(profiles, config.benchmark_sample_size, config.benchmark_sample_seed)

    if client_factory is None:
        client_factory = lambda g: build_benchmark_client(config, g)

    results = {
        group: run_group(
            group, sample, profiles, catalog, config, position_index, peer_index,
            client_factory=client_factory,
            repeats=config.benchmark_repeats,
        )
        for group in config.benchmark_groups
    }
    write_benchmark_workbook(results, config, sample, out_path)
    print(f"Сравнение моделей сохранено в {out_path}")
    return 0


def doctor(group: str | None = None) -> int:
    """Print the full readiness picture and return 1 if anything is broken.

    This is what `START.bat` runs before it launches anything
    (`python main.py --doctor --group <выбор>`): a return code of 0 is its
    signal that the chosen group is safe to serve, and non-zero means show
    the printed explanation and go back to the menu, never crash mid-launch.
    """
    from diagnostics.checks import Status, render, run_checks

    results = run_checks(group)
    print(render(results))
    return 1 if any(result.status is Status.FAIL for result in results) else 0


def serve(port: int = 8000, group: str | None = None, no_llm: bool = False) -> int:
    """Start the REST API (and, once Задача 5 lands, the web UI) and open it
    in the default browser.

    `api/app.py` loads the corpus once at startup (`api.state.get_state`),
    not per request -- see that module for why. If the API package is
    missing from a half-built checkout, this fails with a plain-Russian
    explanation rather than a traceback.
    """
    if group or no_llm:
        # The launcher menu asks which model group to run with, so the choice
        # has to reach the server. Without this the readiness check passed for
        # the group the person picked while the web UI quietly served whatever
        # config.yaml happened to say. "Без модели" likewise has to switch the
        # model off, not merely skip its readiness check.
        import api.state as api_state

        update = {"llm_enabled": False} if no_llm else {"model_group": group}
        config = load_config(PRODUCT_ROOT / "config.yaml").model_copy(update=update)
        api_state.reset_state()
        api_state.get_state(config=config)
        print("Запуск без модели: трек строится расчётом." if no_llm
              else f"Группа моделей на этот запуск: {group}.")

    try:
        import uvicorn
        from api.app import app
    except ModuleNotFoundError as exc:
        print(
            f"Веб-интерфейс пока не собран в этой поставке (не найден модуль «{exc.name}»).",
            file=sys.stderr,
        )
        print(
            "Пользуйтесь командной строкой: --check-data, --track ИМЯ или --track-all --out ФАЙЛ.",
            file=sys.stderr,
        )
        return 1

    import threading
    import webbrowser

    host = os.environ.get("APP_HOST", "127.0.0.1")
    if host == "127.0.0.1":
        url = f"http://127.0.0.1:{port}"
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    uvicorn.run(app, host=host, port=port)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="ИИ-агент индивидуальной траектории обучения")
    parser.add_argument(
        "--check-data",
        action="store_true",
        help="прочитать выгрузки и напечатать отчёт о качестве данных",
    )
    parser.add_argument(
        "--data-dir",
        metavar="PATH",
        help="папка с пакетом выгрузок для --check-data (по умолчанию data/ рядом с продуктом)",
    )
    parser.add_argument(
        "--track",
        metavar="USER_ID",
        help="построить и напечатать трек обучения для одного профиля",
    )
    parser.add_argument(
        "--track-all",
        action="store_true",
        help="построить треки для всех профилей и сохранить их в JSON (см. --out)",
    )
    parser.add_argument(
        "--out",
        metavar="PATH",
        help="файл для --track-all",
    )
    parser.add_argument(
        "--import-booklet",
        metavar="FILE",
        help="прочитать новый буклет программ (PDF) или каталог (Excel/CSV/Word) и показать отчёт «было / стало»; с --apply применить",
    )
    parser.add_argument("--apply", action="store_true", help="для --import-booklet: применить каталог (прежний уходит в резерв)")
    parser.add_argument("--revert-catalog", action="store_true", help="вернуть каталог программ, каким он был до последнего импорта")
    parser.add_argument(
        "--no-llm",
        action="store_true",
        help="запустить без языковой модели: трек строится только расчётом",
    )
    parser.add_argument(
        "--group",
        choices=["russian", "foreign", "local"],
        help="группа моделей на этот запуск; перекрывает llm.model_group из config.yaml",
    )
    parser.add_argument(
        "--report",
        action="store_true",
        help="построить отчёт по трекам и сохранить его (см. --out; .json или .xlsx по расширению)",
    )
    parser.add_argument(
        "--benchmark",
        action="store_true",
        help="прогнать срез профилей через настроенные группы моделей и сохранить сравнительный Excel (см. --out и секцию benchmark: в config.yaml)",
    )
    parser.add_argument(
        "--doctor",
        action="store_true",
        help="проверить готовность продукта (данные, каталог, выбранная модель) и выйти",
    )
    parser.add_argument(
        "--serve",
        action="store_true",
        help="запустить веб-интерфейс и REST API и открыть их в браузере",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8000,
        metavar="PORT",
        help="порт для --serve (по умолчанию 8000)",
    )
    args = parser.parse_args(argv)

    if args.doctor:
        return doctor(args.group)

    if args.serve:
        return serve(args.port, args.group, args.no_llm)

    if args.check_data:
        return check_data(args.data_dir)

    if args.import_booklet:
        return import_booklet_cmd(args.import_booklet, args.apply, args.group, args.no_llm)

    if args.revert_catalog:
        return revert_catalog_cmd()

    if args.track:
        return track_one(args.track, args.group, no_llm=args.no_llm)

    if args.track_all:
        if not args.out:
            parser.error("--track-all требует --out <файл>")
        return track_all(args.out, args.group, no_llm=args.no_llm)

    if args.report:
        if not args.out:
            parser.error("--report требует --out <файл>")
        return report_cmd(args.out, args.group, no_llm=args.no_llm)

    if args.benchmark:
        if not args.out:
            parser.error("--benchmark требует --out <файл>")
        return benchmark_cmd(args.out)

    parser.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
