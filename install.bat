@echo off
rem Установщик DotaVoice для Windows.
rem Создаёт виртуальное окружение, ставит зависимости и ярлык в меню «Пуск».
setlocal
set HERE=%~dp0
python "%~dp0install.py" %*
if errorlevel 1 (
    echo.
    echo Установка не удалась. Убедитесь, что установлен Python 3.11 или новее.
    pause
    exit /b 1
)
echo.
echo Установка завершена. Запустите DotaVoice из меню «Пуск».
pause
