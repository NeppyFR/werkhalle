@echo off
setlocal
cd /d "%~dp0"
set "PYTHONPATH=%CD%\src"
set "PYTHONUTF8=1"

set "BASE_PY="
py -3.13 -c "import sys; raise SystemExit(0 if sys.version_info[:2] == (3, 13) else 1)" >nul 2>&1
if not errorlevel 1 set "BASE_PY=py -3.13"
if defined BASE_PY goto have_python
python -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 13) else 1)" >nul 2>&1
if not errorlevel 1 set "BASE_PY=python"
if defined BASE_PY goto have_python
echo.
echo Python 3.13 wurde nicht gefunden. Bitte installieren: https://www.python.org/downloads/
goto fail

:have_python
if exist ".venv\Scripts\python.exe" goto have_venv
echo Lege die virtuelle Umgebung .venv an ...
%BASE_PY% -m venv .venv
if errorlevel 1 goto venv_failed

:have_venv
rem An interrupted first start leaves python.exe without pip.
".venv\Scripts\python.exe" -m pip --version >nul 2>&1
if not errorlevel 1 goto venv_ok
if defined VENV_CLEARED goto venv_failed
set "VENV_CLEARED=1"
echo Die virtuelle Umgebung .venv ist unvollstaendig und wird neu angelegt ...
%BASE_PY% -m venv --clear .venv
if errorlevel 1 goto venv_failed
goto have_venv

:venv_ok
".venv\Scripts\python.exe" -c "import hashlib; print(hashlib.sha256(open('requirements.txt', 'rb').read()).hexdigest())" > ".venv\.deps-new"
if errorlevel 1 goto hash_failed
fc /b ".venv\.deps-new" ".venv\.deps-ok" >nul 2>&1
if not errorlevel 1 goto have_deps
echo Installiere die Abhaengigkeiten aus requirements.txt ...
".venv\Scripts\python.exe" -m pip install --disable-pip-version-check -r requirements.txt
if errorlevel 1 goto pip_failed
copy /y ".venv\.deps-new" ".venv\.deps-ok" >nul

:have_deps
del ".venv\.deps-new" >nul 2>&1
".venv\Scripts\python.exe" -m werkhalle %*
if errorlevel 1 goto run_failed
exit /b 0

:venv_failed
echo.
echo Die virtuelle Umgebung konnte nicht angelegt werden.
goto fail

:hash_failed
echo.
echo Die virtuelle Umgebung .venv ist beschaedigt. Bitte den Ordner .venv loeschen und neu starten.
goto fail

:pip_failed
echo.
echo Die Abhaengigkeiten konnten nicht installiert werden. Bitte die Internetverbindung pruefen.
goto fail

:run_failed
echo.
echo Die Werkhalle wurde mit einem Fehler beendet. Details stehen im Ordner logs.
goto fail

:fail
pause
exit /b 1
