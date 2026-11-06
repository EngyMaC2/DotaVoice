#!/bin/sh
# Установщик DotaVoice для Linux и macOS.
# Создаёт виртуальное окружение, ставит зависимости и ярлык запуска.
set -e
HERE=$(cd "$(dirname "$0")" && pwd)
PYTHON=${PYTHON:-python3}

if ! command -v "$PYTHON" >/dev/null 2>&1; then
    echo "Не найден $PYTHON. Установите Python 3.11 или новее и повторите."
    exit 1
fi

"$PYTHON" "$HERE/install.py" "$@"
