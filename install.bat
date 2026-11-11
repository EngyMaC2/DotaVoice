@echo off
rem Установщик DotaVoice для Windows (запуск из исходников).
rem
rem Создаёт виртуальное окружение, ставит зависимости и делает ярлыки
rem на рабочем столе и в меню «Пуск».
rem
rem Если рядом лежит собранный dist\DotaVoice.exe, ярлыки будут указывать на него —
rem тогда приложение запускается как обычная программа, без консоли.
rem
rem Чтобы получить настоящий .exe и установщик .msi, запустите build_windows.bat.
setlocal
set HERE=%~dp0

where python >nul 2>nul
if errorlevel 1 (
    echo.
    echo Не найден Python. Установите Python 3.12 или новее с python.org
    echo и обязательно отметьте галочку "Add python.exe to PATH".
    echo.
    pause
    exit /b 1
)

python "%~dp0install.py" %*
if errorlevel 1 (
    echo.
    echo Установка не удалась. Убедитесь, что установлен Python 3.12 или новее.
    pause
    exit /b 1
)

echo.
echo Установка завершена.
echo Ярлык DotaVoice добавлен на рабочий стол и в меню «Пуск».
echo.
echo Хотите настоящий установщик .msi? Запустите build_windows.bat.
pause
