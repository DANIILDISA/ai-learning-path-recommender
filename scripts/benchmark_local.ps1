<#
.SYNOPSIS
    Замеряет локальную группу моделей (Ollama) одной командой.

.DESCRIPTION
    Проверяет, что локальный сервер моделей отвечает (та же проверка, что
    делает --doctor --group local), затем прогоняет через группу `local`
    ту же выборку профилей, что уже используется для сравнения российской
    и зарубежной групп (настройки -- в config.yaml, раздел benchmark), и
    сохраняет результат в output/: Excel в формате сравнительной книги и
    JSON с теми же данными построчно.

    Ничего не отправляет никуда по сети, кроме обращений к локальному
    серверу моделей на этом же компьютере.

.EXAMPLE
    scripts\benchmark_local.ps1
#>

$ErrorActionPreference = 'Stop'

$ProductRoot = Split-Path -Parent $PSScriptRoot

function Resolve-ProductPython {
    param([string]$Root)
    $candidates = @(
        (Join-Path $Root 'runtime\python.exe'),
        (Join-Path $Root 'runtime\python\python.exe')
    )
    foreach ($candidate in $candidates) {
        if (Test-Path $candidate) { return $candidate }
    }
    return 'python'
}

$Python = Resolve-ProductPython -Root $ProductRoot
$MainPy = Join-Path $ProductRoot 'main.py'

Write-Host "Продукт:  $ProductRoot"
Write-Host "Python:   $Python"
Write-Host ""
Write-Host "Проверяю доступность локальной модели (--doctor --group local)..."
Write-Host ""

& $Python $MainPy '--doctor' '--group' 'local'
$doctorExit = $LASTEXITCODE

if ($doctorExit -ne 0) {
    Write-Host ""
    Write-Host "Локальная модель недоступна -- пояснение см. выше." -ForegroundColor Red
    Write-Host "Замер не запущен. Установка и проверка описаны в docs\локальная-модель.md." -ForegroundColor Red
    exit 1
}

Write-Host ""
Write-Host "Локальная модель отвечает. Запускаю замер -- это может занять несколько минут..."
Write-Host ""

$timestamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$outDir = Join-Path $ProductRoot 'output'
New-Item -ItemType Directory -Force -Path $outDir | Out-Null
$xlsxPath = Join-Path $outDir "бенчмарк-локальная-$timestamp.xlsx"
$jsonPath = Join-Path $outDir "бенчмарк-локальная-$timestamp.json"

# Единственный способ измерить ровно группу `local`, не трогая российскую и
# зарубежную: main.py --benchmark всегда прогоняет все группы, перечисленные
# в config.yaml (benchmark.groups), и не принимает --group. Поэтому здесь
# напрямую вызываются те же самые функции, которыми пользуется --benchmark
# (benchmark.runner.run_group, benchmark.report.write_benchmark_workbook) --
# ничего в продукте это не меняет и не подменяет.
$pythonScript = @'
import json
import sys
from dataclasses import asdict

sys.path.insert(0, r"__PRODUCT_ROOT__")

from benchmark.report import write_benchmark_workbook
from benchmark.runner import build_benchmark_client, run_group, sample_user_ids
from core.paths import PRODUCT_ROOT
from core.config import load_config
from engine.grades import PositionIndex
from engine.peers import PeerIndex
from main import load_everything

config = load_config(PRODUCT_ROOT / "config.yaml")
profiles, catalog, _ = load_everything()
position_index = PositionIndex.load(PRODUCT_ROOT / "catalog" / "positions.yaml")
peer_index = PeerIndex.build(list(profiles.values()))
sample = sample_user_ids(profiles, config.benchmark_sample_size, config.benchmark_sample_seed)

result = run_group(
    "local", sample, profiles, catalog, config, position_index, peer_index,
    client_factory=lambda group: build_benchmark_client(config, group),
    repeats=config.benchmark_repeats,
)

write_benchmark_workbook({"local": result}, config, sample, r"__XLSX_PATH__")

payload = {
    "group": result.group,
    "model": result.model,
    "status": result.status,
    "reason": result.reason,
    "sample_size": len(sample),
    "sample": sample,
    "repeats": config.benchmark_repeats,
    "runs": [asdict(run) for run in result.runs],
}
with open(r"__JSON_PATH__", "w", encoding="utf-8") as fh:
    json.dump(payload, fh, ensure_ascii=False, indent=2)

print(f"Статус: {result.status}")
if result.model:
    print(f"Модель: {result.model}")
if result.reason:
    print(f"Причина: {result.reason}")
print(f"Профилей в выборке: {len(sample)}, повторов: {config.benchmark_repeats}")
'@

$pythonScript = $pythonScript.Replace('__PRODUCT_ROOT__', $ProductRoot)
$pythonScript = $pythonScript.Replace('__XLSX_PATH__', $xlsxPath)
$pythonScript = $pythonScript.Replace('__JSON_PATH__', $jsonPath)

$tempScript = Join-Path $env:TEMP "benchmark_local_$([guid]::NewGuid().ToString('N')).py"
Set-Content -Path $tempScript -Value $pythonScript -Encoding utf8

try {
    & $Python $tempScript
    $runExit = $LASTEXITCODE
} finally {
    Remove-Item -Path $tempScript -Force -ErrorAction SilentlyContinue
}

if ($runExit -ne 0) {
    Write-Host ""
    Write-Host "Замер завершился с ошибкой -- см. сообщение python выше." -ForegroundColor Red
    exit 1
}

Write-Host ""
Write-Host "Готово. Результаты сохранены:" -ForegroundColor Green
Write-Host "  Excel: $xlsxPath"
Write-Host "  JSON:  $jsonPath"
Write-Host ""
Write-Host "Оба файла нужно передать тому, кто ведёт сравнительную книгу моделей."
