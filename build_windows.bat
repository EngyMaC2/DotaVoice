@echo off
rem Сборка DotaVoice для Windows: .exe (PyInstaller) и .msi (WiX).
rem
rem Результат: dist\DotaVoice.exe и dist\DotaVoice-<версия>.msi.
rem Требуется Python 3.12+ и интернет для установки зависимостей сборки.
setlocal
set HERE=%~dp0
cd /d "%HERE%"

where python >nul 2>nul
if errorlevel 1 (
    echo Не найден Python. Установите Python 3.12 или новее с python.org.
    pause
    exit /b 1
)

echo Ставлю зависимости для сборки...
python -m pip install --upgrade pip pyinstaller
python -m pip install -r requirements.txt
python -m pip install -e .

echo.
echo Собираю .exe и .msi...
python packaging\build_windows.py %*
if errorlevel 1 (
    echo.
    echo Сборка завершилась с ошибкой.
    pause
    exit /b 1
)

echo.
echo Готово. Смотрите папку dist.
pause
