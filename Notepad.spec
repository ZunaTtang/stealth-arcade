# -*- mode: python ; coding: utf-8 -*-
"""Stealth Puyo 빌드 설정.

    python -m PyInstaller --noconfirm Notepad.spec

이 앱은 QtCore / QtGui / QtWidgets / QtNetwork 만 쓴다. PyInstaller 의 PyQt5
훅은 Qt5 bin 폴더를 거의 통째로 담기 때문에, 확실히 쓰지 않는 것만 아래에서
걷어낸다(40MB -> 22MB).

DROP_BINARIES 는 보수적으로 유지한다. QtWidgets.pyd 는 Qt5PrintSupport 처럼
겉보기에 안 쓰는 DLL 에도 링크되어 있어서, 많이 떼어내면 실행 즉시 죽는다.
문제가 생기면 DROP_BINARIES = [] 로 두고 다시 빌드하면 원래대로 돌아온다.
"""

# 이름에 이 조각이 들어간 번들 파일은 담지 않는다.
DROP_BINARIES = [
    # 소프트웨어 OpenGL 대체 구현(21MB). 위젯 화면은 래스터로 그리므로
    # OpenGL 문맥을 만들지 않는다. ANGLE(libGLESv2/d3dcompiler)은 남겨 둔다.
    "opengl32sw.dll",
    # QML / Quick — 이 앱에는 QML 이 한 줄도 없다
    "Qt5Qml.dll",
    "Qt5QmlModels.dll",
    "Qt5QmlWorkerScript.dll",
    "Qt5Quick.dll",
    "Qt5QuickControls2.dll",
    "Qt5QuickParticles.dll",
    "Qt5QuickShapes.dll",
    "Qt5QuickTemplates2.dll",
    "Qt5QuickTest.dll",
    "Qt5QuickWidgets.dll",
    # OpenSSL — 단일 인스턴스 확인에 QLocalServer(이름 있는 파이프)만 쓰고
    # TLS 통신은 하지 않는다. Qt5Network 가 실행 중에 필요할 때만 찾는 DLL.
    "libcrypto-", "libssl-", "libeay32.dll", "ssleay32.dll",
]

EXCLUDES = [
    "PyQt5.QtQml", "PyQt5.QtQuick", "PyQt5.QtQuickWidgets",
    "PyQt5.QtWebEngine", "PyQt5.QtWebEngineWidgets", "PyQt5.QtWebEngineCore",
    "PyQt5.QtMultimedia", "PyQt5.QtMultimediaWidgets",
    "PyQt5.QtBluetooth", "PyQt5.QtNfc", "PyQt5.QtPositioning",
    "PyQt5.QtLocation", "PyQt5.QtSensors", "PyQt5.QtSerialPort",
    "PyQt5.QtSql", "PyQt5.QtTest", "PyQt5.QtDesigner", "PyQt5.QtHelp",
    "PyQt5.QtXmlPatterns",
    "tkinter", "unittest", "pydoc", "doctest", "lib2to3",
    "numpy", "PIL",
]


def keep(entry):
    name = entry[0]
    return not any(bad.lower() in name.lower() for bad in DROP_BINARIES)


a = Analysis(
    ['stealth_puyo.py'],
    pathex=[],
    binaries=[],
    datas=[],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=EXCLUDES,
    noarchive=False,
)
a.binaries = TOC([e for e in a.binaries if keep(e)])
a.datas = TOC([e for e in a.datas if keep(e)])

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='Notepad',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
