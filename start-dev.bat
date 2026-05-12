@echo off
setlocal

set "ROOT=%~dp0"
if "%ROOT:~-1%"=="\" set "ROOT=%ROOT:~0,-1%"
set "FRONTEND_DIR=%ROOT%\frontend"
set "FRONTEND_URL=http://127.0.0.1:5173"

where uv >nul 2>nul
if errorlevel 1 (
  echo uv was not found in the current shell environment.
  echo Install uv or open the shell where uv works, then run start-dev.bat again.
  exit /b 1
)

where npm >nul 2>nul
if errorlevel 1 (
  echo npm was not found in the current shell environment.
  echo Open the shell where npm works, cd into this project, and run start-dev.bat again.
  exit /b 1
)

echo ==> Syncing backend dependencies with uv
pushd "%ROOT%"
call uv sync --locked
if errorlevel 1 (
  popd
  exit /b 1
)
popd

if not exist "%FRONTEND_DIR%\node_modules" (
  echo ==> Installing frontend dependencies
  pushd "%FRONTEND_DIR%"
  call npm install
  if errorlevel 1 (
    popd
    exit /b 1
  )
  popd
)

echo ==> Starting backend
start "Paper PPT Agent Backend" cmd /k "cd /d ""%ROOT%"" && set PYTHONUNBUFFERED=1 && uv run python -m uvicorn backend.app:app --host 127.0.0.1 --port 8000 --reload --reload-dir backend --reload-include=*.py"

echo ==> Starting frontend
start "Paper PPT Agent Frontend" cmd /k "cd /d ""%FRONTEND_DIR%"" && npm run dev -- --host 127.0.0.1 --port 5173 --strictPort --open"

echo.
echo Paper PPT Agent is starting:
echo   Backend:  http://127.0.0.1:8000
echo   Frontend: %FRONTEND_URL%

endlocal
