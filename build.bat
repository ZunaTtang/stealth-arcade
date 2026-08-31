@echo off
REM Stealth Puyo - exe 빌드 스크립트
REM 결과물: dist\Notepad.exe  (콘솔 창 없음, 단일 파일, 약 24MB)
REM
REM 반드시 Notepad.spec 으로 빌드한다.
REM 예전처럼 stealth_puyo.py 를 직접 넘기면 PyInstaller 가 Notepad.spec 을
REM 새로 만들어 덮어써 버린다. 그러면 쓰지 않는 Qt 모듈을 걷어내는 설정이
REM 사라져 결과물이 40MB 로 돌아가고, 스펙 파일의 주석과 목록도 날아간다.
REM
REM 실행 파일 이름을 바꾸려면 Notepad.spec 의 name='Notepad' 를 고치세요.

setlocal

echo [1/2] 의존성 확인
python -m pip install --user PyQt5 pyinstaller || goto :fail

echo [2/2] 빌드
python -m PyInstaller --noconfirm Notepad.spec || goto :fail

echo.
echo 완료: dist\Notepad.exe
echo 설정은 %%APPDATA%%\StealthPuyo\config.json 에 저장됩니다.
goto :eof

:fail
echo.
echo 빌드 실패. 위 메시지를 확인하세요.
echo 파일 접근이 거부되었다면, 실행 중인 앱이 dist\Notepad.exe 를 잠그고
echo 있을 수 있습니다. Ctrl+Alt+Q 로 종료한 뒤 다시 실행하세요.
exit /b 1
