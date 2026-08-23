@echo off
rem =====================================================================
rem VintedBot - launcher per Windows Task Scheduler.
rem
rem Perche' un .cmd e non un .ps1: l'azione del task esegue direttamente
rem questo file, senza deroghe alla ExecutionPolicy e senza un processo
rem PowerShell in mezzo. La finestra non compare perche' il task gira in
rem sessione 0 (LogonType S4U), NON per merito di questo script.
rem
rem Ogni percorso e' assoluto: %~dp0 e' la cartella di QUESTO file, quindi
rem non dipende dalla directory corrente - sotto Task Scheduler la cwd non
rem e' quella del progetto.
rem
rem L'unico compito qui e' il preflight: cio' che puo' fallire DOPO che
rem Python e' partito (searches.toml, credenziali) lo gestisce la CLI, che
rem sa dare messaggi migliori e sa scrivere nel log strutturato.
rem =====================================================================
setlocal EnableExtensions

rem Cartella del progetto = genitore di scripts\, normalizzata ad assoluta.
for %%I in ("%~dp0..") do set "PROJECT_DIR=%%~fI"

set "PYTHON_EXE=%PROJECT_DIR%\.venv\Scripts\python.exe"
set "ENV_FILE=%PROJECT_DIR%\.env"
set "LAUNCHER_LOG=%PROJECT_DIR%\data\logs\launcher.log"

if not exist "%PROJECT_DIR%\data\logs" mkdir "%PROJECT_DIR%\data\logs" 2>nul

if not exist "%PYTHON_EXE%" (
    call :fail "ambiente virtuale mancante: %PYTHON_EXE% - esegui 'uv sync' nel progetto"
    exit /b 2
)

if not exist "%ENV_FILE%" (
    call :fail "configurazione mancante: %ENV_FILE% - copia .env.example in .env"
    exit /b 2
)

rem Marca la provenienza: la CLI la registra nei log come trigger=scheduler.
set "VINTEDBOT_INVOKED_BY=scheduler"

cd /d "%PROJECT_DIR%"
"%PYTHON_EXE%" -m vintedbot run-all
rem L'exit code di Python e' l'unica cosa che il Task Scheduler mostra in
rem "Ultimo risultato esecuzione": va propagato intatto.
exit /b %ERRORLEVEL%

:fail
echo [%date% %time%] run-all NON avviato: %~1>>"%LAUNCHER_LOG%"
echo run-all NON avviato: %~1 1>&2
goto :eof
