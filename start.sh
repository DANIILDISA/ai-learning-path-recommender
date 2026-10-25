#!/usr/bin/env bash
# Interactive launcher for macOS/Linux -- the non-Windows twin of START.bat.
set -u
cd "$(dirname "${BASH_SOURCE[0]}")"

if [ -x "runtime/bin/python3" ]; then
    PYTHON="runtime/bin/python3"
elif [ -x "runtime/bin/python" ]; then
    PYTHON="runtime/bin/python"
elif command -v python3 >/dev/null 2>&1; then
    PYTHON="python3"
else
    PYTHON="python"
fi

menu() {
    clear
    echo "  ИИ-агент индивидуальной траектории обучения"
    echo "  ==========================================="
    echo "   1. Российская модель (GigaChat)"
    echo "   2. Зарубежная модель (OpenRouter)"
    echo "   3. Локальная модель (Ollama на этом компьютере)"
    echo "   4. Без модели -- только расчёт (работает всегда)"
    echo "   5. Проверить готовность и выйти"
    echo
    read -r -p "Выберите пункт (1-5): " CHOICE
}

check_and_launch() {
    local group="$1"
    echo
    if [ -n "$group" ]; then
        "$PYTHON" main.py --doctor --group "$group"
    else
        "$PYTHON" main.py --doctor
    fi
    if [ $? -ne 0 ]; then
        echo
        echo "Продукт не готов к запуску в этом режиме -- пояснение см. выше."
        echo "Устраните проблему по подсказке или выберите другой пункт меню."
        echo
        read -r -p "Нажмите Enter, чтобы вернуться в меню..." _
        return
    fi

    echo
    echo "Всё готово. Запускаю веб-интерфейс..."
    echo "Чтобы остановить продукт, нажмите Ctrl+C."
    echo
    if [ -n "$group" ]; then
        "$PYTHON" main.py --serve --group "$group"
    else
        "$PYTHON" main.py --serve
    fi
    echo
    echo "Продукт остановлен."
    read -r -p "Нажмите Enter, чтобы вернуться в меню..." _
}

while true; do
    menu
    case "$CHOICE" in
        1) check_and_launch "russian" ;;
        2) check_and_launch "foreign" ;;
        3) check_and_launch "local" ;;
        4) check_and_launch "" ;;
        5)
            echo
            "$PYTHON" main.py --doctor
            echo
            read -r -p "Нажмите Enter, чтобы выйти..." _
            exit 0
            ;;
        *)
            echo
            echo "Не понял выбор. Введите число от 1 до 5."
            echo
            read -r -p "Нажмите Enter, чтобы продолжить..." _
            ;;
    esac
done
