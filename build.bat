@echo off
REM Stealth Puyo - exe 빌드 스크립트
REM 결과물: dist\Notepad.exe  (콘솔 창 없음, 단일 파일)
REM 실행 파일 이름을 바꾸려면 아래 APPNAME 만 고치세요.

setlocal
set APPNAME=Notepad

echo [1/2] 의존성 확인
python -m pip install --user PyQt5 pyinstaller || goto :fail

echo [2/2] 빌드
python -m PyInstaller --noconfirm --onefile --windowed ^
    --name %APPNAME% stealth_puyo.py || goto :fail

echo.
echo 완료: dist\%APPNAME%.exe
echo 설정은 %%APPDATA%%\StealthPuyo\config.json 에 저장됩니다.
goto :eof

:fail
echo.
echo 빌드 실패. 위 메시지를 확인하세요.
exit /b 1
