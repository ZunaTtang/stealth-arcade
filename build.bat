@echo off
REM Stealth Arcade - build script. Output: dist\Notepad.exe (~24MB, no console)
REM
REM Always build from Notepad.spec. Passing stealth_puyo.py to PyInstaller
REM makes it overwrite Notepad.spec, losing the Qt module exclusions that
REM cut the exe from 40MB to 24MB.
REM Rename the exe by editing name='Notepad' in Notepad.spec.
REM
REM cmd reads .bat files in the system codepage, so UTF-8 Korean text above
REM chcp would be mangled into broken commands. Everything before chcp is
REM ASCII only; Korean messages come after it.
chcp 65001 > nul
setlocal
cd /d "%~dp0"

echo [1/3] 실행 중인 앱 종료
REM dist 안의 exe 만 고른다. 윈도우 메모장도 이름이 Notepad.exe 라 경로로 가른다.
REM 따옴표 안에서는 ^ 가 탈출 문자가 아니다. | 를 ^| 로 적으면 PowerShell 이
REM 그대로 받아 구문 오류가 나고, 아무 말 없이 지나가 버린다.
powershell -NoProfile -Command "$t = Join-Path '%~dp0' 'dist\Notepad.exe'; $hit = @(Get-Process Notepad -ErrorAction SilentlyContinue | Where-Object { $_.Path -eq $t }); if ($hit.Count) { foreach ($p in $hit) { Write-Host ('  종료: PID ' + $p.Id); try { $p.Kill(); $p.WaitForExit(5000) } catch {} } } else { Write-Host '  실행 중인 앱 없음' }"
if errorlevel 1 goto fail

echo [2/3] 의존성 확인
python -m pip install --user --quiet PyQt5 pyinstaller
if errorlevel 1 goto fail

echo [3/3] 빌드
python -m PyInstaller --noconfirm Notepad.spec
if errorlevel 1 goto fail

echo.
echo 완료: dist\Notepad.exe
echo 설정은 %%APPDATA%%\StealthPuyo\config.json 에 저장됩니다.
goto end

:fail
echo.
echo 빌드 실패. 위 메시지를 확인하세요.
echo dist\Notepad.exe 가 잠겨 있다면 앱이 아직 떠 있는 것입니다.
echo 트레이 아이콘에서 종료하거나 Ctrl+Alt+Q 를 누른 뒤 다시 실행하세요.
exit /b 1

:end
endlocal
