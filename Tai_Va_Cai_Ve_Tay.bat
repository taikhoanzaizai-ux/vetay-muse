@echo off
setlocal
set "DEST=%LOCALAPPDATA%\VeTayApp"
where git >nul 2>&1
if errorlevel 1 (
  echo Can cai Git for Windows truoc.
  pause
  exit /b 1
)
if not exist "%DEST%\.git" (
  git clone --branch main https://github.com/taikhoanzaizai-ux/vetay-muse.git "%DEST%"
  if errorlevel 1 (
    pause
    exit /b 1
  )
)
call "%DEST%\Cai_Ve_Tay.bat"
