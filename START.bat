@echo off
chcp 65001 >nul
cd /d "%~dp0"

if exist "%~dp0runtime\python\python.exe" (
    set "PYTHON=%~dp0runtime\python\python.exe"
) else if exist "%~dp0runtime\python.exe" (
    set "PYTHON=%~dp0runtime\python.exe"
) else (
    set "PYTHON=python"
)

:menu
cls
echo   ИИ-агент индивидуальной траектории обучения
echo   ===========================================
echo    1. Российская модель (GigaChat)
echo    2. Зарубежная модель (OpenRouter)
echo    3. Локальная модель (Ollama на этом компьютере)
echo    4. Без модели -- только расчёт (работает всегда)
echo    5. Проверить готовность и выйти
echo.
set "CHOICE="
set /p CHOICE="Выберите пункт (1-5): "

if "%CHOICE%"=="1" (
    set "GROUP=russian"
    goto check_and_launch
)
if "%CHOICE%"=="2" (
    set "GROUP=foreign"
    goto check_and_launch
)
if "%CHOICE%"=="3" (
    set "GROUP=local"
    goto check_and_launch
)
if "%CHOICE%"=="4" (
    set "GROUP="
    set "NOLLM=1"
    goto check_and_launch
)
if "%CHOICE%"=="5" (
    goto diagnose_only
)

echo.
echo Не понял выбор. Введите число от 1 до 5.
echo.
pause
goto menu

:check_and_launch
echo.
if defined GROUP (
    "%PYTHON%" main.py --doctor --group %GROUP%
) else (
    "%PYTHON%" main.py --doctor
)
if errorlevel 1 (
    echo.
    echo Продукт не готов к запуску в этом режиме -- пояснение см. выше.
    echo Устраните проблему по подсказке или выберите другой пункт меню.
    echo.
    pause
    goto menu
)

echo.
echo Всё готово. Запускаю веб-интерфейс...
echo Чтобы остановить продукт, закройте это окно.
echo.
if defined GROUP (
    "%PYTHON%" main.py --serve --group %GROUP%
) else (
    "%PYTHON%" main.py --serve --no-llm
)
echo.
echo Продукт остановлен.
pause
goto menu

:diagnose_only
echo.
"%PYTHON%" main.py --doctor
echo.
pause
exit /b 0
