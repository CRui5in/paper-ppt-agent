@echo off
setlocal

set "ROOT=%~dp0"
if "%ROOT:~-1%"=="\" set "ROOT=%ROOT:~0,-1%"
set "FRONTEND_DIR=%ROOT%\frontend"
set "DIST_ROOT=%ROOT%\dist"
set "DIST_NAME=PaperPPTAgent"
set "WORK_ROOT=%ROOT%\build"
set "SPEC_FILE=paper_ppt_agent.spec"

where uv >nul 2>nul
if errorlevel 1 (
  echo uv was not found in the current shell environment.
  echo Install uv first, then run build-exe.bat again.
  exit /b 1
)

where npm >nul 2>nul
if errorlevel 1 (
  echo npm was not found in the current shell environment.
  echo Install Node.js and npm first, then run build-exe.bat again.
  exit /b 1
)

echo [1/4] Syncing backend dependencies
pushd "%ROOT%"
call uv sync --locked
if errorlevel 1 (
  popd
  exit /b 1
)
popd

if not exist "%FRONTEND_DIR%\node_modules" (
  echo [2/4] Installing frontend dependencies
  pushd "%FRONTEND_DIR%"
  call npm install
  if errorlevel 1 (
    popd
    exit /b 1
  )
  popd
)

echo [3/4] Building frontend
pushd "%FRONTEND_DIR%"
call npm run build
if errorlevel 1 (
  popd
  exit /b 1
)
popd

if exist "%DIST_ROOT%\%DIST_NAME%" (
  echo Cleaning previous output in %DIST_ROOT%\%DIST_NAME%
  rmdir /s /q "%DIST_ROOT%\%DIST_NAME%"
)

if exist "%WORK_ROOT%" (
  echo Cleaning previous build cache in %WORK_ROOT%
  rmdir /s /q "%WORK_ROOT%"
)

echo [4/4] Packaging Windows executable to %DIST_ROOT%\%DIST_NAME%
pushd "%ROOT%"
call uv run --with pyinstaller pyinstaller --noconfirm --clean --distpath "%DIST_ROOT%" --workpath "%WORK_ROOT%" "%SPEC_FILE%"
if errorlevel 1 (
  popd
  exit /b 1
)
popd

if exist "%WORK_ROOT%" (
  echo Cleaning temporary build cache in %WORK_ROOT%
  rmdir /s /q "%WORK_ROOT%"
)

echo.
echo Build completed.
echo Output folder: %DIST_ROOT%\%DIST_NAME%
echo Main executable: %DIST_ROOT%\%DIST_NAME%\PaperPPTAgent.exe

endlocal
