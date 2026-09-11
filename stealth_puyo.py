# -*- coding: utf-8 -*-
"""
Stealth Puyo  -  Windows / PyQt5 단일 파일 뿌요뿌요

설치 :  pip install PyQt5
실행 :  python stealth_puyo.py
빌드 :  pyinstaller --noconfirm --onefile --windowed --name Notepad stealth_puyo.py

설정 저장 위치 : %APPDATA%\\StealthPuyo\\config.json

Stealth TXT Reader 와 같은 은폐 구조를 그대로 이어받았다.
  · 전역 핫키(Ctrl+Alt+Z)로 어느 창에 있든 즉시 숨기고 복귀
  · Esc 즉시 숨기기, 포커스를 잃으면 자동 숨기기(옵션)
  · 배경 지우기 — 창 배경 알파를 0 까지 내려 뿌요만 떠 있게 만든다
  · 다른 창을 쓰는 중에도 전역 조작키로 플레이 / 빠른 재시작
  · Qt.Tool 창 모드로 작업 표시줄/Alt+Tab 목록에서 제외, 트레이 상주
"""

import ctypes
import ctypes.wintypes
import itertools
import json
import math
import os
import random
import sys
import tempfile
import time

from PyQt5.QtCore import (
    QAbstractNativeEventFilter, QDataStream, QEvent, QPoint, QPointF, QRect,
    QRectF, Qt, QTimer, pyqtSignal,
)
from PyQt5.QtGui import (
    QColor, QCursor, QFont, QFontMetrics, QIcon, QKeySequence, QPainter,
    QPainterPath, QPalette, QPen, QPixmap, QRadialGradient,
)
from PyQt5.QtNetwork import QLocalServer, QLocalSocket
from PyQt5.QtWidgets import (
    QAction, QApplication, QCheckBox, QColorDialog, QComboBox, QDialog,
    QDialogButtonBox, QDoubleSpinBox, QFormLayout, QHBoxLayout, QKeySequenceEdit,
    QLabel, QLineEdit, QMenu, QPushButton, QScrollArea, QSizePolicy, QSlider,
    QSpinBox,
    QSystemTrayIcon, QTabWidget, QVBoxLayout, QWidget,
)

APP_NAME = "StealthPuyo"
UI_FONT = "Malgun Gothic"   # resolve_ui_font() 에서 실제 있는 글꼴로 바꾼다


def resolve_ui_font():
    """한글 글리프가 있는 글꼴을 하나 골라 둔다.

    QFont() 기본값은 한글이 없는 글꼴로 잡히는 경우가 있고,
    QPainterPath.addText 는 글꼴 대체를 하지 않아 한글이 □ 로 나온다.
    """
    global UI_FONT
    from PyQt5.QtGui import QFontDatabase, QFontInfo
    db = QFontDatabase()
    for name in ("Malgun Gothic", "맑은 고딕", "Gulim", "굴림", "Dotum",
                 "돋움", "Segoe UI"):
        family = QFontInfo(QFont(name)).family()
        if QFontDatabase.Korean in db.writingSystems(family):
            UI_FONT = family
            return UI_FONT
    UI_FONT = QFontInfo(QFont()).family()
    return UI_FONT
IPC_KEY = "StealthPuyo.SingleInstance.v1"

# ---------------------------------------------------------------- Win32 상수
WM_HOTKEY = 0x0312
MOD_ALT, MOD_CONTROL, MOD_SHIFT, MOD_NOREPEAT = 0x0001, 0x0002, 0x0004, 0x4000
MOD_WIN = 0x0008

# Qt 키 코드 -> Windows 가상 키 코드. 영문자·숫자는 두 체계가 값이 같아 따로 안 쓴다.
QT_TO_VK = {
    Qt.Key_Space: 0x20, Qt.Key_PageUp: 0x21, Qt.Key_PageDown: 0x22,
    Qt.Key_End: 0x23, Qt.Key_Home: 0x24, Qt.Key_Left: 0x25, Qt.Key_Up: 0x26,
    Qt.Key_Right: 0x27, Qt.Key_Down: 0x28, Qt.Key_Insert: 0x2D,
    Qt.Key_Delete: 0x2E, Qt.Key_Escape: 0x1B, Qt.Key_Tab: 0x09,
    Qt.Key_Backspace: 0x08, Qt.Key_Return: 0x0D, Qt.Key_Enter: 0x0D,
    Qt.Key_Semicolon: 0xBA, Qt.Key_Equal: 0xBB, Qt.Key_Comma: 0xBC,
    Qt.Key_Minus: 0xBD, Qt.Key_Period: 0xBE, Qt.Key_Slash: 0xBF,
    Qt.Key_QuoteLeft: 0xC0, Qt.Key_BracketLeft: 0xDB, Qt.Key_Backslash: 0xDC,
    Qt.Key_BracketRight: 0xDD, Qt.Key_Apostrophe: 0xDE,
}
for _i in range(12):
    QT_TO_VK[Qt.Key_F1 + _i] = 0x70 + _i

MOD_MASK = int(Qt.ControlModifier | Qt.AltModifier |
               Qt.ShiftModifier | Qt.MetaModifier)


def seq_to_combo(seq_text):
    """'Ctrl+B' -> keyPressEvent 와 비교할 수 있는 정수. 빈 값이면 None."""
    seq = QKeySequence(seq_text or "")
    if seq.isEmpty():
        return None
    return int(seq[0])


def seq_to_win(seq_text):
    """'Ctrl+Alt+Z' -> (수식키 비트, 가상 키). 전역 등록이 불가능하면 None."""
    combo = seq_to_combo(seq_text)
    if combo is None:
        return None
    qt_mods = combo & MOD_MASK
    qt_key = combo & ~int(Qt.KeyboardModifierMask)

    mods = 0
    if qt_mods & Qt.ControlModifier:
        mods |= MOD_CONTROL
    if qt_mods & Qt.AltModifier:
        mods |= MOD_ALT
    if qt_mods & Qt.ShiftModifier:
        mods |= MOD_SHIFT
    if qt_mods & Qt.MetaModifier:
        mods |= MOD_WIN
    if not mods:
        return None            # 수식키 없는 전역 핫키는 위험해서 막는다

    if qt_key in QT_TO_VK:
        vk = QT_TO_VK[qt_key]
    elif 0x30 <= qt_key <= 0x39 or 0x41 <= qt_key <= 0x5A:
        vk = qt_key
    else:
        return None
    return mods, vk


# 편집 가능한 동작 목록 — (id, 라벨, 기본 키)
LOCAL_ACTIONS = [
    ("left",           "왼쪽 이동",         "Left"),
    ("right",          "오른쪽 이동",       "Right"),
    ("soft",           "빠른 낙하",         "Down"),
    ("rot_cw",         "시계 방향 회전",    "Up"),
    ("rot_cw2",        "시계 방향 (보조)",  "X"),
    ("rot_ccw",        "반시계 방향 회전",  "Z"),
    ("hard",           "즉시 낙하",         "Space"),
    ("hold",           "홀드",              "C"),
    ("rot_180",        "180도 회전",        "A"),
    ("cur_left",       "커서 왼쪽",         "Left"),
    ("cur_right",      "커서 오른쪽",       "Right"),
    ("cur_up",         "커서 위",           "Up"),
    ("cur_down",       "커서 아래",         "Down"),
    ("num_1",          "숫자 1 넣기",       "1"),
    ("num_2",          "숫자 2 넣기",       "2"),
    ("num_3",          "숫자 3 넣기",       "3"),
    ("num_4",          "숫자 4 넣기",       "4"),
    ("num_5",          "숫자 5 넣기",       "5"),
    ("num_6",          "숫자 6 넣기",       "6"),
    ("num_7",          "숫자 7 넣기",       "7"),
    ("num_8",          "숫자 8 넣기",       "8"),
    ("num_9",          "숫자 9 넣기",       "9"),
    ("reveal",         "칸 열기",           "Space"),
    ("flag",           "깃발 꽂기",         "F"),
    ("chord",          "주변 한꺼번에",     "D"),
    ("note",           "메모 모드",         "N"),
    ("erase",          "지우기",            "Delete"),
    ("erase2",         "지우기 (보조)",     "Backspace"),
    ("undo",           "되돌리기",          "Ctrl+Z"),
    ("hint",           "힌트",              "H"),
    ("pause",          "일시정지",          "P"),
    ("new_game",       "새 게임",           "F2"),
    ("restart",        "빠른 재시작",       "R"),
    ("hide",           "즉시 숨기기",       "Esc"),
    ("settings",       "설정 열기",         "F1"),
    ("menu",           "메뉴 열기",         "Ctrl+R"),
    ("toggle_bg",      "배경 지우기 토글",  "Ctrl+B"),
    ("toggle_grid",    "격자선 토글",       "Ctrl+G"),
    ("opacity_up",     "더 진하게",         "Ctrl+]"),
    ("opacity_down",   "더 투명하게",       "Ctrl+["),
    ("cell_up",        "화면 크게",         "Ctrl+Up"),
    ("cell_down",      "화면 작게",         "Ctrl+Down"),
    ("toggle_top",     "항상 위 토글",      "Ctrl+T"),
    ("toggle_panel",   "사이드 패널 토글",  "Ctrl+H"),
    ("toggle_topbar",  "상단바 토글",       "Ctrl+J"),
]

GLOBAL_ACTIONS = [
    ("g_hide",    "숨기기 / 복귀",     "Ctrl+Alt+Z"),
    # Ctrl+Alt+R 은 다른 프로그램이 자주 선점해서, 앱 키(F2)와 짝이 되는 조합으로 둔다
    ("g_restart", "빠른 재시작",       "Ctrl+Alt+F2"),
    ("g_bg",      "배경 지우기 토글",  "Ctrl+Alt+B"),
    ("g_pause",   "일시정지 토글",     "Ctrl+Alt+P"),
    ("g_left",    "왼쪽 이동",         "Ctrl+Alt+Left"),
    ("g_right",   "오른쪽 이동",       "Ctrl+Alt+Right"),
    ("g_soft",    "빠른 낙하",         "Ctrl+Alt+Down"),
    ("g_rot",     "시계 방향 회전",    "Ctrl+Alt+Up"),
    ("g_hard",    "즉시 낙하",         "Ctrl+Alt+Return"),
    ("g_hold",    "홀드",              "Ctrl+Alt+H"),
    ("g_quit",    "종료",              "Ctrl+Alt+Q"),
]

DEFAULT_KEYS = {aid: key for aid, _label, key in LOCAL_ACTIONS + GLOBAL_ACTIONS}
ACTION_LABELS = {aid: label for aid, label, _key in LOCAL_ACTIONS + GLOBAL_ACTIONS}

# ================================================================= 표시 언어
# 화면에 보이는 글만 바꾼다. 설정 파일에 담기는 값(게임 이름, 난이도 key 등)은
# 그대로 두므로, 언어를 오가도 설정과 기록이 섞이지 않는다.
LANG = "ko"
LANG_NAMES = [("ko", "한국어"), ("en", "English")]
_TR_MISS = set()          # 번역이 없어 그냥 내보낸 글 (점검용)

TR = {
    # ---- 공용 ----
    "설정": "Settings",
    "닫기": "Close",
    "화면": "Screen",
    "게임": "Game",
    "단축키": "Keys",
    "규칙": "Rules",
    "모드": "Mode",
    "난이도": "Difficulty",
    "없음": "none",
    "(없음)": "(none)",
    "해제": "Clear",
    "기본": "Reset",
    "이 동작에 키를 지정하지 않음": "Leave this action unbound",
    "기본값 %s 으로": "Back to default %s",
    "모든 키를 기본값으로": "Reset all keys to defaults",
    "<b>앱 단축키</b> (창이 활성일 때)": "<b>App keys</b> (when the window is active)",
    "<b>전역 핫키</b> (다른 창에 있어도 동작 · 수식키 필수)":
        "<b>Global hotkeys</b> (work from any window · modifier required)",
    # ---- 화면 탭 ----
    "셀 크기": "Cell size",
    "배경색": "Background colour",
    "배경 진하기 (0 = 배경 지우기)": "Background opacity (0 = erase background)",
    "격자선": "Grid lines",
    "창 투명도": "Window opacity",
    "항상 위": "Always on top",
    "테두리 없음": "Frameless",
    "상단바 표시": "Show top bar",
    "사이드 패널 표시": "Show side panel",
    "착지 위치 표시": "Show landing ghost",
    "숨기기": "Hide",
    "재시작": "Restart",
    "상단바 아이콘": "Top bar icons",
    "숨은 창 (작업 표시줄·Alt+Tab 에서 제외)":
        "Hidden window (kept out of the taskbar and Alt+Tab)",
    "위장 창 (다른 제목으로 표시)": "Disguised window (shows another title)",
    "보통 창": "Normal window",
    "창 모드": "Window mode",
    "위장 제목": "Disguise title",
    "포커스를 잃으면 자동으로 숨기기": "Hide automatically when focus is lost",
    "숨길 때 자동 일시정지": "Pause automatically while hidden",
    "낙하 속도": "Fall speed",
    "표시 언어": "Language",
    '6×12 필드 + 숨은 13번째 줄, 3열이 막히면 게임 오버.\n본가 점수식(연쇄·색·덩어리 보너스), 퀵턴·벽 밀기·바닥 밀기,\n싹쓸이는 다음 공격에 얹히고, 방해뿌요 상쇄와 마진 타임을 따른다.':
        '6x12 field plus a hidden 13th row; game over when column 3 is blocked.\nArcade scoring (chain / colour / group bonuses), quick turn, wall and\nfloor kicks. An all clear pays out on your next attack, and garbage\noffsetting and margin time follow the arcade rules.',
    '10×20 필드. SRS 회전과 벽 밀기 표(I 는 따로), 7-bag,\n굳기 지연 0.5초에 이동·회전 15회까지 미루기,\nT-스핀 3코너 판정, 백투백, 콤보, 퍼펙트 클리어,\n가이드라인 점수와 중력 곡선, 뿌요테트 공격표를 따른다.':
        '10x20 field. SRS rotation with the official kick tables (I has its own),\n7-bag, 0.5s lock delay with up to 15 move resets, 3-corner T-Spin\ndetection, back-to-back, combos, perfect clear, guideline scoring and\ngravity curve, and the Puyo Puyo Tetris attack table.',
    'sudoku.com 방식이다. 메모(연필)·힌트·되돌리기가 있고,\n숫자를 넣으면 같은 줄·칸·박스의 그 숫자 메모가 자동으로 지워진다.\n고른 칸의 줄·칸·박스와 같은 숫자를 함께 밝게 보여 준다.\n난이도는 주어진 숫자 개수와 풀이에 필요한 기법으로 가른다.\n난이도를 바꾸면 다음 문제부터 적용된다 (F2 / R 로 새 문제).\n실수 허용 횟수는 지금 풀던 판에도 바로 적용된다.\n풀던 판은 앱을 껐다 켜도 그대로 이어서 푼다.':
        "Follows sudoku.com. Notes, hints and undo are available.\n Placing a number clears that note from its row, column and\nbox. The selected cell's row, column and box are highlighted along with\nevery matching number. Difficulty comes from the number of givens and the\ntechniques a solver needs. A new difficulty applies to the next puzzle\n(F2 / R for a new one). The mistake limit takes effect on the puzzle\nyou are solving right now. An unfinished puzzle is kept, so you pick up\nwhere you left off after restarting the app.",
    # ---- 상단바 · 메뉴 · 안내 ----
    "Esc 숨기기 · Ctrl+Alt+Z 복귀": "Esc to hide · Ctrl+Alt+Z to bring back",
    "새 게임 (F2 / R)": "New game (F2 / R)",
    "설정 (F1)": "Settings (F1)",
    "숨기기 (Esc / Ctrl+Alt+Z)": "Hide (Esc / Ctrl+Alt+Z)",
    "사이드 패널": "Side panel",
    "상단바": "Top bar",
    "전역 키 등록 실패: ": "Could not register global keys: ",
    "  (등록 실패)": "  (not registered)",
    "보이기 / 숨기기": "Show / hide",
    "새 게임 / 재시작": "New game / restart",
    "설정…": "Settings…",
    "종료": "Quit",
    "투명도 %d%%": "Opacity %d%%",
    "셀 %dpx": "Cell %dpx",
    "배경 지우기 ON": "Background erased",
    "배경 지우기 OFF": "Background back",
    "항상 위 ": "Always on top ",
    "%s 로 재개": "%s to resume",
    "재개": "Resumed",
    "일시정지": "Paused",
    "%s 시작": "%s started",
    "새 게임": "New game",
    "새 게임\t%s / %s": "New game\t%s / %s",
    "배경 지우기\t%s": "Erase background\t%s",
    "항상 위\t%s": "Always on top\t%s",
    "사이드 패널\t%s": "Side panel\t%s",
    "상단바\t%s": "Top bar\t%s",
    "설정…\t%s": "Settings…\t%s",
    "숨기기\t%s / %s": "Hide\t%s / %s",
    "종료\t%s": "Quit\t%s",
    " 로 재개": " to resume",
    "%s / %s 다시 시작": "%s / %s to restart",
    "%s점": "%s pts",
    "색 수는 다음 게임부터 적용됩니다": "Colour count applies from the next game",
    "단축키를 기본값으로 되돌렸습니다 — 설정을 다시 열면 반영됩니다":
        "Keys reset to defaults — reopen settings to see them",
    "표시 언어를 바꿨습니다": "Language changed",
    # ---- 게임 이름 ----
    "뿌요뿌요": "Puyo Puyo",
    "테트리스": "Tetris",
    "스도쿠": "Sudoku",
    # ---- 뿌요 ----
    "%d 연쇄!": "%d chain!",
    "싹쓸이 보너스! +%d, 방해뿌요 +%d": "All clear bonus! +%d, +%d garbage",
    "전체 지우기! 다음 공격에 보너스": "All clear! Bonus on your next attack",
    "방해뿌요 %d개": "%d garbage puyo",
    "뿌요 색 수": "Puyo colours",
    "%d색": "%d colours",
    "엔드리스 (혼자 연쇄 연습)": "Endless (chain practice)",
    "방해뿌요 (일정 간격으로 방해뿌요가 쏟아진다)":
        "Garbage (garbage puyo rain in at intervals)",
    "마진 타임 (시간이 지나면 상쇄가 어려워진다)":
        "Margin time (offsetting gets harder as time passes)",
    "방해뿌요": "Garbage",
    "엔드리스": "Endless",
    "예고 %d": "Incoming %d",
    "<br><br><span style='color:#ffe066'>싹쓸이 대기<br>+%d</span>":
        "<br><br><span style='color:#ffe066'>All clear ready<br>+%d</span>",
    "%s점 · %d연쇄": "%s pts · %d chain",
    "<b>%s</b><br>연쇄 <b>%d</b><br>Lv <b>%d</b>%s":
        "<b>%s</b><br>Chain <b>%d</b><br>Lv <b>%d</b>%s",
    "점수<br><b>%s</b><br><br>연쇄 <b>%d</b><br>최고연쇄 <b>%d</b><br>레벨 <b>%d</b>"
    "<br><br>최고점수<br><b>%s</b><br><br>%s<br>전송 <b>%d</b><br>예고 <b>%d</b>%s":
        "Score<br><b>%s</b><br><br>Chain <b>%d</b><br>Best chain <b>%d</b>"
        "<br>Level <b>%d</b><br><br>Best score<br><b>%s</b><br><br>%s"
        "<br>Sent <b>%d</b><br>Incoming <b>%d</b>%s",
    # ---- 테트리스 ----
    "홀드": "Hold",
    "T-스핀 미니": "T-Spin Mini",
    "T-스핀": "T-Spin",
    "T-스핀 미니 ": "T-Spin Mini ",
    "T-스핀 ": "T-Spin ",
    "싱글": "Single",
    "더블": "Double",
    "트리플": "Triple",
    " %d콤보": " %d combo",
    "퍼펙트 클리어": "Perfect Clear",
    "방해줄 %d줄": "%d garbage lines",
    "시작 레벨": "Start level",
    "엔드리스 (혼자 쌓기 연습)": "Endless (stacking practice)",
    "방해줄 (일정 간격으로 방해줄이 올라온다)":
        "Garbage (garbage lines rise at intervals)",
    "홀드 사용 (조각마다 한 번)": "Hold enabled (once per piece)",
    "방해줄": "Garbage",
    "%s점 · %d줄": "%s pts · %d lines",
    "<b>%s</b><br>줄 <b>%d</b><br>Lv <b>%d</b>%s":
        "<b>%s</b><br>Lines <b>%d</b><br>Lv <b>%d</b>%s",
    "<br>B2B <b>ON</b>": "<br>B2B <b>ON</b>",
    "<br>콤보 <b>%d</b>": "<br>Combo <b>%d</b>",
    "점수<br><b>%s</b><br><br>줄 <b>%d</b><br>레벨 <b>%d</b><br>테트리스 <b>%d</b>"
    "<br>T-스핀 <b>%d</b>%s%s<br><br>최고점수<br><b>%s</b><br><br>%s"
    "<br>전송 <b>%d</b><br>예고 <b>%d</b>%s":
        "Score<br><b>%s</b><br><br>Lines <b>%d</b><br>Level <b>%d</b>"
        "<br>Tetrises <b>%d</b><br>T-Spins <b>%d</b>%s%s<br><br>Best score"
        "<br><b>%s</b><br><br>%s<br>Sent <b>%d</b><br>Incoming <b>%d</b>%s",
    # ---- 스도쿠 ----
    "완성!": "Solved!",
    "%s · %s점": "%s · %s pts",
    "실수 %d번": "%d mistakes",
    "실수 1번": "1 mistake",
    "%s / %s 새 문제": "%s / %s for a new puzzle",
    "메모": "Notes",
    "지우기": "Erase",
    "힌트": "Hint",
    "되돌리기": "Undo",
    "힌트 %d": "Hint %d",
    "메모 켜짐": "Notes on",
    "메모 꺼짐": "Notes off",
    "메모 ": "Notes ",
    "켜짐": "on",
    "꺼짐": "off",
    "처음부터 있던 숫자입니다": "That number was given",
    "실수 %d / %d": "Mistake %d / %d",
    "실수 %s": "Mistake %s",
    "걸린 시간 %s": "Solved in %s",
    "최고 기록!": "Best time!",
    "최고 %d:%02d": "Best %d:%02d",
    "%s점": "%s pts",
    "실수 허용": "Mistake limit",
    "제한 없음": "No limit",
    "%d번": "%d",
    "이어서 풀기": "Resuming",
    "일시정지 / 재개 (P)": "Pause / resume (P)",
    "일시정지 / 재개": "Pause / resume",
    "일시정지 (P)": "Pause (P)",
    "재개 (P)": "Resume (P)",
    "%s 또는 클릭으로 재개": "%s or click to resume",
    # ---- 지뢰찾기 ----
    "지뢰찾기": "Minesweeper",
    "초급": "Beginner",
    "중급": "Intermediate",
    "고급": "Expert",
    "칸 열기": "Reveal cell",
    "깃발 꽂기": "Place flag",
    "주변 한꺼번에": "Clear around",
    "물음표 표시 쓰기": "Use question marks",
    "%s (%dx%d, 지뢰 %d)": "%s (%dx%d, %d mines)",
    "지뢰!": "Mine!",
    "클리어!": "Cleared!",
    "지뢰를 밟았다": "You hit a mine",
    "%s / %s 새 게임": "%s / %s for a new game",
    "걸린 시간 %s초": "Cleared in %ss",
    "최고 %d초": "Best %ds",
    "%d초": "%ds",
    "%s초 · 남은 지뢰 %d": "%ss · %d mines left",
    "시간<br><b>%s초</b>": "Time<br><b>%ss</b>",
    "걸린 시간<br><b>%s초</b>": "Cleared in<br><b>%ss</b>",
    "<b>%s초</b><br>%s<br><br>지뢰 <b>%d</b><br>깃발 <b>%d</b>"
    "<br>최고 <b>%s</b>":
        "<b>%ss</b><br>%s<br><br>Mines <b>%d</b><br>Flags <b>%d</b>"
        "<br>Best <b>%s</b>",
    "%s<br><br>난이도<br><b>%s</b><br><br>남은 지뢰 <b>%d</b>"
    "<br>꽂은 깃발 <b>%d</b><br>연 칸 <b>%d</b>/%d<br><br>최고 기록<br><b>%s</b>":
        "%s<br><br>Difficulty<br><b>%s</b><br><br>Mines left <b>%d</b>"
        "<br>Flags <b>%d</b><br>Opened <b>%d</b>/%d<br><br>Best time<br><b>%s</b>",
    "원작 규칙 그대로다. 왼쪽 클릭으로 열고 오른쪽 클릭으로 깃발을 꽂는다.\n"
    "숫자 칸에서 양쪽 버튼(또는 가운데 버튼)을 누르면 주변이 한꺼번에 열린다.\n"
    "꽂아 둔 깃발 수가 그 숫자와 같을 때만 열리고, 깃발이 틀렸으면 터진다.\n"
    "첫 칸은 절대 지뢰가 아니다.\n"
    "난이도를 바꾸면 다음 게임부터 적용된다 (F2 / R 로 새 게임).":
        "The original rules. Left click reveals, right click places a flag.\n"
        "On a numbered cell, press both buttons (or the middle button) to clear\n"
        "around it. That only opens when the flags you placed match the number,\n"
        "and if a flag is wrong it blows up.\n"
        "The first cell is never a mine.\n"
        "A new difficulty applies to the next game (F2 / R for a new one).",
    "격자선 보이기": "Show grid",
    "배경이 옅을 때 또렷하게": "Sharpen on see-through background",
    "끄면 보조선과 받침 없이 예전처럼 그린다.":
        "Turn off to draw as before, with no helper lines or text plates.",
    "격자선 진하기": "Grid strength",
    "격자선 ON": "Grid ON",
    "격자선 OFF": "Grid OFF",
    "격자선 토글": "Toggle grid",
    "격자선	%s": "Grid	%s",
    "지움": "Ers",
    "힌%d": "H%d",
    "되돌": "Und",
    "✎ 메모 · %s": "✎ Notes · %s",
    "힌트 %d번째": "Hint #%d",
    "%s (%d~%d칸)": "%s (%d–%d givens)",
    "%s · 실수 %s": "%s · %s mistakes",
    "<b>%s</b><br>%s<br>남은 <b>%d</b><br>실수 <b>%s</b><br>힌트 <b>%d</b>"
    "<br>최고 <b>%s</b>":
        "<b>%s</b><br>%s<br>Left <b>%d</b><br>Mistakes <b>%s</b>"
        "<br>Hints <b>%d</b><br>Best <b>%s</b>",
    "%s<br><br>난이도<br><b>%s</b><br>필요 기법<br><b>%s</b>"
    "<br><br>남은 칸 <b>%d</b><br>실수 <b>%s</b><br>힌트 <b>%d</b>"
    "<br><br>최고 기록<br><b>%s</b>":
        "%s<br><br>Difficulty<br><b>%s</b><br>Needs<br><b>%s</b>"
        "<br><br>Empty <b>%d</b><br>Mistakes <b>%s</b><br>Hints <b>%d</b>"
        "<br><br>Best time<br><b>%s</b>",
    "시간<br><b>%s</b>": "Time<br><b>%s</b>",
    "걸린 시간<br><b>%s</b>": "Solved in<br><b>%s</b>",
    # 난이도 이름
    "쉬움": "Easy",
    "보통": "Medium",
    "어려움": "Hard",
    "전문가": "Expert",
    "마스터": "Master",
    "익스트림": "Extreme",
    # 풀이 기법 이름
    "단일값": "Singles",
    "후보 가두기": "Locked candidates",
    "쌍·삼중": "Pairs / triples",
    "X-Wing·Swordfish": "X-Wing / Swordfish",
    "XY-Wing·색칠·유니크 렉탱글": "XY-Wing / Colouring / Unique Rectangle",
    "포싱 체인": "Forcing chains",
    "깊은 포싱 체인": "Deep forcing chains",
    "논리로 못 품": "Not solvable by logic",
    # 동작 이름 (설정 → 단축키)
    "왼쪽 이동": "Move left",
    "오른쪽 이동": "Move right",
    "빠른 낙하": "Soft drop",
    "시계 방향 회전": "Rotate clockwise",
    "시계 방향 (보조)": "Rotate clockwise (alt)",
    "반시계 방향 회전": "Rotate counter-clockwise",
    "즉시 낙하": "Hard drop",
    "180도 회전": "Rotate 180°",
    "커서 왼쪽": "Cursor left",
    "커서 오른쪽": "Cursor right",
    "커서 위": "Cursor up",
    "커서 아래": "Cursor down",
    "메모 모드": "Notes mode",
    "지우기 (보조)": "Erase (alt)",
    "빠른 재시작": "Quick restart",
    "즉시 숨기기": "Hide now",
    "설정 열기": "Open settings",
    "메뉴 열기": "Open menu",
    "배경 지우기 토글": "Toggle erased background",
    "더 진하게": "More opaque",
    "더 투명하게": "More transparent",
    "화면 크게": "Bigger",
    "화면 작게": "Smaller",
    "항상 위 토글": "Toggle always on top",
    "사이드 패널 토글": "Toggle side panel",
    "상단바 토글": "Toggle top bar",
    "숨기기 / 복귀": "Hide / bring back",
    "일시정지 토글": "Toggle pause",
}
for _n in range(1, 10):
    TR["숫자 %d 넣기" % _n] = "Enter %d" % _n
del _n


def tr(text):
    """화면에 보일 글을 지금 언어로. 번역이 없으면 원문 그대로 내보낸다."""
    if LANG == "ko" or not text:
        return text
    got = TR.get(text)
    if got is None:
        _TR_MISS.add(text)
        return text
    return got


def set_language(code):
    global LANG
    LANG = "en" if code == "en" else "ko"
    return LANG


# 게임이 무엇이든 그대로인 설정. 화면·은폐·창 위치가 여기 들어간다.
COMMON_DEFAULTS = {
    # ---- 화면 ----
    "cell": 30,                   # 셀 한 변 픽셀 — 창 크기가 여기서 결정된다
    "bg_color": "#101418",
    "bg_alpha": 78,               # 0~255. 0 이면 '배경 지우기'
    "visibility_aid": True,       # 배경이 옅을 때 보조선·받침을 넣을지
    "show_grid": True,            # 격자선 켜고 끄기
    "grid_alpha": 20,             # 격자선 진하기 (켜져 있을 때)
    "opacity": 0.94,              # 창 전체 투명도
    "always_on_top": True,
    "frameless": True,
    "show_topbar": True,
    "show_panel": True,
    "show_ghost": True,           # 착지 위치 표시
    "btn_pause": True,
    "btn_hide": True,
    "btn_settings": True,
    "btn_restart": True,
    "window_mode": "hidden",      # hidden | disguise | normal
    "disguise_title": "메모장",
    # ---- 은폐 ----
    "pause_on_hide": True,        # 숨기면 자동 일시정지
    "hide_on_blur": False,        # 포커스를 잃으면 자동으로 숨기기
    # ---- 표시 언어 ----
    "lang": "ko",                 # ko | en
    # ---- 공통 게임 ----
    "speed": 1.0,                 # 낙하 속도 배율
    # ---- 위치 ----
    "pos": [320, 240],
}

# 옛 설정 파일(모든 값이 한 덩어리였다)에서 게임 쪽으로 옮겨야 하는 키
LEGACY_GAME_KEYS = ("num_colors", "mode", "margin_time")


# ================================================================= 게임 등록
class GameSpec:
    """게임 하나를 이 창에 끼우는 데 필요한 것 전부.

    창·은폐 계층은 이 명세만 보고 움직인다. 새 게임을 붙일 때 PuyoWindow 나
    Config 를 고칠 일이 없도록, 게임마다 달라지는 것을 여기 한곳에 모았다.

        key           설정 파일에 쓰이는 이름
        label         사람에게 보여 줄 이름
        defaults      이 게임 전용 설정 (공용 설정과 이름이 겹치면 안 된다)
        engine(opt)   규칙 객체. reset / update(dt) / move / rotate /
                      soft_drop / hard_drop 과 score·over·state 를 제공한다
        board(win)    필드 위젯. resync() 로 셀 크기를 다시 반영한다
        side(win)     NEXT 같은 옆 위젯. 없으면 None
        stats(win, game, compact) -> 사이드 패널에 넣을 글
        settings_tab(dlg) -> 설정 창의 '게임' 탭 위젯
        actions       이 게임이 실제로 쓰는 조작 동작 id 집합
        records(game) -> {기록 이름: 값}. 큰 값으로만 갱신한다
        wants_mouse   필드를 마우스로 눌러야 하는 게임인가. 켜면 필드와 옆
                      위젯이 클릭을 받는다(그만큼 그 자리에서는 창을 끌 수
                      없다 — 상단바·점수 칸·창 가장자리로는 그대로 끌 수 있다)
    """

    def __init__(self, key, label, defaults, engine, board, stats,
                 settings_tab, actions, records, side=None,
                 wants_mouse=False, dump=None, restore=None):
        self.key = key
        self.label = label
        self.defaults = dict(defaults)
        self.engine = engine
        self.board = board
        self.side = side
        self.stats = stats
        self.settings_tab = settings_tab
        self.actions = frozenset(actions)
        self.records = records
        self.wants_mouse = bool(wants_mouse)
        # 이어하기. 둘 다 있어야 켜진다.
        #   dump(game)          -> JSON 으로 담을 수 있는 dict, 또는 None
        #   restore(game, data) -> 되살렸으면 True
        # 판이 쌓이는 게임(뿌요·테트리스)은 잠깐 끊겼다 이어 봐야 의미가 없어
        # 붙이지 않았다. 오래 붙들고 푸는 스도쿠만 쓴다.
        self.dump = dump
        self.restore = restore

    @property
    def resumable(self):
        return bool(self.dump and self.restore)


GAMES = {}                        # key -> GameSpec (등록 순서 유지)


def register_game(spec):
    clash = set(spec.defaults) & set(COMMON_DEFAULTS)
    if clash:
        raise ValueError("게임 설정 이름이 공용 설정과 겹칩니다: %s" % sorted(clash))
    unknown = spec.actions - {aid for aid, _l, _k in LOCAL_ACTIONS + GLOBAL_ACTIONS}
    if unknown:
        raise ValueError("동작 목록에 없는 id: %s" % sorted(unknown))
    GAMES[spec.key] = spec
    return spec


def default_game():
    return next(iter(GAMES)) if GAMES else "puyo"


# ================================================================= 설정 저장
class Config:
    """설정을 공용과 게임별로 나눠 담는다.

        settings  화면·은폐·창 위치 — 게임이 바뀌어도 그대로
        games     {게임 key: 그 게임 전용 설정}
        records   {게임 key: {기록 이름: 값}}
        game      지금 고른 게임
        keys      단축키 (게임과 무관하게 하나로 관리한다)
    """

    def __init__(self):
        base = os.environ.get("APPDATA") or os.path.expanduser("~")
        self.dir = os.path.join(base, APP_NAME)
        self.path = os.path.join(self.dir, "config.json")
        self.data = {
            "settings": dict(COMMON_DEFAULTS),
            "games": {k: dict(sp.defaults) for k, sp in GAMES.items()},
            "records": {k: {} for k in GAMES},
            "game": default_game(),
            "keys": dict(DEFAULT_KEYS),
            "saves": {},
        }
        self.load()

    def load(self):
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                raw = json.load(f)
        except Exception:
            return
        if not isinstance(raw, dict):
            return
        old = raw.get("settings") or {}

        settings = dict(COMMON_DEFAULTS)
        for k, v in old.items():
            if k in COMMON_DEFAULTS:
                settings[k] = v
        self.data["settings"] = settings

        games = {k: dict(sp.defaults) for k, sp in GAMES.items()}
        for key, saved in (raw.get("games") or {}).items():
            if key in games and isinstance(saved, dict):
                for k, v in saved.items():
                    if k in games[key]:
                        games[key][k] = v
        # 옛 설정 파일은 게임 설정이 공용과 한 덩어리였다 — 뿌요 쪽으로 옮긴다
        if "games" not in raw and "puyo" in games:
            for k in LEGACY_GAME_KEYS:
                if k in old and k in games["puyo"]:
                    games["puyo"][k] = old[k]
        self.data["games"] = games

        records = {k: {} for k in GAMES}
        for key, saved in (raw.get("records") or {}).items():
            if key in records and isinstance(saved, dict):
                records[key] = {k: v for k, v in saved.items()}
        if "records" not in raw and "puyo" in records:
            for old_key, new_key in (("best", "best"), ("best_chain", "best_chain")):
                if raw.get(old_key):
                    records["puyo"][new_key] = int(raw[old_key])
        self.data["records"] = records

        saves = {}
        for key, saved in (raw.get("saves") or {}).items():
            if key in GAMES and isinstance(saved, dict):
                saves[key] = saved
        self.data["saves"] = saves

        picked = raw.get("game")
        self.data["game"] = picked if picked in GAMES else default_game()

        # 저장된 키 중 지금도 존재하는 동작만 받아들인다 (버전이 올라가도 안전)
        keys = dict(DEFAULT_KEYS)
        for aid, seq in (raw.get("keys") or {}).items():
            if aid in DEFAULT_KEYS and isinstance(seq, str):
                keys[aid] = seq
        self.data["keys"] = keys

    def save(self):
        """임시 파일에 쓰고 교체 — 도중에 죽어도 기존 설정이 깨지지 않는다."""
        try:
            os.makedirs(self.dir, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=self.dir, suffix=".tmp")
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(self.data, f, ensure_ascii=False, indent=1)
            os.replace(tmp, self.path)
        except Exception:
            pass

    @property
    def s(self):
        """공용 설정 (화면·은폐·창)."""
        return self.data["settings"]

    @property
    def keys(self):
        return self.data["keys"]

    @property
    def game(self):
        return self.data["game"]

    @property
    def spec(self):
        return GAMES[self.data["game"]]

    @property
    def g(self):
        """지금 고른 게임의 전용 설정."""
        return self.data["games"][self.data["game"]]

    @property
    def rec(self):
        """지금 고른 게임의 기록."""
        return self.data["records"].setdefault(self.data["game"], {})

    def opt(self, key):
        """규칙 객체가 쓰는 설정 읽기 — 게임 전용을 먼저 보고 없으면 공용."""
        g = self.g
        return g[key] if key in g else self.s[key]

    def set_opt(self, key, value):
        """opt() 와 같은 자리에 쓴다."""
        g = self.g
        if key in g:
            g[key] = value
        else:
            self.s[key] = value

    def set_game(self, key):
        if key in GAMES:
            self.data["game"] = key

    # ------------------------------------------------------------ 이어하기
    def take_save(self, key):
        """저장해 둔 판을 꺼내면서 지운다.

        꺼낼 때 지우는 이유 — 되살리다 실패해도 깨진 기록이 남아 켤 때마다
        같은 곳에서 넘어지지 않는다.
        """
        return self.data["saves"].pop(key, None)

    def put_save(self, key, data):
        if data:
            self.data["saves"][key] = data
        else:
            self.data["saves"].pop(key, None)


# ========================================================= 전역 핫키 수신기
class HotkeyFilter(QAbstractNativeEventFilter):
    """스레드 메시지 큐로 들어오는 WM_HOTKEY 를 잡아 콜백으로 넘긴다."""

    def __init__(self, callback):
        super().__init__()
        self.callback = callback

    def nativeEventFilter(self, event_type, message):
        try:
            et = bytes(event_type)
        except Exception:
            et = event_type
        if et == b"windows_generic_MSG":
            msg = ctypes.wintypes.MSG.from_address(int(message))
            if msg.message == WM_HOTKEY:
                self.callback(int(msg.wParam))
        return False, 0


class HotkeyManager:
    """전역 핫키 등록을 한곳에서 관리한다 — 설정에서 바꿀 때마다 다시 등록한다."""

    def __init__(self):
        self.by_id = {}          # Win32 핫키 id -> 동작 id
        self._next_id = 1

    def clear(self):
        user32 = ctypes.windll.user32
        for hid in self.by_id:
            user32.UnregisterHotKey(None, hid)
        self.by_id = {}

    def apply(self, bindings):
        """bindings = {동작 id: 'Ctrl+Alt+Z'}. 등록 실패한 동작 id 목록을 돌려준다."""
        self.clear()
        user32 = ctypes.windll.user32
        failed = []
        for action_id, _label, _default in GLOBAL_ACTIONS:
            seq = (bindings.get(action_id) or "").strip()
            if not seq:
                continue         # 빈 값 = 사용 안 함
            parsed = seq_to_win(seq)
            if parsed is None:
                failed.append(action_id)
                continue
            mods, vk = parsed
            hid = self._next_id
            self._next_id += 1
            if user32.RegisterHotKey(None, hid, mods | MOD_NOREPEAT, vk):
                self.by_id[hid] = action_id
            else:
                failed.append(action_id)
        return failed

    def action_for(self, hotkey_id):
        return self.by_id.get(hotkey_id)


def mix(c1, c2, t):
    """두 색을 t 비율로 섞는다 (t=0 이면 c1, t=1 이면 c2)."""
    return QColor(int(c1.red() * (1 - t) + c2.red() * t),
                  int(c1.green() * (1 - t) + c2.green() * t),
                  int(c1.blue() * (1 - t) + c2.blue() * t))


# ------------------------------------------------- 배경이 옅을 때의 가시성
# 배경 알파를 낮추면 뒤 창이 그대로 비친다. 흰 문서 위에 올리면 흰 격자선과
# 흰 테두리가 통째로 사라져서, 판이 어디서 시작하고 끝나는지조차 안 보인다.
# 색을 바꿔서는 못 고친다 — 어두운 바탕에서 잘 보이는 색과 밝은 바탕에서 잘
# 보이는 색이 서로 반대다. 그래서 밝은 선 뒤에 어두운 선을 한 겹 깔아, 어느
# 바탕에서도 둘 중 하나는 남게 한다.
FAINT_BG = 140                # 이 아래면 뒤 창이 비친다고 본다


def faint_bg(s):
    return int(s["bg_alpha"]) < FAINT_BG


def faint_strength(s):
    """0.0(안 옅음) ~ 1.0(완전 투명). 옅을수록 보조선을 세게 준다.

    아래의 모든 보조 — 격자 보조선, 판 테두리, 글자 받침 — 가 이 값 하나를
    거친다. 그래서 여기서 0 을 돌려주면 보조를 넣기 전과 똑같이 그려진다.
    가시성이 부담스러운 사람은 설정에서 통째로 끌 수 있다.
    """
    if not s.get("visibility_aid", True):
        return 0.0
    a = int(s["bg_alpha"])
    if a >= FAINT_BG:
        return 0.0
    return (FAINT_BG - a) / float(FAINT_BG)


def draw_lines(p, lines, color, strength, width=1.0, style=Qt.SolidLine):
    """격자선 — 옅을 때는 어두운 선을 먼저 깔고 그 위에 밝은 선을 긋는다."""
    if strength > 0:
        p.setPen(QPen(QColor(0, 0, 0, int(40 + 110 * strength)), width, style))
        for x1, y1, x2, y2 in lines:
            p.drawLine(int(x1 + 1), int(y1 + 1), int(x2 + 1), int(y2 + 1))
    p.setPen(QPen(color, width, style))
    for x1, y1, x2, y2 in lines:
        p.drawLine(int(x1), int(y1), int(x2), int(y2))


def draw_board_edge(p, width, height, radius, strength):
    """판 테두리. 배경이 옅을수록 또렷하게 — 판의 경계는 반드시 보여야 한다."""
    if strength <= 0:
        return
    rect = QRectF(1.5, 1.5, width - 3.0, height - 3.0)
    p.setBrush(Qt.NoBrush)
    p.setPen(QPen(QColor(0, 0, 0, int(70 + 130 * strength)),
                  2.0 + 1.5 * strength))
    p.drawRoundedRect(rect, radius, radius)
    p.setPen(QPen(QColor(255, 255, 255, int(70 + 140 * strength)), 1.3))
    p.drawRoundedRect(rect, radius, radius)


def text_plate_css(bg_color, strength):
    """옆 칸·상단바 글 뒤에 깔 어두운 받침.

    글자에 그림자만 둘러서는 안 된다. 거의 흰 글자를 흰 문서 위에 올리면
    테두리를 아무리 진하게 줘도 획 안쪽이 바탕과 같은 색이라 읽히지 않는다.
    뒤에 받침을 깔면 바탕이 무엇이든 글자가 놓일 자리가 생긴다. 배경이
    옅을수록만 진해지므로, 배경을 살려 둔 사람에게는 아무것도 달라지지 않는다.
    """
    if strength <= 0:
        return ""
    c = QColor(bg_color)
    return ("background-color: rgba(%d,%d,%d,%d);"
            " border-radius: 4px; padding: 2px 4px;"
            % (c.red(), c.green(), c.blue(), int(90 + 90 * strength)))


def grid_alpha_of(s):
    """지금 써야 할 격자선 진하기. 꺼져 있으면 0."""
    return int(s["grid_alpha"]) if s.get("show_grid", True) else 0


def grid_color(grid_alpha, strength):
    """옅을수록 격자선 자체도 조금 진하게."""
    return QColor(255, 255, 255,
                  min(255, int(grid_alpha + 45 * strength)))


def menu_stylesheet(bg_color):
    """배경색이 어떻든 글자가 반드시 보이도록 메뉴 색을 계산한다."""
    bg = QColor(bg_color)
    ink = QColor("#ffffff") if bg.lightness() < 128 else QColor("#141414")
    panel = mix(bg, ink, 0.10)
    border = mix(bg, ink, 0.30)
    hover = mix(bg, ink, 0.28)
    return (
        "QMenu { background-color: %s; color: %s; border: 1px solid %s; padding: 4px; }"
        "QMenu::item { background-color: transparent; color: %s;"
        " padding: 5px 22px 5px 18px; }"
        "QMenu::item:selected { background-color: %s; }"
        "QMenu::separator { height: 1px; background: %s; margin: 4px 6px; }"
        % (panel.name(), ink.name(), border.name(), ink.name(),
           hover.name(), border.name()))


def tool_icon(kind, color, active=True):
    """상단바 아이콘을 코드로 그린다 — 외부 이미지 파일이 필요 없다."""
    pix = QPixmap(20, 18)
    pix.fill(Qt.transparent)
    p = QPainter(pix)
    p.setRenderHint(QPainter.Antialiasing)
    ink = QColor(color)
    p.setPen(QPen(ink, 1.6))

    if kind == "hide":                      # 아래 화살표 + 밑줄 = 내려서 감추기
        p.drawLine(10, 4, 10, 11)
        p.drawLine(10, 11, 6, 7)
        p.drawLine(10, 11, 14, 7)
        p.drawLine(5, 14, 15, 14)
    elif kind == "settings":                # 톱니 느낌의 육각 + 가운데 점
        path = QPainterPath()
        for i in range(6):
            ang = i * 3.14159 / 3
            x = 10 + 6.0 * (1 if i % 2 else 0.86) * math.cos(ang)
            y = 9 + 6.0 * (1 if i % 2 else 0.86) * math.sin(ang)
            if i == 0:
                path.moveTo(x, y)
            else:
                path.lineTo(x, y)
        path.closeSubpath()
        p.drawPath(path)
        p.setBrush(ink)
        p.drawEllipse(QPointF(10, 9), 1.8, 1.8)
    elif kind == "pause":                   # 세로 막대 둘 = 멈추기
        p.setBrush(ink)
        p.setPen(Qt.NoPen)
        p.drawRect(QRectF(6.4, 3.5, 2.6, 11))
        p.drawRect(QRectF(11.2, 3.5, 2.6, 11))
    elif kind == "play":                    # 삼각형 = 재개
        p.setBrush(ink)
        p.setPen(Qt.NoPen)
        p.drawPolygon(*[QPointF(6.8, 3.2), QPointF(6.8, 14.8),
                        QPointF(15.2, 9.0)])
    elif kind == "restart":                 # 원형 화살표 = 다시 시작
        p.drawArc(QRectF(4.5, 3.5, 11, 11), 40 * 16, 280 * 16)
        p.setBrush(ink)
        p.drawPolygon(*[QPointF(13.6, 2.6), QPointF(15.4, 7.4),
                        QPointF(10.6, 6.2)])
    p.end()
    return QIcon(pix)


# =============================================================== 게임 규칙
COLS, ROWS = 6, 13            # ROWS 중 index 0 은 '13번째 줄' = 숨은 줄
VISIBLE_ROWS = 12
HIDDEN_ROW = 0
SPAWN_COL = 2                 # 3열 — 여기가 막히면 게임 오버 (본가와 동일)
GARBAGE = -1                  # 방해뿌요

# 뿌요 색 — 빨강 / 파랑 / 노랑 / 초록. 설정에서 3~4색을 고른다.
# 본가 뿌요테트의 기본 대전 색 수도 4색이다.
PUYO_COLORS = ["#ff4f5e", "#4aa8ff", "#ffd23f", "#4ddc79"]
GARBAGE_COLOR = "#9aa3b2"

SEQUENCE_LEN = 256            # 본가처럼 이 길이의 순서표를 만들어 되풀이한다

# 회전 방향 — 0:위 1:오른쪽 2:아래 3:왼쪽 (자뿌요가 축뿌요 기준 어디에 있는지)
ROT_DIRS = [(0, -1), (1, 0), (0, 1), (-1, 0)]

# 본가 연쇄 보너스 표. 19연쇄 이후는 32씩 증가.
CHAIN_POWER = [0, 8, 16, 32, 64, 96, 128, 160, 192, 224,
               256, 288, 320, 352, 384, 416, 448, 480, 512]
COLOR_BONUS = {1: 0, 2: 3, 3: 6, 4: 12, 5: 24}
GROUP_BONUS = {4: 0, 5: 2, 6: 3, 7: 4, 8: 5, 9: 6, 10: 7}
ALL_CLEAR_BONUS = 2100        # 전체 지우기 점수 — 다음 공격에 붙는다
ALL_CLEAR_GARBAGE = 30        # 전체 지우기로 다음 공격에 얹히는 방해뿌요
TARGET_POINT = 70.0           # 방해뿌요 1개를 보내는 데 필요한 점수
# 마진 타임 배율 — 96초 이후 16초마다 한 칸씩 내려간다 (본가와 같은 방식)
MARGIN_TABLE = [1.0, 0.75, 0.5, 0.4, 0.3, 0.25, 0.2, 0.15, 0.1, 0.05,
                0.03, 0.02, 0.01]
MARGIN_START = 96.0
MARGIN_STEP = 16.0

LOCK_DELAY = 420.0            # 바닥에 닿은 뒤 굳기까지 (회전/이동으로 초기화)
SETTLE_MS = 80.0              # 낙하가 끝난 뒤 터짐을 판정하기까지의 짧은 멈춤
POP_MS = 400.0                # 뿌요가 터지는 연출 시간
GARBAGE_MS = 180.0            # 방해뿌요 예고에서 낙하까지의 뜸

# 중력 — 떠 있는 뿌요가 실제로 떨어지는 속도 (셀/ms). 가속이 붙는다.
FALL_V0 = 0.006
FALL_ACC = 0.00013
FALL_VMAX = 0.048


def chain_power(n):
    if n <= len(CHAIN_POWER):
        return CHAIN_POWER[n - 1]
    return 512 + (n - len(CHAIN_POWER)) * 32


def group_bonus(size):
    return GROUP_BONUS.get(size, 10)      # 11개 이상은 10


class PuyoGame:
    """화면과 무관한 순수 게임 상태. 60fps 로 update(dt) 를 불러 준다.

    상태 흐름
        play    조작 가능 — 낙하, 이동, 회전
        drop    공중에 뜬 뿌요가 아래로 떨어지는 중 (중력)
        settle  낙하가 멈춘 뒤 터질 덩어리를 판정하기까지의 짧은 대기
        pop     4개 이상 붙은 뿌요가 터지는 연출
        garbage 방해뿌요가 떨어질 차례를 기다리는 뜸
        over    게임 오버

    굳힌 직후 · 터진 직후 · 방해뿌요가 쏟아진 직후에는 반드시 drop 을 지나며,
    낙하가 완전히 끝난 뒤에만 다음 터짐을 판정한다. 그래서 연쇄가 한 단계씩
    눈에 보인다.
    """

    def __init__(self, opt):
        self.opt = opt                     # opt(key) -> 설정값
        self.reset()

    # ------------------------------------------------------------- 초기화
    def reset(self):
        self.grid = [[None] * COLS for _ in range(ROWS)]
        self.queue = []
        self.cur = None
        self.state = "play"
        self.timer = 0.0
        self.fall = 0.0                    # 현재 셀 안에서의 낙하 진행도 0~1
        self.ground_ms = 0.0
        self.score = 0
        self.chain = 0
        self.max_chain = 0
        self.pieces = 0
        self.elapsed = 0.0
        self.pending = 0                   # 떨어질 차례를 기다리는 방해뿌요
        self.sent = 0                      # 보낸 방해뿌요 누적
        self.leftover = 0.0                # 상쇄 계산에서 남은 점수
        self.chain_score = 0               # 이번 연쇄로 번 점수 (상쇄 계산용)
        self.pop_cells = []                # 지금 터지는 중인 좌표
        # 낙하 애니메이션 — {(x, 도착 행): 남은 거리(셀)}. 그림은 이만큼 위에 그린다.
        self.fall_offsets = {}
        self.fall_vel = 0.0
        self.pop_gain = 0                  # 방금 연쇄로 얻은 점수 (표시용)
        self.zenkeshi = False              # 다음 공격에 얹을 전체 지우기 보너스
        self.msg = ""
        self.msg_t = 0.0
        self.rain_t = 0.0
        self.over = False
        self._seq_i = 0
        self._pick_colors()
        self._fill_queue()
        self.spawn()

    # --------------------------------------------------------------- 조각
    def _pick_colors(self):
        """이 판에서 쓸 색과 조 순서표를 만든다 — 판이 시작될 때 한 번만.

        본가 뿌요테트와 같은 방식이다.
          · 팔레트에서 설정한 개수만큼 색을 골라 한 판 동안 고정한다.
          · 길이 %d 의 순서표를 미리 뽑아 두고 끝까지 쓰면 처음으로 돌아간다.
          · 각 뿌요는 독립 균등 추첨이다. 그래서 같은 색 조(더블)가 1/색수
            확률로 나오고, 어느 색도 더 자주 나오지 않는다.
          · 처음 세 조는 그중 3색만 쓴다. 어떤 색을 뺄지도 무작위로 정한다.

        색 세트를 한 판 동안 고정하는 것이 중요하다. 도중에 바꿔 버리면 판에
        남은 색이 다시는 나오지 않아 영구히 지울 수 없는 뿌요가 생긴다.
        """ % SEQUENCE_LEN
        n = max(3, min(len(PUYO_COLORS), int(self.opt("num_colors"))))
        self.colors = random.sample(range(len(PUYO_COLORS)), n)
        self.opening = (random.sample(self.colors, 3) if n > 3
                        else list(self.colors))
        self.sequence = [
            (random.choice(self.opening if i < 3 else self.colors),
             random.choice(self.opening if i < 3 else self.colors))
            for i in range(SEQUENCE_LEN)]

    def _rand_pair(self):
        a, b = self.sequence[self._seq_i % SEQUENCE_LEN]
        self._seq_i += 1
        return [a, b]

    def _fill_queue(self):
        while len(self.queue) < 3:
            self.queue.append(self._rand_pair())

    def spawn(self):
        if self.grid[HIDDEN_ROW][SPAWN_COL] is not None:
            self.over = True
            self.state = "over"
            self.cur = None
            return
        self._fill_queue()
        a, b = self.queue.pop(0)
        self._fill_queue()
        # 축뿌요는 숨은 줄, 자뿌요는 그 위(화면 밖) — 본가의 등장 위치와 같다.
        self.cur = {"x": SPAWN_COL, "y": HIDDEN_ROW, "rot": 0, "a": a, "b": b}
        self.fall = 0.0
        self.ground_ms = 0.0
        self.pieces += 1
        self.state = "play"

    def cells(self, cur=None):
        """[(x, y, color), ...] — 축뿌요 먼저, 자뿌요 다음."""
        c = cur or self.cur
        if not c:
            return []
        dx, dy = ROT_DIRS[c["rot"]]
        return [(c["x"], c["y"], c["a"]), (c["x"] + dx, c["y"] + dy, c["b"])]

    def free(self, x, y):
        """그 칸에 뿌요가 들어갈 수 있나. y<0 은 화면 위 = 비어 있는 것으로 본다."""
        if x < 0 or x >= COLS or y >= ROWS:
            return False
        if y < 0:
            return True
        return self.grid[y][x] is None

    def fits(self, x, y, rot):
        dx, dy = ROT_DIRS[rot]
        return self.free(x, y) and self.free(x + dx, y + dy)

    def grounded(self):
        c = self.cur
        return bool(c) and not self.fits(c["x"], c["y"] + 1, c["rot"])

    # --------------------------------------------------------------- 조작
    def move(self, dx):
        if self.state != "play" or not self.cur:
            return False
        c = self.cur
        if self.fits(c["x"] + dx, c["y"], c["rot"]):
            c["x"] += dx
            self.ground_ms = 0.0            # 굳기 직전에 밀어 넣을 여지를 준다
            return True
        return False

    def rotate(self, dir_):
        """본가식 회전 — 벽 밀기, 바닥 밀기, 막혔을 때의 퀵턴까지 처리한다."""
        if self.state != "play" or not self.cur:
            return False
        c = self.cur
        old_rot, old_x, old_y = c["rot"], c["x"], c["y"]
        new_rot = (old_rot + dir_ + 4) % 4

        if self.fits(c["x"], c["y"], new_rot):
            c["rot"] = new_rot
            self.ground_ms = 0.0
            return True

        dx, dy = ROT_DIRS[new_rot]
        # 좌우로 돌리다 막히면 축뿌요를 반대쪽으로 한 칸 밀어 준다 (벽 밀기)
        if dx and self.fits(c["x"] - dx, c["y"], new_rot):
            c["rot"], c["x"] = new_rot, c["x"] - dx
            self.ground_ms = 0.0
            return True
        # 아래로 돌리다 막히면 축뿌요를 한 칸 띄운다 (바닥 밀기)
        if dy > 0 and self.fits(c["x"], c["y"] - 1, new_rot):
            c["rot"], c["y"] = new_rot, c["y"] - 1
            self.ground_ms = 0.0
            return True
        # 세로로 서 있고 좌우가 모두 막혔으면 180도 뒤집는다 (퀵턴)
        if old_rot in (0, 2):
            if not self.free(c["x"] - 1, c["y"]) and not self.free(c["x"] + 1, c["y"]):
                flip = (old_rot + 2) % 4
                if self.fits(c["x"], c["y"], flip):
                    c["rot"] = flip
                    self.ground_ms = 0.0
                    return True
                if self.fits(c["x"], c["y"] - 1, flip):
                    c["rot"], c["y"] = flip, c["y"] - 1
                    self.ground_ms = 0.0
                    return True

        c["rot"], c["x"], c["y"] = old_rot, old_x, old_y
        return False

    def soft_drop(self):
        """한 칸 내리고, 이미 바닥이면 바로 굳힌다."""
        if self.state != "play" or not self.cur:
            return
        c = self.cur
        if self.fits(c["x"], c["y"] + 1, c["rot"]):
            c["y"] += 1
            self.fall = 0.0
            self.ground_ms = 0.0
        else:
            self.lock()

    def hard_drop(self):
        if self.state != "play" or not self.cur:
            return
        c = self.cur
        while self.fits(c["x"], c["y"] + 1, c["rot"]):
            c["y"] += 1
        self.lock()

    def ghost_y(self):
        """착지 위치 표시용 — 지금 그대로 떨어졌을 때의 축뿌요 y."""
        c = self.cur
        if not c:
            return None
        y = c["y"]
        while self.fits(c["x"], y + 1, c["rot"]):
            y += 1
        return y

    # ------------------------------------------------------------ 굳히기
    def lock(self):
        for x, y, color in self.cells():
            if y < 0:
                continue                   # 화면 위로 삐져나온 뿌요는 버린다
            self.grid[y][x] = color
        self.cur = None
        self.chain = 0
        self.chain_score = 0
        self.pop_gain = 0
        self.apply_gravity()
        self._fall_or_settle()

    def apply_gravity(self):
        """열마다 공중에 뜬 뿌요를 아래로 모은다.

        칸의 값은 바로 최종 위치로 옮기고, 움직인 거리만 fall_offsets 에 적어
        둔다. 그림은 그만큼 위에서 시작해 내려오므로 눈에는 떨어지는 것으로
        보이면서, 규칙 판정은 항상 최종 위치를 기준으로 한다.
        """
        moved = False
        for x in range(COLS):
            write = ROWS - 1
            for y in range(ROWS - 1, -1, -1):
                v = self.grid[y][x]
                if v is None:
                    continue
                if write != y:
                    self.grid[write][x] = v
                    self.grid[y][x] = None
                    self.fall_offsets[(x, write)] = float(write - y)
                    moved = True
                write -= 1
        return moved

    def _fall_or_settle(self):
        """떨어질 것이 있으면 낙하 연출로, 없으면 곧바로 터짐 판정으로."""
        if self.fall_offsets:
            self.fall_vel = FALL_V0
            self.state = "drop"
        else:
            self.state = "settle"
            self.timer = SETTLE_MS

    def fall_offset(self, x, y):
        """그리기용 — 이 칸의 뿌요가 아직 얼마나 위에 있나 (셀)."""
        return self.fall_offsets.get((x, y), 0.0)

    def find_groups(self):
        """4개 이상 붙은 같은 색 덩어리들. 숨은 줄(row 0)은 세지 않는다."""
        seen = [[False] * COLS for _ in range(ROWS)]
        groups = []
        for y in range(HIDDEN_ROW + 1, ROWS):
            for x in range(COLS):
                color = self.grid[y][x]
                if color is None or color == GARBAGE or seen[y][x]:
                    continue
                stack = [(x, y)]
                seen[y][x] = True
                group = []
                while stack:
                    cx, cy = stack.pop()
                    group.append((cx, cy))
                    for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                        nx, ny = cx + dx, cy + dy
                        if (HIDDEN_ROW < ny < ROWS and 0 <= nx < COLS
                                and not seen[ny][nx]
                                and self.grid[ny][nx] == color):
                            seen[ny][nx] = True
                            stack.append((nx, ny))
                if len(group) >= 4:
                    groups.append(group)
        return groups

    def _check_pop(self):
        groups = self.find_groups()
        if not groups:
            self._finish_chain()
            return
        self.chain += 1
        self.max_chain = max(self.max_chain, self.chain)

        cleared = sum(len(g) for g in groups)
        colors = len({self.grid[y][x] for g in groups for x, y in g})
        bonus = (chain_power(self.chain) + COLOR_BONUS.get(colors, 24)
                 + sum(group_bonus(len(g)) for g in groups))
        bonus = max(1, min(999, bonus))
        gain = 10 * cleared * bonus
        self.score += gain
        self.chain_score += gain
        self.pop_gain = gain

        # 터지는 덩어리에 붙어 있는 방해뿌요도 같이 사라진다
        cells = {(x, y) for g in groups for x, y in g}
        extra = set()
        for x, y in cells:
            for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                nx, ny = x + dx, y + dy
                if (0 <= nx < COLS and HIDDEN_ROW < ny < ROWS
                        and self.grid[ny][nx] == GARBAGE):
                    extra.add((nx, ny))
        self.pop_cells = list(cells | extra)
        self.state = "pop"
        self.timer = POP_MS
        if self.chain >= 2:
            self.flash(tr("%d 연쇄!") % self.chain)

    def _do_remove(self):
        for x, y in self.pop_cells:
            self.grid[y][x] = None
        self.pop_cells = []
        self.apply_gravity()
        self._fall_or_settle()

    def board_empty(self):
        return all(v is None for row in self.grid for v in row)

    def target_point(self):
        """마진 타임이 지나면 이 값이 내려가 방해뿌요가 더 많이 날아간다."""
        if not self.opt("margin_time"):
            return TARGET_POINT
        t = self.elapsed / 1000.0
        if t < MARGIN_START:
            return TARGET_POINT
        step = int((t - MARGIN_START) // MARGIN_STEP) + 1
        m = MARGIN_TABLE[min(step, len(MARGIN_TABLE) - 1)]
        return max(1.0, TARGET_POINT * m)

    def _finish_chain(self):
        # 이번 연쇄로 판을 비웠나. 보너스는 지금 주지 않는다 (아래 참고)
        cleared_all = bool(self.chain) and self.board_empty()

        # 번 점수를 방해뿌요로 환산해 들어올 것부터 상쇄한다
        if self.chain_score:
            tp = self.target_point()
            total = self.chain_score + self.leftover
            n = int(total // tp)
            self.leftover = total - n * tp
            # 지난 판에 예약해 둔 전체 지우기 보너스를 이 공격에 얹는다
            if self.zenkeshi:
                self.zenkeshi = False
                self.score += ALL_CLEAR_BONUS
                n += ALL_CLEAR_GARBAGE
                self.flash(tr("싹쓸이 보너스! +%d, 방해뿌요 +%d")
                           % (ALL_CLEAR_BONUS, ALL_CLEAR_GARBAGE))
            if n:
                cancel = min(n, self.pending)
                self.pending -= cancel
                self.sent += n - cancel
            self.chain_score = 0

        # 본가와 같이 보너스는 '다음 공격'에 붙는다. 즉 싹쓸이한 턴에는 표시만
        # 되고, 그 다음 연쇄를 터뜨리는 턴에 점수와 방해뿌요가 함께 나간다.
        if cleared_all:
            self.zenkeshi = True
            self.flash(tr("전체 지우기! 다음 공격에 보너스"))

        if self.pending > 0:
            self.state = "garbage"
            self.timer = GARBAGE_MS
        else:
            self.spawn()

    def drop_garbage(self, n):
        """한 번에 최대 30개(5줄). 남는 개수는 무작위 열에 한 개씩."""
        n = min(n, COLS * 5)
        full, rem = divmod(n, COLS)
        counts = [full] * COLS
        for x in random.sample(range(COLS), rem):
            counts[x] += 1
        for x in range(COLS):
            free_y = ROWS - 1
            while free_y >= 0 and self.grid[free_y][x] is not None:
                free_y -= 1
            left = counts[x]
            while left > 0 and free_y >= 0:
                self.grid[free_y][x] = GARBAGE
                # 천장 위에서 함께 내려오도록 시작 높이를 맞춘다
                self.fall_offsets[(x, free_y)] = float(free_y) + 1.5
                free_y -= 1
                left -= 1
        return n

    # ------------------------------------------------------------- 진행
    def level(self):
        return min(20, 1 + self.pieces // 15)

    def drop_ms(self):
        base = 780.0 * (0.87 ** (self.level() - 1))
        return max(45.0, base / max(0.2, float(self.opt("speed"))))

    def rain_interval(self):
        return max(4500.0, 15000.0 - self.level() * 550.0)

    def flash(self, text, ms=1300.0):
        self.msg = text
        self.msg_t = ms

    def update(self, dt):
        if self.state == "over":
            return
        self.elapsed += dt
        if self.msg_t > 0:
            self.msg_t = max(0.0, self.msg_t - dt)
            if self.msg_t == 0:
                self.msg = ""

        if self.state == "play":
            if self.opt("mode") == "garbage":
                self.rain_t += dt
                if self.rain_t >= self.rain_interval():
                    self.rain_t = 0.0
                    self.pending += min(30, 2 + self.level())
            iv = self.drop_ms()
            self.fall += dt / iv
            while self.fall >= 1.0:
                self.fall -= 1.0
                c = self.cur
                if c and self.fits(c["x"], c["y"] + 1, c["rot"]):
                    c["y"] += 1
                else:
                    self.fall = 0.0
                    break
            if self.grounded():
                self.ground_ms += dt
                if self.ground_ms >= LOCK_DELAY:
                    self.lock()
            else:
                self.ground_ms = 0.0

        elif self.state == "drop":
            # 떠 있는 뿌요를 가속하며 내린다. 다 내려앉은 뒤에 터짐을 본다.
            self.fall_vel = min(FALL_VMAX, self.fall_vel + FALL_ACC * dt)
            step = self.fall_vel * dt
            for key, left in list(self.fall_offsets.items()):
                left -= step
                if left <= 0.0:
                    del self.fall_offsets[key]
                else:
                    self.fall_offsets[key] = left
            if not self.fall_offsets:
                self.state = "settle"
                self.timer = SETTLE_MS

        elif self.state == "settle":
            self.timer -= dt
            if self.timer <= 0:
                self._check_pop()

        elif self.state == "pop":
            self.timer -= dt
            if self.timer <= 0:
                self._do_remove()

        elif self.state == "garbage":
            self.timer -= dt
            if self.timer <= 0:
                dropped = self.drop_garbage(self.pending)
                self.pending -= dropped
                self.flash(tr("방해뿌요 %d개") % dropped)
                # 낙하 -> settle -> (터질 것 없음) -> _finish_chain -> spawn 으로
                # 이어진다. 30개를 넘겨 남은 방해뿌요가 있으면 한 번 더 쏟아진다.
                self._fall_or_settle()


# =============================================================== 뿌요 그리기
def puyo_qcolor(v):
    return QColor(GARBAGE_COLOR if v == GARBAGE else PUYO_COLORS[v])


def paint_puyo(p, left, top, cell, v, conn=(), alpha=1.0):
    """뿌요 한 개. conn 은 같은 색이 이어진 방향들 — 본가처럼 붙어 보이게 한다."""
    base = puyo_qcolor(v)
    r = cell * 0.46
    cx, cy = left + cell / 2.0, top + cell / 2.0
    p.save()
    p.setOpacity(alpha)
    p.setPen(Qt.NoPen)

    # 이어진 방향으로 사각형을 깔아 두 뿌요가 한 덩어리로 보이게 한다
    if v != GARBAGE and conn:
        p.setBrush(base)
        w = r * 1.15
        for d in conn:
            if d == "u":
                p.drawRect(QRectF(cx - w / 2, top - cell * 0.1, w, cell * 0.65))
            elif d == "d":
                p.drawRect(QRectF(cx - w / 2, cy, w, cell * 0.62))
            elif d == "l":
                p.drawRect(QRectF(left - cell * 0.1, cy - w / 2, cell * 0.65, w))
            elif d == "r":
                p.drawRect(QRectF(cx, cy - w / 2, cell * 0.62, w))

    grad = QRadialGradient(QPointF(cx - r * 0.35, cy - r * 0.4), r * 1.7)
    grad.setColorAt(0.0, base.lighter(155))
    grad.setColorAt(0.55, base)
    grad.setColorAt(1.0, base.darker(150))
    p.setBrush(grad)
    p.drawEllipse(QPointF(cx, cy), r, r)

    if v == GARBAGE:
        # 방해뿌요 — 회색 덩어리에 못마땅한 입
        p.setPen(QPen(base.darker(210), max(1.0, cell * 0.055)))
        p.setBrush(Qt.NoBrush)
        p.drawArc(QRectF(cx - r * 0.45, cy - r * 0.1, r * 0.9, r * 0.7),
                  0, 180 * 16)
        p.restore()
        return

    # 눈 — 최신작 뿌요의 얼굴. 셀이 작으면 생략한다.
    if cell >= 16:
        ew, eh = r * 0.36, r * 0.44
        for sx in (-1, 1):
            ex = cx + sx * r * 0.34
            ey = cy - r * 0.12
            p.setBrush(QColor("#ffffff"))
            p.setPen(Qt.NoPen)
            p.drawEllipse(QPointF(ex, ey), ew, eh)
            p.setBrush(QColor("#20242e"))
            p.drawEllipse(QPointF(ex + sx * ew * 0.15, ey + eh * 0.16),
                          ew * 0.46, eh * 0.46)
    # 윗면 반짝임
    p.setBrush(QColor(255, 255, 255, 90))
    p.setPen(Qt.NoPen)
    p.drawEllipse(QPointF(cx - r * 0.32, cy - r * 0.55), r * 0.26, r * 0.17)
    p.restore()


def paint_ghost(p, left, top, cell, v):
    base = puyo_qcolor(v)
    r = cell * 0.42
    p.save()
    p.setOpacity(0.30)
    p.setPen(QPen(base, max(1.0, cell * 0.08)))
    p.setBrush(Qt.NoBrush)
    p.drawEllipse(QPointF(left + cell / 2.0, top + cell / 2.0), r, r)
    p.restore()


class PuyoBoard(QWidget):
    """뿌요 필드. 배경 알파를 0 으로 내리면 뿌요만 공중에 떠 있는 모습이 된다."""

    def __init__(self, win):
        super().__init__(win)
        self.win = win
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self.resync()

    def cell(self):
        return int(self.win.cfg.s["cell"])

    def resync(self):
        c = self.cell()
        self.setFixedSize(COLS * c, int(c * (VISIBLE_ROWS + 0.5)))
        self.update()

    def y_of(self, row):
        """숨은 줄(row 0)은 위쪽 절반이 화면 밖으로 잘려 나간다."""
        return (row - 0.5) * self.cell()

    def paintEvent(self, _event):
        g = self.win.game
        s = self.win.cfg.s
        c = self.cell()
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)

        # ---- 배경 / 격자 ----
        bg = QColor(s["bg_color"])
        bg.setAlpha(int(s["bg_alpha"]))
        if bg.alpha():
            p.setPen(Qt.NoPen)
            p.setBrush(bg)
            p.drawRoundedRect(QRectF(0, 0, self.width(), self.height()),
                              c * 0.18, c * 0.18)
        lift = faint_strength(s)
        ga = grid_alpha_of(s)
        if ga:
            lines = [(x * c, self.y_of(1), x * c, self.height())
                     for x in range(COLS + 1)]
            lines += [(0, self.y_of(row), self.width(), self.y_of(row))
                      for row in range(1, ROWS + 1)]
            draw_lines(p, lines, grid_color(ga, lift), lift)
        # 숨은 줄 경계 — 여기 넘어가면 위험하다는 표시. 보조가 켜져 있으면
        # 격자를 꺼 두어도 이 선만은 남긴다. 판에서 가장 중요한 구분선이다.
        warn_a = min(255, ga * 5)
        if lift:
            warn_a = max(warn_a, 110)
        if warn_a:
            draw_lines(p, [(0, self.y_of(1), self.width(), self.y_of(1))],
                       QColor(255, 120, 120, warn_a), lift,
                       1.0 + lift, Qt.DashLine)
        draw_board_edge(p, self.width(), self.height(), c * 0.18, lift)

        # ---- 쌓인 뿌요 ----
        blink = g.state == "pop" and int(g.timer / 90) % 2 == 0
        pop = set(g.pop_cells)
        for row in range(ROWS):
            for col in range(COLS):
                v = g.grid[row][col]
                if v is None:
                    continue
                off = g.fall_offset(col, row)
                alpha = 0.45 if row == HIDDEN_ROW and off == 0.0 else 1.0
                if (col, row) in pop:
                    alpha *= 0.25 if blink else 1.0
                conn = []
                # 떨어지는 중인 뿌요는 아직 붙지 않았으므로 연결부를 그리지 않는다
                if v != GARBAGE and off == 0.0:
                    for d, dx, dy in (("u", 0, -1), ("d", 0, 1),
                                      ("l", -1, 0), ("r", 1, 0)):
                        nx, ny = col + dx, row + dy
                        if (0 <= nx < COLS and HIDDEN_ROW < ny < ROWS
                                and g.grid[ny][nx] == v
                                and g.fall_offset(nx, ny) == 0.0
                                and (col, row) not in pop
                                and (nx, ny) not in pop
                                and row != HIDDEN_ROW):
                            conn.append(d)
                paint_puyo(p, col * c, self.y_of(row) - off * c, c, v, conn, alpha)

        # ---- 착지 위치 표시 ----
        if g.cur and g.state == "play" and s["show_ghost"]:
            gy = g.ghost_y()
            if gy is not None and gy != g.cur["y"]:
                ghost = dict(g.cur)
                ghost["y"] = gy
                for x, y, v in g.cells(ghost):
                    if y >= 0:
                        paint_ghost(p, x * c, self.y_of(y), c, v)

        # ---- 조작 중인 조 ----
        if g.cur and g.state == "play":
            off = 0.0 if g.grounded() else g.fall * c
            for x, y, v in g.cells():
                paint_puyo(p, x * c, self.y_of(y) + off, c, v)

        # ---- 연쇄 / 안내 문구 ----
        if g.msg:
            self._center_text(p, g.msg, c * 0.62, self.height() * 0.22,
                              QColor("#fff6a8"))
        if self.win.paused and not g.over:
            self._veil(p)
            self._center_text(p, tr("일시정지"), c * 0.72, self.height() * 0.46,
                              QColor("#ffffff"))
            self._center_text(p, self.win.key_hint("pause") + tr(" 로 재개"),
                              c * 0.38, self.height() * 0.56, QColor("#c9d1e0"))
        if g.over:
            self._veil(p)
            self._center_text(p, "GAME OVER", c * 0.68, self.height() * 0.40,
                              QColor("#ff8a95"))
            self._center_text(p, tr("%s점") % format(g.score, ","), c * 0.46,
                              self.height() * 0.50, QColor("#ffffff"))
            self._center_text(p, tr("%s / %s 다시 시작")
                              % (self.win.key_hint("new_game"),
                                 self.win.key_hint("restart")),
                              c * 0.38, self.height() * 0.59, QColor("#c9d1e0"))
        p.end()

    def _veil(self, p):
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(8, 10, 16, 170))
        p.drawRoundedRect(QRectF(0, 0, self.width(), self.height()),
                          self.cell() * 0.18, self.cell() * 0.18)

    def _center_text(self, p, text, size, y, color):
        f = QFont(UI_FONT)
        f.setPixelSize(max(9, int(size)))
        f.setBold(True)
        p.setFont(f)
        path = QPainterPath()
        fm = p.fontMetrics()
        try:
            tw = fm.horizontalAdvance(text)
        except AttributeError:      # Qt 5.10 이하
            tw = fm.width(text)
        x = (self.width() - tw) / 2.0
        path.addText(QPointF(x, y), f, text)
        p.setPen(QPen(QColor(0, 0, 0, 200), max(2.0, size * 0.12)))
        p.setBrush(Qt.NoBrush)
        p.drawPath(path)
        p.setPen(Qt.NoPen)
        p.setBrush(color)
        p.drawPath(path)


class PuyoNext(QWidget):
    """뿌요 NEXT 두 조 + 들어올 방해뿌요 표시."""

    def __init__(self, win):
        super().__init__(win)
        self.win = win
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self.resync()

    def cell(self):
        return max(10, int(self.win.cfg.s["cell"] * 0.62))

    def resync(self):
        c = self.cell()
        self.setFixedSize(c * 3, int(c * 6.3))
        self.update()

    def paintEvent(self, _event):
        g = self.win.game
        c = self.cell()
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)

        f = QFont(UI_FONT)
        f.setPixelSize(max(8, int(c * 0.52)))
        f.setBold(True)
        p.setFont(f)
        p.setPen(QColor(255, 255, 255, 110))
        p.drawText(QRectF(0, 0, self.width(), c * 0.8), Qt.AlignLeft | Qt.AlignVCenter,
                   "NEXT")

        for i in range(2):
            if i >= len(g.queue):
                break
            a, b = g.queue[i]
            scale = 1.0 if i == 0 else 0.78
            cc = c * scale
            x = c * 0.6
            y = c * 1.0 + i * c * 2.4
            p.setOpacity(1.0 if i == 0 else 0.72)
            paint_puyo(p, x, y, cc, b)              # 자뿌요가 위
            paint_puyo(p, x, y + cc, cc, a)         # 축뿌요가 아래
            p.setOpacity(1.0)

        # 들어올 방해뿌요 — 최신작의 예고 표시
        if g.pending:
            p.setPen(QColor("#ffb4b4"))
            p.drawText(QRectF(0, self.height() - c * 0.9, self.width(), c * 0.9),
                       Qt.AlignLeft | Qt.AlignVCenter, tr("예고 %d") % g.pending)
        p.end()


# =============================================================== 설정 패널
# 설정 창 색. 팔레트와 스타일시트를 함께 준다 —— 스타일시트만 주면 거기서
# 빠뜨린 위젯(탭 이름, 스크롤 영역 안쪽, 콤보박스 펼친 목록 등)이 시스템 기본
# 흰 배경으로 남는데 글자색은 밝은 색이 상속되어 글자가 보이지 않는다.
DLG_BG = "#1b1f2a"       # 창 바탕
DLG_FIELD = "#141822"    # 입력칸 바탕
DLG_BTN = "#2b3140"      # 버튼 바탕
DLG_LINE = "#333a49"     # 테두리
DLG_INK = "#e8ecf4"      # 글자
DLG_DIM = "#aeb6c8"      # 흐린 글자
DLG_SEL = "#3d63ff"      # 선택 표시


def puyo_settings_tab(dlg):
    """설정 창의 '게임' 탭 — 뿌요 전용."""
    w = dlg.w
    page = QWidget()
    form = QFormLayout(page)

    colors = QComboBox()
    choices = list(range(3, len(PUYO_COLORS) + 1))
    for n in choices:
        colors.addItem(tr("%d색") % n, n)
    current = max(3, min(len(PUYO_COLORS), int(w.cfg.opt("num_colors"))))
    colors.setCurrentIndex(choices.index(current))
    colors.currentIndexChanged.connect(
        lambda i: dlg._set_game("num_colors", colors.itemData(i)))
    form.addRow(tr("뿌요 색 수"), colors)

    mode = QComboBox()
    mode.addItem(tr("엔드리스 (혼자 연쇄 연습)"), "endless")
    mode.addItem(tr("방해뿌요 (일정 간격으로 방해뿌요가 쏟아진다)"), "garbage")
    mode.setCurrentIndex(["endless", "garbage"].index(w.cfg.opt("mode")))
    mode.currentIndexChanged.connect(
        lambda i: dlg._set_game("mode", mode.itemData(i)))
    form.addRow(tr("모드"), mode)

    margin = QCheckBox(tr("마진 타임 (시간이 지나면 상쇄가 어려워진다)"))
    margin.setChecked(bool(w.cfg.opt("margin_time")))
    margin.toggled.connect(lambda on: dlg._set_game("margin_time", bool(on)))
    form.addRow(margin)

    note = QLabel(
        tr("6×12 필드 + 숨은 13번째 줄, 3열이 막히면 게임 오버.\n"
        "본가 점수식(연쇄·색·덩어리 보너스), 퀵턴·벽 밀기·바닥 밀기,\n"
        "싹쓸이는 다음 공격에 얹히고, 방해뿌요 상쇄와 마진 타임을 따른다."))
    note.setWordWrap(True)
    form.addRow(tr("규칙"), note)
    return page


def puyo_stats(win, g, compact):
    """사이드 패널 글과 상단바 글. (패널 HTML, 상단바 글) 을 돌려준다."""
    mode = tr("방해뿌요") if win.cfg.opt("mode") == "garbage" else tr("엔드리스")
    # 싹쓸이 보너스는 다음 공격에 나가므로 대기 중임을 계속 보여 준다
    zen = (tr("<br><br><span style='color:#ffe066'>싹쓸이 대기<br>"
           "+%d</span>") % ALL_CLEAR_BONUS) if g.zenkeshi else ""
    info = tr("%s점 · %d연쇄") % (format(g.score, ","), g.chain)
    if compact:
        return (tr("<b>%s</b><br>연쇄 <b>%d</b><br>Lv <b>%d</b>%s")
                % (format(g.score, ","), g.chain, g.level(), zen), info)
    return (tr("점수<br><b>%s</b>"
            "<br><br>연쇄 <b>%d</b>"
            "<br>최고연쇄 <b>%d</b>"
            "<br>레벨 <b>%d</b>"
            "<br><br>최고점수<br><b>%s</b>"
            "<br><br>%s<br>전송 <b>%d</b><br>예고 <b>%d</b>%s")
            % (format(g.score, ","), g.chain, g.max_chain, g.level(),
               format(int(win.cfg.rec.get("best", 0)), ","), mode, g.sent,
               g.pending, zen), info)


def puyo_records(g):
    return {"best": g.score, "best_chain": g.max_chain}


PUYO = register_game(GameSpec(
    key="puyo",
    label="뿌요뿌요",
    defaults={"num_colors": 4, "mode": "endless", "margin_time": True},
    engine=PuyoGame,
    board=PuyoBoard,
    side=PuyoNext,
    stats=puyo_stats,
    settings_tab=puyo_settings_tab,
    actions={"left", "right", "soft", "rot_cw", "rot_cw2", "rot_ccw", "hard",
             "g_left", "g_right", "g_soft", "g_rot", "g_hard"},
    records=puyo_records,
))


# =============================================================== 테트리스 규칙
# 본가(뿌요뿌요 테트리스 / 테트리스 가이드라인)를 따른다.
TCOLS = 10                    # 가로 10칸
TVIS = 20                     # 눈에 보이는 20줄
THID = 2                      # 그 위의 숨은 줄 — 조각이 여기서 나온다
TROWS = TVIS + THID

# I S Z L J T O 순서. 색도 가이드라인 색을 따른다.
TETRIS_COLORS = ["#40d5f0", "#5ce65c", "#ff5f5f", "#ffa53d", "#4a7dff",
                 "#c060ff", "#ffe14a"]
TET_NAMES = ["I", "S", "Z", "L", "J", "T", "O"]
TET_I, TET_S, TET_Z, TET_L, TET_J, TET_T, TET_O = range(7)

# 회전칸 크기와 처음 모양 (칸 안의 좌표, y 는 아래로 증가)
TET_BOX = {TET_I: 4, TET_O: 3, TET_S: 3, TET_Z: 3, TET_L: 3, TET_J: 3, TET_T: 3}
TET_SPAWN = {
    TET_I: [(0, 1), (1, 1), (2, 1), (3, 1)],
    TET_S: [(1, 0), (2, 0), (0, 1), (1, 1)],
    TET_Z: [(0, 0), (1, 0), (1, 1), (2, 1)],
    TET_L: [(2, 0), (0, 1), (1, 1), (2, 1)],
    TET_J: [(0, 0), (0, 1), (1, 1), (2, 1)],
    TET_T: [(1, 0), (0, 1), (1, 1), (2, 1)],
    TET_O: [(1, 0), (2, 0), (1, 1), (2, 1)],
}


def _rot_cw(cells, n):
    """칸 안에서 시계 방향으로 한 번 돌린다."""
    return sorted((n - 1 - y, x) for x, y in cells)


def _build_shapes():
    shapes = {}
    for kind, base in TET_SPAWN.items():
        n = TET_BOX[kind]
        states = [sorted(base)]
        for _ in range(3):
            states.append(_rot_cw(states[-1], n))
        if kind == TET_O:                 # O 는 돌아도 그대로다
            states = [sorted(base)] * 4
        shapes[kind] = states
    return shapes


TET_SHAPES = _build_shapes()

# SRS 벽 밀기 표. (회전 전, 회전 후) -> 시도할 (dx, dy) 들.
# 원표는 y 가 위로 증가하므로 여기서는 부호를 뒤집어 담았다.
_SRS_JLSTZ = {
    (0, 1): [(0, 0), (-1, 0), (-1, -1), (0, 2), (-1, 2)],
    (1, 0): [(0, 0), (1, 0), (1, 1), (0, -2), (1, -2)],
    (1, 2): [(0, 0), (1, 0), (1, 1), (0, -2), (1, -2)],
    (2, 1): [(0, 0), (-1, 0), (-1, -1), (0, 2), (-1, 2)],
    (2, 3): [(0, 0), (1, 0), (1, -1), (0, 2), (1, 2)],
    (3, 2): [(0, 0), (-1, 0), (-1, 1), (0, -2), (-1, -2)],
    (3, 0): [(0, 0), (-1, 0), (-1, 1), (0, -2), (-1, -2)],
    (0, 3): [(0, 0), (1, 0), (1, -1), (0, 2), (1, 2)],
}
_SRS_I = {
    (0, 1): [(0, 0), (-2, 0), (1, 0), (-2, 1), (1, -2)],
    (1, 0): [(0, 0), (2, 0), (-1, 0), (2, -1), (-1, 2)],
    (1, 2): [(0, 0), (-1, 0), (2, 0), (-1, -2), (2, 1)],
    (2, 1): [(0, 0), (1, 0), (-2, 0), (1, 2), (-2, -1)],
    (2, 3): [(0, 0), (2, 0), (-1, 0), (2, -1), (-1, 2)],
    (3, 2): [(0, 0), (-2, 0), (1, 0), (-2, 1), (1, -2)],
    (3, 0): [(0, 0), (1, 0), (-2, 0), (1, 2), (-2, -1)],
    (0, 3): [(0, 0), (-1, 0), (2, 0), (-1, -2), (2, 1)],
}
# 180도 회전은 원표에 없다. 본가에서 쓰는 것과 같은 느낌으로, 제자리에서
# 안 되면 위아래·좌우로 한 칸씩만 밀어 본다.
_SRS_180 = [(0, 0), (0, -1), (1, 0), (-1, 0), (1, -1), (-1, -1), (0, 1)]

TET_LOCK_DELAY = 500.0        # 바닥에 닿은 뒤 굳기까지
TET_LOCK_RESETS = 15          # 이동·회전으로 미룰 수 있는 횟수
TET_CLEAR_MS = 220.0          # 줄이 사라지는 연출
TET_SPAWN_MS = 90.0           # 다음 조각이 나오기까지의 뜸
TET_NEXT_COUNT = 5            # NEXT 몇 개를 보여 주나

# 줄 지우기 점수 (레벨 곱하기 전). 가이드라인 값.
TET_LINE_SCORE = {0: 0, 1: 100, 2: 300, 3: 500, 4: 800}
TET_TSPIN_SCORE = {0: 400, 1: 800, 2: 1200, 3: 1600}
TET_MINI_SCORE = {0: 100, 1: 200, 2: 400}
TET_PC_SCORE = {1: 800, 2: 1200, 3: 1800, 4: 2000}
# 상대에게 보내는 줄 수 (뿌요테트 공격표)
TET_ATTACK = {1: 0, 2: 1, 3: 2, 4: 4}
TET_TSPIN_ATTACK = {0: 0, 1: 2, 2: 4, 3: 6}
TET_MINI_ATTACK = {0: 0, 1: 0, 2: 1}
TET_PC_ATTACK = 10
# 콤보 보너스 (콤보 1부터)
TET_COMBO_ATTACK = [0, 1, 1, 2, 2, 3, 3, 4, 4, 4, 5]


def tet_gravity_ms(level):
    """가이드라인 낙하 속도 — 한 칸 내려가는 데 걸리는 시간(ms)."""
    lv = max(1, min(20, level))
    sec = (0.8 - (lv - 1) * 0.007) ** (lv - 1)
    return max(16.0, sec * 1000.0)


class TetrisGame:
    """화면과 무관한 테트리스 규칙.

    상태 흐름
        play    조작 가능
        clear   지운 줄이 사라지는 연출
        spawn   다음 조각이 나오기 전 짧은 뜸
        over    게임 오버

    본가를 따르는 것
        · SRS 회전과 벽 밀기 표 (I 와 나머지가 다른 표를 쓴다)
        · 7-bag — 일곱 조각을 섞어 한 묶음씩 내보낸다
        · 홀드는 조각마다 한 번
        · 굳기 지연 0.5초, 이동·회전으로 15번까지 미룰 수 있다
        · T-스핀 3코너 판정 (앞 두 코너가 비면 미니)
        · B2B, 콤보, 퍼펙트 클리어, 가이드라인 점수와 중력 곡선
    """

    def __init__(self, opt):
        self.opt = opt
        self.reset()

    # ------------------------------------------------------------- 초기화
    def reset(self):
        self.grid = [[None] * TCOLS for _ in range(TROWS)]
        self.bag = []
        self.queue = []
        self.cur = None
        self.held = None
        self.hold_used = False
        self.state = "play"
        self.timer = 0.0
        self.fall = 0.0
        self.ground_ms = 0.0
        self.lock_resets = 0
        self.score = 0
        self.lines = 0
        self.pieces = 0
        self.elapsed = 0.0
        self.combo = -1
        self.max_combo = 0
        self.b2b = False
        self.tspins = 0
        self.tetrises = 0
        self.pending = 0
        self.sent = 0
        self.leftover = 0.0
        self.clear_rows = []
        self.lowest_y = 0              # 이 조각이 여태 닿은 가장 낮은 줄
        self.last_action = ""          # 화면에 띄울 마지막 성과
        self.msg = ""
        self.msg_t = 0.0
        self.rain_t = 0.0
        self.over = False
        self._last_kick = 0
        self._last_was_rotate = False
        self._fill_queue()
        self.spawn()

    # --------------------------------------------------------------- 조각
    def _fill_queue(self):
        while len(self.queue) <= TET_NEXT_COUNT:
            if not self.bag:
                self.bag = list(range(7))
                random.shuffle(self.bag)
            self.queue.append(self.bag.pop())

    def cells(self, cur=None):
        c = cur or self.cur
        if not c:
            return []
        return [(c["x"] + dx, c["y"] + dy, c["kind"])
                for dx, dy in TET_SHAPES[c["kind"]][c["rot"]]]

    def free(self, x, y):
        if x < 0 or x >= TCOLS or y >= TROWS:
            return False
        if y < 0:
            return True
        return self.grid[y][x] is None

    def fits(self, x, y, rot, kind):
        for dx, dy in TET_SHAPES[kind][rot]:
            if not self.free(x + dx, y + dy):
                return False
        return True

    def spawn(self, kind=None):
        self._fill_queue()
        if kind is None:
            kind = self.queue.pop(0)
            self._fill_queue()
        # 가이드라인 등장 위치 — 가운데, 숨은 줄에서 나온다
        x = 3
        y = 0
        if not self.fits(x, y, 0, kind):
            self.cur = None
            self.over = True
            self.state = "over"
            return
        self.cur = {"kind": kind, "x": x, "y": y, "rot": 0}
        self.fall = 0.0
        self.ground_ms = 0.0
        self.lock_resets = 0
        self.lowest_y = y
        self.hold_used = False
        self._last_was_rotate = False
        self.pieces += 1
        self.state = "play"

    def grounded(self):
        c = self.cur
        return bool(c) and not self.fits(c["x"], c["y"] + 1, c["rot"], c["kind"])

    # --------------------------------------------------------------- 조작
    def _touch(self):
        """이동·회전에 성공하면 굳기를 미뤄 준다 (횟수 제한)."""
        if self.grounded() and self.lock_resets < TET_LOCK_RESETS:
            self.lock_resets += 1
            self.ground_ms = 0.0

    def _descended(self):
        """조각이 여태까지보다 더 아래로 내려갔을 때.

        본가 규칙 — 더 낮은 줄에 닿으면 굳기 지연과 미루기 횟수가 모두
        처음으로 돌아간다. 그래서 아래로 내려가는 한 계속 조작할 수 있고,
        같은 높이에서만 열다섯 번까지 미룰 수 있다.
        """
        if self.cur and self.cur["y"] > self.lowest_y:
            self.lowest_y = self.cur["y"]
            self.lock_resets = 0
            self.ground_ms = 0.0

    def move(self, dx):
        if self.state != "play" or not self.cur:
            return False
        c = self.cur
        if self.fits(c["x"] + dx, c["y"], c["rot"], c["kind"]):
            c["x"] += dx
            self._last_was_rotate = False
            self._touch()
            return True
        return False

    def _try_rotate(self, new_rot, kicks):
        c = self.cur
        for i, (dx, dy) in enumerate(kicks):
            if self.fits(c["x"] + dx, c["y"] + dy, new_rot, c["kind"]):
                c["x"] += dx
                c["y"] += dy
                c["rot"] = new_rot
                self._last_kick = i
                self._last_was_rotate = True
                self._touch()
                return True
        return False

    def rotate(self, dir_):
        if self.state != "play" or not self.cur:
            return False
        c = self.cur
        if c["kind"] == TET_O:
            return False
        new_rot = (c["rot"] + dir_ + 4) % 4
        table = _SRS_I if c["kind"] == TET_I else _SRS_JLSTZ
        return self._try_rotate(new_rot, table[(c["rot"], new_rot)])

    def rotate_180(self):
        if self.state != "play" or not self.cur:
            return False
        if self.cur["kind"] == TET_O:
            return False
        return self._try_rotate((self.cur["rot"] + 2) % 4, _SRS_180)

    def soft_drop(self):
        """한 칸 내린다. 바닥에 닿아도 굳히지 않는다.

        본가는 소프트 드롭 중에도 굳기 지연이 그대로 살아 있어, 바닥에 닿은
        뒤에도 좌우로 밀거나 돌려 자리를 잡을 수 있다. 즉시 굳는 것은 하드
        드롭뿐이다.
        """
        if self.state != "play" or not self.cur:
            return
        c = self.cur
        if self.fits(c["x"], c["y"] + 1, c["rot"], c["kind"]):
            c["y"] += 1
            self.score += 1                    # 소프트 드롭 1칸 1점
            self.fall = 0.0
            self._last_was_rotate = False
            self._descended()

    def hard_drop(self):
        if self.state != "play" or not self.cur:
            return
        c = self.cur
        moved = 0
        while self.fits(c["x"], c["y"] + 1, c["rot"], c["kind"]):
            c["y"] += 1
            moved += 1
        self.score += moved * 2                # 하드 드롭 1칸 2점
        self._last_was_rotate = False
        self.lock()

    def ghost_y(self):
        c = self.cur
        if not c:
            return None
        y = c["y"]
        while self.fits(c["x"], y + 1, c["rot"], c["kind"]):
            y += 1
        return y

    def hold(self):
        """조각마다 한 번. 들고 있던 것과 바꾼다."""
        if self.state != "play" or not self.cur or self.hold_used:
            return False
        if not self.opt("use_hold"):
            return False
        kind = self.cur["kind"]
        swap = self.held
        self.held = kind
        if swap is None:
            self.spawn()
        else:
            self.spawn(swap)
        if self.over:
            return False
        self.hold_used = True
        self.flash(tr("홀드"))
        return True

    # ------------------------------------------------------------ 굳히기
    def _corners_filled(self):
        """T 조각 가운데 칸을 둘러싼 네 모서리 중 막힌 것들."""
        c = self.cur
        cx, cy = c["x"] + 1, c["y"] + 1          # 3x3 칸의 가운데
        # 회전 상태별 '앞쪽' 두 모서리
        front = {0: [(-1, -1), (1, -1)], 1: [(1, -1), (1, 1)],
                 2: [(1, 1), (-1, 1)], 3: [(-1, 1), (-1, -1)]}[c["rot"]]
        corners = [(-1, -1), (1, -1), (-1, 1), (1, 1)]
        filled = {}
        for dx, dy in corners:
            filled[(dx, dy)] = not self.free(cx + dx, cy + dy)
        return filled, front

    def _detect_tspin(self):
        """3코너 규칙. (T스핀인가, 미니인가) 를 돌려준다."""
        c = self.cur
        if c["kind"] != TET_T or not self._last_was_rotate:
            return False, False
        filled, front = self._corners_filled()
        if sum(filled.values()) < 3:
            return False, False
        front_filled = sum(filled[p] for p in front)
        # 앞 두 모서리가 모두 막혔으면 정식 T스핀, 아니면 미니.
        # 단, 마지막 벽 밀기(다섯 번째)로 들어간 경우는 정식으로 친다.
        mini = front_filled < 2 and self._last_kick != 4
        return True, mini

    def lock(self):
        tspin, mini = self._detect_tspin()
        for x, y, kind in self.cells():
            if y < 0:
                continue
            self.grid[y][x] = kind
        self.cur = None

        full = [y for y in range(TROWS)
                if all(self.grid[y][x] is not None for x in range(TCOLS))]
        self._score_clear(full, tspin, mini)
        if full:
            self.clear_rows = full
            self.state = "clear"
            self.timer = TET_CLEAR_MS
        else:
            self.state = "spawn"
            self.timer = TET_SPAWN_MS

    def _score_clear(self, full, tspin, mini):
        n = len(full)
        level = self.level()
        if n == 0 and not tspin:
            self.combo = -1
            return
        if n == 0:
            # 줄은 못 지웠지만 T스핀은 점수가 있다. 콤보는 끊긴다.
            self.combo = -1
            base = TET_MINI_SCORE[0] if mini else TET_TSPIN_SCORE[0]
            self.score += base * level
            self.tspins += 1
            self.last_action = tr("T-스핀 미니") if mini else tr("T-스핀")
            self.flash(self.last_action)
            return

        self.combo += 1
        self.max_combo = max(self.max_combo, self.combo)
        self.lines += n

        if tspin:
            base = (TET_MINI_SCORE.get(n, 400) if mini
                    else TET_TSPIN_SCORE.get(n, 1600))
            attack = (TET_MINI_ATTACK.get(n, 1) if mini
                      else TET_TSPIN_ATTACK.get(n, 6))
            name = (tr("T-스핀 미니 ") if mini else tr("T-스핀 ")) + \
                {1: tr("싱글"), 2: tr("더블"), 3: tr("트리플")}.get(n, "")
            hard = True
            self.tspins += 1
        else:
            base = TET_LINE_SCORE.get(n, 800)
            attack = TET_ATTACK.get(n, 4)
            name = {1: tr("싱글"), 2: tr("더블"), 3: tr("트리플"), 4: tr("테트리스")}.get(n, "")
            hard = (n == 4)
            if n == 4:
                self.tetrises += 1

        # 백투백 — 어려운 지우기가 이어지면 1.5배, 보내는 줄도 하나 더
        if hard and self.b2b:
            base = int(base * 1.5)
            attack += 1
            name = "B2B " + name
        self.b2b = hard

        gain = base * level
        if self.combo > 0:
            gain += 50 * self.combo * level
            idx = min(self.combo, len(TET_COMBO_ATTACK) - 1)
            attack += TET_COMBO_ATTACK[idx]
            name += tr(" %d콤보") % self.combo

        # 퍼펙트 클리어 — 지울 줄을 걷어내면 판이 텅 비는가
        occupied = {y for y in range(TROWS)
                    if any(self.grid[y][x] is not None for x in range(TCOLS))}
        if occupied and occupied <= set(full):
            gain += TET_PC_SCORE.get(n, 2000) * level
            attack += TET_PC_ATTACK
            name = tr("퍼펙트 클리어")

        self.score += gain
        self.last_action = name
        self.flash(name)
        self._send(attack)

    def _send(self, lines):
        if lines <= 0:
            return
        cancel = min(lines, self.pending)
        self.pending -= cancel
        self.sent += lines - cancel

    # ------------------------------------------------------------- 방해줄
    def drop_garbage(self, n):
        n = min(n, TVIS)
        hole = random.randrange(TCOLS)
        for _ in range(n):
            del self.grid[0]
            row = [GARBAGE] * TCOLS
            row[hole] = None
            self.grid.append(row)
            if random.random() < 0.3:          # 가끔 구멍 위치가 바뀐다
                hole = random.randrange(TCOLS)
        return n

    def rain_interval(self):
        return max(4000.0, 14000.0 - self.level() * 500.0)

    # ------------------------------------------------------------- 진행
    def level(self):
        return min(20, 1 + self.lines // 10 +
                   (int(self.opt("start_level")) - 1))

    def drop_ms(self):
        return tet_gravity_ms(self.level()) / max(0.2, float(self.opt("speed")))

    def flash(self, text, ms=1300.0):
        self.msg = text
        self.msg_t = ms

    def _finish_clear(self):
        gone = set(self.clear_rows)
        kept = [row for i, row in enumerate(self.grid) if i not in gone]
        while len(kept) < TROWS:
            kept.insert(0, [None] * TCOLS)
        self.grid = kept
        self.clear_rows = []
        self.state = "spawn"
        self.timer = TET_SPAWN_MS

    def update(self, dt):
        if self.state == "over":
            return
        self.elapsed += dt
        if self.msg_t > 0:
            self.msg_t = max(0.0, self.msg_t - dt)
            if self.msg_t == 0:
                self.msg = ""

        if self.state == "play":
            if self.opt("mode") == "garbage":
                self.rain_t += dt
                if self.rain_t >= self.rain_interval():
                    self.rain_t = 0.0
                    self.pending += min(TVIS, 1 + self.level() // 3)
            iv = self.drop_ms()
            self.fall += dt / iv
            while self.fall >= 1.0:
                self.fall -= 1.0
                c = self.cur
                if c and self.fits(c["x"], c["y"] + 1, c["rot"], c["kind"]):
                    c["y"] += 1
                    self._last_was_rotate = False
                    self._descended()
                else:
                    self.fall = 0.0
                    break
            if self.grounded():
                self.ground_ms += dt
                if self.ground_ms >= TET_LOCK_DELAY:
                    self.lock()
            else:
                self.ground_ms = 0.0

        elif self.state == "clear":
            self.timer -= dt
            if self.timer <= 0:
                self._finish_clear()

        elif self.state == "spawn":
            self.timer -= dt
            if self.timer <= 0:
                if self.pending > 0:
                    dropped = self.drop_garbage(self.pending)
                    self.pending -= dropped
                    self.flash(tr("방해줄 %d줄") % dropped)
                self.spawn()


# =============================================================== 테트리스 화면
def tet_qcolor(v):
    return QColor(GARBAGE_COLOR if v == GARBAGE else TETRIS_COLORS[v])


def paint_block(p, left, top, cell, v, alpha=1.0):
    """네모 블록 하나. 위쪽을 밝게, 아래쪽을 어둡게 해서 입체로 보이게 한다."""
    base = tet_qcolor(v)
    p.save()
    p.setOpacity(alpha)
    p.setPen(Qt.NoPen)
    r = QRectF(left + 1, top + 1, cell - 2, cell - 2)
    p.setBrush(base)
    p.drawRoundedRect(r, cell * 0.14, cell * 0.14)
    p.setBrush(base.lighter(145))
    p.drawRoundedRect(QRectF(r.x(), r.y(), r.width(), r.height() * 0.32),
                      cell * 0.12, cell * 0.12)
    p.setBrush(base.darker(140))
    p.drawRoundedRect(QRectF(r.x(), r.y() + r.height() * 0.78,
                             r.width(), r.height() * 0.22),
                      cell * 0.10, cell * 0.10)
    p.restore()


class TetrisBoard(QWidget):
    """테트리스 필드."""

    def __init__(self, win):
        super().__init__(win)
        self.win = win
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self.resync()

    def cell(self):
        return int(self.win.cfg.s["cell"])

    def resync(self):
        c = self.cell()
        self.setFixedSize(TCOLS * c, TVIS * c)
        self.update()

    def y_of(self, row):
        return (row - THID) * self.cell()

    def paintEvent(self, _event):
        g = self.win.game
        s = self.win.cfg.s
        c = self.cell()
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)

        bg = QColor(s["bg_color"])
        bg.setAlpha(int(s["bg_alpha"]))
        if bg.alpha():
            p.setPen(Qt.NoPen)
            p.setBrush(bg)
            p.drawRoundedRect(QRectF(0, 0, self.width(), self.height()),
                              c * 0.16, c * 0.16)
        lift = faint_strength(s)
        ga = grid_alpha_of(s)
        if ga:
            lines = [(x * c, 0, x * c, self.height())
                     for x in range(TCOLS + 1)]
            lines += [(0, row * c, self.width(), row * c)
                      for row in range(TVIS + 1)]
            draw_lines(p, lines, grid_color(ga, lift), lift)
        draw_board_edge(p, self.width(), self.height(), c * 0.16, lift)

        blink = g.state == "clear" and int(g.timer / 55) % 2 == 0
        for row in range(THID, TROWS):
            for col in range(TCOLS):
                v = g.grid[row][col]
                if v is None:
                    continue
                a = 1.0
                if row in g.clear_rows:
                    a = 0.25 if blink else 1.0
                paint_block(p, col * c, self.y_of(row), c, v, a)

        if g.cur and g.state == "play":
            if s["show_ghost"]:
                gy = g.ghost_y()
                if gy is not None and gy != g.cur["y"]:
                    ghost = dict(g.cur)
                    ghost["y"] = gy
                    for x, y, kind in g.cells(ghost):
                        if y >= THID:
                            p.save()
                            p.setOpacity(0.26)
                            p.setPen(QPen(tet_qcolor(kind), max(1.0, c * 0.09)))
                            p.setBrush(Qt.NoBrush)
                            p.drawRoundedRect(
                                QRectF(x * c + 2, self.y_of(y) + 2, c - 4, c - 4),
                                c * 0.12, c * 0.12)
                            p.restore()
            for x, y, kind in g.cells():
                if y >= THID:
                    paint_block(p, x * c, self.y_of(y), c, kind)

        if g.msg:
            self._center_text(p, g.msg, c * 0.60, self.height() * 0.24,
                              QColor("#fff6a8"))
        if self.win.paused and not g.over:
            self._veil(p)
            self._center_text(p, tr("일시정지"), c * 0.70, self.height() * 0.46,
                              QColor("#ffffff"))
            self._center_text(p, self.win.key_hint("pause") + tr(" 로 재개"),
                              c * 0.36, self.height() * 0.55, QColor("#c9d1e0"))
        if g.over:
            self._veil(p)
            self._center_text(p, "GAME OVER", c * 0.64, self.height() * 0.40,
                              QColor("#ff8a95"))
            self._center_text(p, tr("%s점") % format(g.score, ","), c * 0.44,
                              self.height() * 0.50, QColor("#ffffff"))
            self._center_text(p, tr("%s / %s 다시 시작")
                              % (self.win.key_hint("new_game"),
                                 self.win.key_hint("restart")),
                              c * 0.36, self.height() * 0.59, QColor("#c9d1e0"))
        p.end()

    def _veil(self, p):
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(8, 10, 16, 170))
        p.drawRoundedRect(QRectF(0, 0, self.width(), self.height()),
                          self.cell() * 0.16, self.cell() * 0.16)

    def _center_text(self, p, text, size, y, color):
        f = QFont(UI_FONT)
        f.setPixelSize(max(9, int(size)))
        f.setBold(True)
        p.setFont(f)
        fm = p.fontMetrics()
        try:
            tw = fm.horizontalAdvance(text)
        except AttributeError:
            tw = fm.width(text)
        path = QPainterPath()
        path.addText(QPointF((self.width() - tw) / 2.0, y), f, text)
        p.setPen(QPen(QColor(0, 0, 0, 200), max(2.0, size * 0.12)))
        p.setBrush(Qt.NoBrush)
        p.drawPath(path)
        p.setPen(Qt.NoPen)
        p.setBrush(color)
        p.drawPath(path)


class TetrisSide(QWidget):
    """홀드 한 칸과 NEXT 다섯 개."""

    def __init__(self, win):
        super().__init__(win)
        self.win = win
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self.resync()

    def cell(self):
        return max(6, int(self.win.cfg.s["cell"] * 0.42))

    def resync(self):
        c = self.cell()
        # 홀드(글자+칸) + NEXT(글자 + 다섯 개) + 예고 글자 자리
        self.setFixedSize(c * 5,
                          int(c * (3.4 + 1.4 + TET_NEXT_COUNT * 2.5 + 1.6)))
        self.update()

    def _draw_piece(self, p, kind, left, top, c):
        if kind is None:
            return
        cells = TET_SHAPES[kind][0]
        xs = [x for x, _ in cells]
        ys = [y for _, y in cells]
        w = (max(xs) - min(xs) + 1) * c
        ox = left + (c * 4 - w) / 2.0 - min(xs) * c
        oy = top - min(ys) * c
        for x, y in cells:
            paint_block(p, ox + x * c, oy + y * c, c, kind)

    def paintEvent(self, _event):
        g = self.win.game
        c = self.cell()
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        f = QFont(UI_FONT)
        f.setPixelSize(max(8, int(c * 0.75)))
        f.setBold(True)
        p.setFont(f)

        p.setPen(QColor(255, 255, 255, 110))
        p.drawText(QRectF(0, 0, self.width(), c), Qt.AlignLeft | Qt.AlignVCenter,
                   "HOLD")
        if not g.hold_used:
            self._draw_piece(p, g.held, 0, c * 1.2, c)
        else:
            p.setOpacity(0.35)
            self._draw_piece(p, g.held, 0, c * 1.2, c)
            p.setOpacity(1.0)

        top = c * 3.6
        p.setPen(QColor(255, 255, 255, 110))
        p.drawText(QRectF(0, top, self.width(), c),
                   Qt.AlignLeft | Qt.AlignVCenter, "NEXT")
        top += c * 1.3
        for i in range(min(TET_NEXT_COUNT, len(g.queue))):
            p.setOpacity(1.0 if i == 0 else 0.78)
            self._draw_piece(p, g.queue[i], 0, top + i * c * 2.5, c)
        p.setOpacity(1.0)

        if g.pending:
            p.setPen(QColor("#ffb4b4"))
            p.drawText(QRectF(0, self.height() - c * 1.1, self.width(), c * 1.1),
                       Qt.AlignLeft | Qt.AlignVCenter, tr("예고 %d") % g.pending)
        p.end()


def tetris_settings_tab(dlg):
    w = dlg.w
    page = QWidget()
    form = QFormLayout(page)

    lv = QSpinBox()
    lv.setRange(1, 15)
    lv.setValue(int(w.cfg.opt("start_level")))
    lv.valueChanged.connect(lambda v: dlg._set_game("start_level", v))
    form.addRow(tr("시작 레벨"), lv)

    mode = QComboBox()
    mode.addItem(tr("엔드리스 (혼자 쌓기 연습)"), "endless")
    mode.addItem(tr("방해줄 (일정 간격으로 방해줄이 올라온다)"), "garbage")
    mode.setCurrentIndex(["endless", "garbage"].index(w.cfg.opt("mode")))
    mode.currentIndexChanged.connect(
        lambda i: dlg._set_game("mode", mode.itemData(i)))
    form.addRow(tr("모드"), mode)

    hold = QCheckBox(tr("홀드 사용 (조각마다 한 번)"))
    hold.setChecked(bool(w.cfg.opt("use_hold")))
    hold.toggled.connect(lambda on: dlg._set_game("use_hold", bool(on)))
    form.addRow(hold)

    note = QLabel(
        tr("10×20 필드. SRS 회전과 벽 밀기 표(I 는 따로), 7-bag,\n"
        "굳기 지연 0.5초에 이동·회전 15회까지 미루기,\n"
        "T-스핀 3코너 판정, 백투백, 콤보, 퍼펙트 클리어,\n"
        "가이드라인 점수와 중력 곡선, 뿌요테트 공격표를 따른다."))
    note.setWordWrap(True)
    form.addRow(tr("규칙"), note)
    return page


def tetris_stats(win, g, compact):
    info = tr("%s점 · %d줄") % (format(g.score, ","), g.lines)
    act = ("<br><br><span style='color:#ffe066'>%s</span>" % g.last_action
           if g.last_action else "")
    if compact:
        return (tr("<b>%s</b><br>줄 <b>%d</b><br>Lv <b>%d</b>%s")
                % (format(g.score, ","), g.lines, g.level(), act), info)
    b2b = "<br>B2B <b>ON</b>" if g.b2b else ""
    combo = tr("<br>콤보 <b>%d</b>") % g.combo if g.combo > 0 else ""
    mode = tr("방해줄") if win.cfg.opt("mode") == "garbage" else tr("엔드리스")
    return (tr("점수<br><b>%s</b>"
            "<br><br>줄 <b>%d</b>"
            "<br>레벨 <b>%d</b>"
            "<br>테트리스 <b>%d</b>"
            "<br>T-스핀 <b>%d</b>%s%s"
            "<br><br>최고점수<br><b>%s</b>"
            "<br><br>%s<br>전송 <b>%d</b><br>예고 <b>%d</b>%s")
            % (format(g.score, ","), g.lines, g.level(), g.tetrises, g.tspins,
               b2b, combo, format(int(win.cfg.rec.get("best", 0)), ","),
               mode, g.sent, g.pending, act), info)


def tetris_records(g):
    return {"best": g.score, "best_lines": g.lines, "best_combo": g.max_combo}


TETRIS = register_game(GameSpec(
    key="tetris",
    label="테트리스",
    defaults={"start_level": 1, "mode": "endless", "use_hold": True},
    engine=TetrisGame,
    board=TetrisBoard,
    side=TetrisSide,
    stats=tetris_stats,
    settings_tab=tetris_settings_tab,
    actions={"left", "right", "soft", "rot_cw", "rot_cw2", "rot_ccw", "hard",
             "hold", "rot_180",
             "g_left", "g_right", "g_soft", "g_rot", "g_hard", "g_hold"},
    records=tetris_records,
))


# =============================================================== 스도쿠 규칙
# sudoku.com 을 기준으로 만들었다.
#   · 난이도 여섯 단계 (쉬움 / 보통 / 어려움 / 전문가 / 마스터 / 익스트림)
#   · 실수 3번이면 게임 오버
#   · 메모(연필) 모드, 힌트, 되돌리기, 타이머
#   · 숫자를 넣으면 같은 줄·칸·박스의 메모가 자동으로 지워진다
#   · 고른 칸의 줄·칸·박스와 같은 숫자를 함께 밝게 보여 준다
SUD_N = 9                     # 9x9
SUD_BOX = 3
SUD_MISTAKES = 3              # sudoku.com 과 같이 세 번 틀리면 끝 (기본값)
# 몇 번 틀리면 끝낼지 고를 수 있다. 0 은 제한 없음 — 틀린 칸은 그대로 빨갛게
# 남으니, 끝나지 않을 뿐 실수를 눈감아 주는 것은 아니다.
SUD_MISTAKE_CHOICES = [1, 3, 5, 0]
SUD_ALL = frozenset(range(1, 10))

# 난이도 여섯 단계.
#   givens  처음에 주어지는 숫자 개수 범위
#   need    이 난이도를 풀려면 최소한 여기까지는 써야 한다
#   cap     여기보다 어려우면 그 난이도가 아니다
#
# 단계는 SudSolver 가 실제로 풀어 보고 매긴다 (SUD_TECH_NAME 참고).
# 익스트림은 5단계 — 포싱 체인(가설 검증)까지 써야 풀리지만, 그 기법으로는
# 반드시 풀린다. 6단계(논리로 못 품)는 어느 난이도에서도 내보내지 않는다.
# 아래 범위는 실제로 20판씩 만들어 보고 맞춘 값이다. 주어진 숫자가 적을수록
# 필요한 기법도 자연히 올라가므로, 두 축이 서로 어긋나지 않게 잡았다.
SUD_LEVELS = [
    ("easy",    "쉬움",     (40, 45), 0, 0),
    ("medium",  "보통",     (34, 38), 0, 0),
    ("hard",    "어려움",   (30, 33), 0, 2),
    ("expert",  "전문가",   (27, 30), 1, 4),
    ("master",  "마스터",   (25, 28), 3, 4),
    ("extreme", "익스트림", (22, 26), 5, 6),
]
SUD_LEVEL_KEYS = [k for k, _l, _g, _n, _c in SUD_LEVELS]
SUD_LEVEL_LABEL = {k: l for k, l, _g, _n, _c in SUD_LEVELS}
SUD_LEVEL_INFO = {k: (g, n, c) for k, _l, g, n, c in SUD_LEVELS}
# 난이도마다 기본 점수 (다 풀었을 때). 시간·실수·힌트로 깎인다.
SUD_BASE_SCORE = {"easy": 1000, "medium": 2000, "hard": 3500,
                  "expert": 5000, "master": 7000, "extreme": 10000}

# sudoku.com 처럼 "처음부터 있던 숫자"와 "내가 넣은 숫자"를 색으로 가른다.
# 내가 넣은 숫자는 굵은 파랑이라 어두운 판에서도 또렷하게 읽힌다.
SUD_CELL_COLOR = "#7cc4ff"        # 내가 넣은 숫자
SUD_GIVEN_COLOR = "#d3dcea"       # 처음부터 주어진 숫자
SUD_WRONG_COLOR = "#ff7b7b"       # 틀린 숫자
SUD_NOTE_COLOR = "#9aa8c0"        # 메모(연필)
# 메모 모드일 때 쓰는 강조색. 평소의 파랑과 확실히 갈라야 지금 누르면 확정이
# 아니라 연필이 써진다는 것이 한눈에 보인다.
SUD_NOTE_ACCENT = "#ffc247"
SUD_NOTE_ACCENT_DIM = "#8a6b1f"


def sud_peers(i):
    """그 칸과 같은 줄·칸·박스에 있는 칸 번호들 (자기 자신은 뺀다)."""
    r, c = divmod(i, SUD_N)
    out = set()
    for k in range(SUD_N):
        out.add(r * SUD_N + k)
        out.add(k * SUD_N + c)
    br, bc = (r // SUD_BOX) * SUD_BOX, (c // SUD_BOX) * SUD_BOX
    for dr in range(SUD_BOX):
        for dc in range(SUD_BOX):
            out.add((br + dr) * SUD_N + bc + dc)
    out.discard(i)
    return frozenset(out)


SUD_PEERS = [sud_peers(i) for i in range(81)]
SUD_ROWS = [[r * SUD_N + c for c in range(SUD_N)] for r in range(SUD_N)]
SUD_COLS = [[r * SUD_N + c for r in range(SUD_N)] for c in range(SUD_N)]
SUD_BOXES = [[(br * SUD_BOX + dr) * SUD_N + bc * SUD_BOX + dc
              for dr in range(SUD_BOX) for dc in range(SUD_BOX)]
             for br in range(SUD_BOX) for bc in range(SUD_BOX)]
SUD_UNITS = SUD_ROWS + SUD_COLS + SUD_BOXES
SUD_UNITS_OF = [[u for u in SUD_UNITS if i in u] for i in range(81)]


# --------------------------------------------------------------- 풀이기
def sud_solve_count(grid, limit=2):
    """해가 몇 개인지 센다 (limit 개를 찾으면 멈춘다). 유일해 확인용."""
    cand = [0] * 81                       # 비트마스크 1<<1 .. 1<<9
    full = 0
    for v in range(1, 10):
        full |= 1 << v
    for i in range(81):
        if grid[i]:
            continue
        used = 0
        for p in SUD_PEERS[i]:
            if grid[p]:
                used |= 1 << grid[p]
        cand[i] = full & ~used
        if cand[i] == 0:
            return 0

    found = 0
    work = list(grid)

    def rec():
        nonlocal found
        best, best_n = -1, 10
        for i in range(81):
            if work[i]:
                continue
            used = 0
            for p in SUD_PEERS[i]:
                if work[p]:
                    used |= 1 << work[p]
            free = full & ~used
            n = bin(free).count("1")
            if n == 0:
                return
            if n < best_n:
                best, best_n, best_free = i, n, free
                if n == 1:
                    break
        if best < 0:
            found += 1
            return
        for v in range(1, 10):
            if best_free & (1 << v):
                work[best] = v
                rec()
                work[best] = 0
                if found >= limit:
                    return

    rec()
    return found


def sud_solution(grid):
    """해 하나를 돌려준다 (없으면 None)."""
    work = list(grid)

    def rec():
        best, best_free, best_n = -1, 0, 10
        for i in range(81):
            if work[i]:
                continue
            used = set()
            for p in SUD_PEERS[i]:
                if work[p]:
                    used.add(work[p])
            free = SUD_ALL - used
            if not free:
                return False
            if len(free) < best_n:
                best, best_free, best_n = i, free, len(free)
                if best_n == 1:
                    break
        if best < 0:
            return True
        for v in sorted(best_free):
            work[best] = v
            if rec():
                return True
            work[best] = 0
        return False

    return list(work) if rec() else None


def sud_full_grid():
    """완성된 판을 무작위로 하나 만든다."""
    work = [0] * 81

    def rec(i):
        if i == 81:
            return True
        if work[i]:
            return rec(i + 1)
        used = set()
        for p in SUD_PEERS[i]:
            if work[p]:
                used.add(work[p])
        choices = list(SUD_ALL - used)
        random.shuffle(choices)
        for v in choices:
            work[i] = v
            if rec(i + 1):
                return True
            work[i] = 0
        return False

    rec(0)
    return work


# ------------------------------------------------------- 사람이 푸는 방식
def sud_candidates(grid):
    cand = []
    for i in range(81):
        if grid[i]:
            cand.append(set())
            continue
        used = {grid[p] for p in SUD_PEERS[i] if grid[p]}
        cand.append(set(SUD_ALL) - used)
    return cand


def _sud_locked_step(work, cand):
    """가정 안에서 쓰는 후보 가두기 한 번. 뭔가 지웠으면 True."""
    hit = False
    for box in SUD_BOXES:
        for v in SUD_ALL:
            spots = [i for i in box if not work[i] and v in cand[i]]
            if not spots or len(spots) > 3:
                continue
            rows = {i // SUD_N for i in spots}
            cols = {i % SUD_N for i in spots}
            if len(rows) == 1:
                line = SUD_ROWS[next(iter(rows))]
            elif len(cols) == 1:
                line = SUD_COLS[next(iter(cols))]
            else:
                continue
            for i in line:
                if i not in box and v in cand[i]:
                    cand[i].discard(v)
                    hit = True
    return hit


class SudSolver:
    """사람이 쓰는 기법만으로 푸는 풀이기 (추측으로 훑지 않는다).

    쉬운 것부터 쓰고, 뭔가 지워지면 다시 단일값부터 본다.
        singles     단일값 (naked / hidden single)
        locked      후보 가두기 (pointing / claiming)
        subset      드러난·숨은 쌍·삼중·사중
        xwing       X-Wing
        swordfish   Swordfish
        xywing      XY-Wing
        ur          유니크 렉탱글 (해가 하나뿐이라는 사실을 근거로 지운다)
        chain       포싱 체인 — 후보 하나를 가정하고 단일값으로 끝까지 밀어
                    본다. 모순이 나오면 그 후보를 지우고(가설 검증), 두 갈래가
                    같은 칸에 같은 값을 강요하면 그 값을 확정한다.
    """

    STEPS = [("locked", "locked"), ("subset", "subset"),
             ("xwing", "xwing"), ("swordfish", "swordfish"),
             ("jellyfish", "jellyfish"), ("xywing", "xywing"),
             ("xyzwing", "xyzwing"), ("coloring", "coloring"),
             ("ur", "unique_rect"), ("chain", "chain"),
             ("deep_chain", "deep_chain")]

    def __init__(self, grid):
        self.work = list(grid)
        self.cand = [set() if grid[i] else set(SUD_ALL) for i in range(81)]
        for i in range(81):
            if grid[i]:
                for p in SUD_PEERS[i]:
                    self.cand[p].discard(grid[i])
        self.used = {}

    # ------------------------------------------------------------- 기본
    def _place(self, i, v):
        self.work[i] = v
        self.cand[i] = set()
        for p in SUD_PEERS[i]:
            self.cand[p].discard(v)

    def solved(self):
        return all(self.work)

    def broken(self):
        return any(not self.work[i] and not self.cand[i] for i in range(81))

    def singles(self):
        did = again = True
        did = False
        while again:
            again = False
            for i in range(81):
                if not self.work[i] and len(self.cand[i]) == 1:
                    self._place(i, next(iter(self.cand[i])))
                    did = again = True
            for unit in SUD_UNITS:
                for v in SUD_ALL:
                    spots = [i for i in unit
                             if not self.work[i] and v in self.cand[i]]
                    if len(spots) == 1:
                        self._place(spots[0], v)
                        did = again = True
        return did

    # ------------------------------------------------------------ 기법들
    def locked(self):
        hit = False
        for box in SUD_BOXES:
            for v in SUD_ALL:
                spots = [i for i in box
                         if not self.work[i] and v in self.cand[i]]
                if not spots or len(spots) > 3:
                    continue
                rows = {i // SUD_N for i in spots}
                cols = {i % SUD_N for i in spots}
                if len(rows) == 1:
                    line = SUD_ROWS[next(iter(rows))]
                elif len(cols) == 1:
                    line = SUD_COLS[next(iter(cols))]
                else:
                    continue
                for i in line:
                    if i not in box and v in self.cand[i]:
                        self.cand[i].discard(v)
                        hit = True
        for line in SUD_ROWS + SUD_COLS:
            for v in SUD_ALL:
                spots = [i for i in line
                         if not self.work[i] and v in self.cand[i]]
                if not spots or len(spots) > 3:
                    continue
                bs = {(i // SUD_N // SUD_BOX) * SUD_BOX
                      + (i % SUD_N) // SUD_BOX for i in spots}
                if len(bs) != 1:
                    continue
                for i in SUD_BOXES[next(iter(bs))]:
                    if i not in line and v in self.cand[i]:
                        self.cand[i].discard(v)
                        hit = True
        return hit

    def subset(self):
        hit = False
        for unit in SUD_UNITS:
            empties = [i for i in unit if not self.work[i]]
            placed = {self.work[i] for i in unit if self.work[i]}
            for size in (2, 3, 4):
                for combo in itertools.combinations(empties, size):
                    if any(len(self.cand[i]) < 2 for i in combo):
                        continue
                    union = set()
                    for i in combo:
                        union |= self.cand[i]
                    if len(union) != size:
                        continue
                    for i in empties:
                        if i in combo:
                            continue
                        if self.cand[i] & union:
                            self.cand[i] -= union
                            hit = True
                # 숨은 부분집합. 이미 그 줄에 놓인 숫자를 끼워 넣으면 엉뚱한
                # 칸을 지우게 되므로, 아직 안 놓인 숫자 중에서만 고른다.
                for vals in itertools.combinations(sorted(SUD_ALL - placed),
                                                   size):
                    vs = set(vals)
                    spots = [i for i in empties if self.cand[i] & vs]
                    if len(spots) != size:
                        continue
                    if any(not any(v in self.cand[i] for i in spots)
                           for v in vs):
                        continue
                    for i in spots:
                        if self.cand[i] - vs:
                            self.cand[i] &= vs
                            hit = True
        return hit

    def _fish(self, size):
        hit = False
        for lines, other, keyof in ((SUD_ROWS, SUD_COLS, lambda i: i % SUD_N),
                                    (SUD_COLS, SUD_ROWS, lambda i: i // SUD_N)):
            for v in SUD_ALL:
                spots = {}
                for li, line in enumerate(lines):
                    s = [i for i in line
                         if not self.work[i] and v in self.cand[i]]
                    if 2 <= len(s) <= size:
                        spots[li] = s
                for combo in itertools.combinations(sorted(spots), size):
                    keys = set()
                    for li in combo:
                        keys |= {keyof(i) for i in spots[li]}
                    if len(keys) != size:
                        continue
                    inside = {i for li in combo for i in spots[li]}
                    for k in keys:
                        for i in other[k]:
                            if i not in inside and v in self.cand[i]:
                                self.cand[i].discard(v)
                                hit = True
        return hit

    def xwing(self):
        return self._fish(2)

    def swordfish(self):
        return self._fish(3)

    def jellyfish(self):
        return self._fish(4)

    def xywing(self):
        hit = False
        bi = [i for i in range(81)
              if not self.work[i] and len(self.cand[i]) == 2]
        for pivot in bi:
            if len(self.cand[pivot]) != 2:
                continue                  # 앞선 지우기로 줄었을 수 있다
            a, b = sorted(self.cand[pivot])
            wings = [i for i in bi
                     if i in SUD_PEERS[pivot] and len(self.cand[i]) == 2]
            for w1, w2 in itertools.combinations(wings, 2):
                c1, c2 = self.cand[w1], self.cand[w2]
                if len(c1) != 2 or len(c2) != 2 or c1 == c2:
                    continue
                inter = c1 & c2
                if len(inter) != 1:
                    continue
                c = next(iter(inter))
                if c in (a, b):
                    continue
                if not ((c1 == {a, c} and c2 == {b, c})
                        or (c1 == {b, c} and c2 == {a, c})):
                    continue
                for i in SUD_PEERS[w1] & SUD_PEERS[w2]:
                    if i != pivot and c in self.cand[i]:
                        self.cand[i].discard(c)
                        hit = True
        return hit

    def xyzwing(self):
        """XYZ-Wing — 축이 {a,b,c}, 두 날개가 {a,c}·{b,c} 일 때, 셋 모두를
        보는 칸에서는 c 가 설 자리가 없다."""
        hit = False
        tri = [i for i in range(81)
               if not self.work[i] and len(self.cand[i]) == 3]
        bi = [i for i in range(81)
              if not self.work[i] and len(self.cand[i]) == 2]
        for pivot in tri:
            if len(self.cand[pivot]) != 3:
                continue
            wings = [i for i in bi
                     if i in SUD_PEERS[pivot] and len(self.cand[i]) == 2
                     and self.cand[i] <= self.cand[pivot]]
            for w1, w2 in itertools.combinations(wings, 2):
                inter = self.cand[w1] & self.cand[w2]
                if len(inter) != 1:
                    continue
                if self.cand[w1] | self.cand[w2] != self.cand[pivot]:
                    continue
                c = next(iter(inter))
                for i in SUD_PEERS[pivot] & SUD_PEERS[w1] & SUD_PEERS[w2]:
                    if c in self.cand[i]:
                        self.cand[i].discard(c)
                        hit = True
        return hit

    def coloring(self):
        """단순 색칠 — 한 숫자가 어떤 줄에서 딱 두 칸에만 들어갈 수 있으면
        그 둘은 서로 반대다. 이 관계를 따라 두 색으로 칠한다.

          · 같은 색 둘이 한 줄에 같이 있으면 그 색은 전부 거짓이다
          · 두 색을 모두 보는 칸에는 그 숫자가 들어갈 수 없다
        """
        hit = False
        for v in SUD_ALL:
            spots = [i for i in range(81)
                     if not self.work[i] and v in self.cand[i]]
            if len(spots) < 4:
                continue
            link = {i: set() for i in spots}
            for unit in SUD_UNITS:
                here = [i for i in unit if i in link]
                if len(here) == 2:
                    a, b = here
                    link[a].add(b)
                    link[b].add(a)
            seen = set()
            for start in spots:
                if start in seen or not link[start]:
                    continue
                color = {start: 0}
                stack = [start]
                seen.add(start)
                while stack:
                    i = stack.pop()
                    for j in link[i]:
                        if j not in color:
                            color[j] = 1 - color[i]
                            seen.add(j)
                            stack.append(j)
                if len(color) < 4:
                    continue
                groups = ([i for i in color if color[i] == 0],
                          [i for i in color if color[i] == 1])
                # 같은 색 둘이 한 줄에 있으면 그 색은 전부 거짓
                bad = None
                for gi, grp in enumerate(groups):
                    for a, b in itertools.combinations(grp, 2):
                        if b in SUD_PEERS[a]:
                            bad = gi
                            break
                    if bad is not None:
                        break
                if bad is not None:
                    for i in groups[bad]:
                        if v in self.cand[i]:
                            self.cand[i].discard(v)
                            hit = True
                    continue
                # 두 색을 모두 보는 칸에서는 그 숫자를 지운다
                for i in range(81):
                    if self.work[i] or i in color or v not in self.cand[i]:
                        continue
                    if any(a in SUD_PEERS[i] for a in groups[0]) and                        any(b in SUD_PEERS[i] for b in groups[1]):
                        self.cand[i].discard(v)
                        hit = True
        return hit

    def unique_rect(self):
        """유니크 렉탱글 Type 1.

        두 줄·두 칸이 만드는 네 모서리가 박스 두 개에 걸치고 그중 셋이 똑같은
        두 후보 {a,b} 만 가지면, 네 번째 칸에서 a 와 b 를 지운다. 남겨 두면
        a·b 를 서로 바꾼 두 번째 해가 생겨 "해가 하나뿐"이라는 전제가 깨진다.
        """
        hit = False
        for r1, r2 in itertools.combinations(range(SUD_N), 2):
            for c1, c2 in itertools.combinations(range(SUD_N), 2):
                corners = [r1 * SUD_N + c1, r1 * SUD_N + c2,
                           r2 * SUD_N + c1, r2 * SUD_N + c2]
                if any(self.work[i] for i in corners):
                    continue
                boxes = {(i // SUD_N // SUD_BOX) * SUD_BOX
                         + (i % SUD_N) // SUD_BOX for i in corners}
                if len(boxes) != 2:
                    continue
                pairs = [i for i in corners if len(self.cand[i]) == 2]
                if len(pairs) != 3:
                    continue
                base = self.cand[pairs[0]]
                if any(self.cand[i] != base for i in pairs):
                    continue
                extra = [i for i in corners if i not in pairs][0]
                if not (base <= self.cand[extra]):
                    continue
                if len(self.cand[extra]) <= 2:
                    continue
                for v in list(base):
                    if v in self.cand[extra]:
                        self.cand[extra].discard(v)
                        hit = True
        return hit

    # ---------------------------------------------------- 포싱 체인 (가설)
    def _propagate(self, work, cand):
        again = True
        while again:
            again = False
            for i in range(81):
                if work[i]:
                    continue
                if not cand[i]:
                    return None                  # 모순
                if len(cand[i]) == 1:
                    v = next(iter(cand[i]))
                    work[i] = v
                    cand[i] = set()
                    for p in SUD_PEERS[i]:
                        cand[p].discard(v)
                    again = True
            for unit in SUD_UNITS:
                for v in SUD_ALL:
                    if any(work[i] == v for i in unit):
                        continue
                    spots = [i for i in unit if not work[i] and v in cand[i]]
                    if not spots:
                        return None              # 모순
                    if len(spots) == 1:
                        i = spots[0]
                        work[i] = v
                        cand[i] = set()
                        for p in SUD_PEERS[i]:
                            cand[p].discard(v)
                        again = True
        return work

    def _assume(self, i, v, strong=False):
        work = list(self.work)
        cand = [set(c) for c in self.cand]
        work[i] = v
        cand[i] = set()
        for p in SUD_PEERS[i]:
            cand[p].discard(v)
        got = self._propagate(work, cand)
        if got is None or not strong:
            return got
        # 강한 전파 — 단일값이 막히면 후보 가두기까지 써 보고 다시 민다
        for _ in range(6):
            if all(work):
                return work
            if not _sud_locked_step(work, cand):
                return work
            got = self._propagate(work, cand)
            if got is None:
                return None
        return work

    def deep_chain(self):
        """깊은 포싱 체인 — 후보가 셋인 칸까지 가정하고, 가정 안에서도
        후보 가두기까지 써서 민다. 보통 체인으로 안 풀리는 판을 연다."""
        return self.chain(max_cand=3, strong=True, limit=16)

    def chain(self, max_cand=2, strong=False, limit=28):
        bi = [i for i in range(81)
              if not self.work[i] and 2 <= len(self.cand[i]) <= max_cand]
        # 이웃이 많은 칸부터 — 모순이 빨리 드러난다
        bi.sort(key=lambda i: -sum(1 for p in SUD_PEERS[i] if not self.work[p]))
        for i in bi[:limit]:
            if not (2 <= len(self.cand[i]) <= max_cand):
                continue
            vals = sorted(self.cand[i])
            outs = {}
            dead = []
            for v in vals:
                got = self._assume(i, v, strong)
                if got is None:
                    dead.append(v)
                else:
                    outs[v] = got
            if dead and len(dead) < len(vals):
                for v in dead:
                    self.cand[i].discard(v)
                return True
            if len(outs) == len(vals) and len(vals) >= 2:
                # 모든 갈래가 같은 칸에 같은 값을 강요하면 그 값으로 확정된다
                first = outs[vals[0]]
                for j in range(81):
                    if self.work[j] or not first[j]:
                        continue
                    if all(outs[v][j] == first[j] for v in vals[1:]):
                        self._place(j, first[j])
                        return True
        return False

    # ------------------------------------------------------------- 실행
    def solve(self, allow):
        self.singles()
        while True:
            if self.solved():
                return True
            if self.broken():
                return False
            moved = False
            for name, meth in self.STEPS:
                if name not in allow:
                    continue
                if getattr(self, meth)():
                    self.used[name] = self.used.get(name, 0) + 1
                    self.singles()
                    moved = True
                    break
            if not moved:
                return False


# 난이도 등급 — 이 단계까지 써야 풀린다
_T0 = {"singles"}
_T1 = _T0 | {"locked"}
_T2 = _T1 | {"subset"}
_T3 = _T2 | {"xwing", "swordfish", "jellyfish"}
_T4 = _T3 | {"xywing", "xyzwing", "ur", "coloring"}
_T5 = _T4 | {"chain"}
_T6 = _T5 | {"deep_chain"}
SUD_TIERS = [(0, _T0), (1, _T1), (2, _T2), (3, _T3), (4, _T4), (5, _T5),
             (6, _T6)]
SUD_TECH_NAME = {0: "단일값", 1: "후보 가두기", 2: "쌍·삼중",
                 3: "X-Wing·Swordfish", 4: "XY-Wing·색칠·유니크 렉탱글",
                 5: "포싱 체인", 6: "깊은 포싱 체인", 7: "논리로 못 품"}


def sud_grade(grid):
    """쉬운 기법부터 더해 가며, 처음으로 풀리는 단계를 돌려준다.

    6 이 나오면 여기 넣은 기법으로는 못 푼다는 뜻이다 (더 깊은 사슬이나
    추측이 필요하다). 그런 문제는 내보내지 않는다.
    """
    for tier, allow in SUD_TIERS:
        if SudSolver(grid).solve(allow):
            return tier
    return 7


def sud_make_puzzle(level, tries=20, budget=6.0):
    """그 난이도에 맞는 문제를 만든다. (문제, 해답, 기법단계) 를 돌려준다.

    완성된 판에서 숫자를 하나씩 걷어내되, 해가 둘 이상이 되면 되돌린다.
    목표 개수까지 판 뒤 기법 단계를 재서 조건에 맞으면 받아들인다. 시간 예산
    안에 못 맞추면 그때까지 본 것 중 가장 가까운 것을 쓴다.
    """
    band, need, cap = SUD_LEVEL_INFO.get(level, ((30, 34), 0, None))
    lo, hi = band
    deadline = time.monotonic() + budget
    best = None                       # (나쁨 정도, 문제, 해답, 단계)

    for _ in range(tries):
        sol = sud_full_grid()
        puz = list(sol)
        order = list(range(81))
        random.shuffle(order)
        # 범위의 아래끝까지 판다. 목표를 범위 안에서 무작위로 잡으면 거기까지
        # 못 파고 버려지는 판이 많아져, 어려운 난이도일수록 오래 걸린다.
        target = lo
        givens = 81
        for i in order:
            if givens <= target:
                break
            keep = puz[i]
            puz[i] = 0
            if sud_solve_count(puz, 2) != 1:
                puz[i] = keep
            else:
                givens -= 1
        if givens > hi:               # 더 못 팠다 — 이 판은 버린다
            continue

        tech = sud_grade(puz)
        if tech >= 7:
            # 여기 넣은 기법으로 못 푸는 문제다. 사람이 추측으로 찍어야 하니
            # 어느 난이도에서도 내보내지 않는다.
            continue
        bad = 0
        if tech < need:
            bad += (need - tech) * 100
        if cap is not None and tech > cap:
            bad += (tech - cap) * 100
        bad += abs(givens - (lo + hi) // 2)
        if best is None or bad < best[0]:
            best = (bad, list(puz), list(sol), tech)
        if bad < 10:                  # 기법 조건을 만족하고 개수도 범위 안
            return list(puz), list(sol), tech
        if time.monotonic() > deadline:
            break

    if best is None or best[3] >= 7:
        # 시간 안에 하나도 못 만든 아주 드문 경우. 확실히 풀리는 판을 준다.
        sol = sud_full_grid()
        puz = list(sol)
        order = list(range(81))
        random.shuffle(order)
        givens = 81
        for i in order:
            if givens <= 40:
                break
            keep = puz[i]
            puz[i] = 0
            if sud_solve_count(puz, 2) != 1:
                puz[i] = keep
            else:
                givens -= 1
        return puz, sol, sud_grade(puz)
    return best[1], best[2], best[3]


class SudokuGame:
    """화면과 무관한 스도쿠 규칙 (sudoku.com 방식).

        · 실수 3번이면 게임 오버
        · 메모 모드에서 숫자를 누르면 연필 표시를 켜고 끈다
        · 숫자를 넣으면 같은 줄·칸·박스의 그 숫자 메모가 지워진다
        · 힌트는 고른 칸(없으면 아무 빈칸)에 정답을 넣는다
        · 되돌리기는 넣기·지우기·메모를 한 단계씩 물린다
    """

    def __init__(self, opt):
        self.opt = opt
        self.reset()

    def reset(self):
        self.level = self.opt("level")
        if self.level not in SUD_LEVEL_INFO:
            self.level = "easy"
        self.puzzle, self.answer, self.tech = sud_make_puzzle(self.level)
        self.grid = list(self.puzzle)
        self.given = [v != 0 for v in self.puzzle]
        self.notes = [set() for _ in range(81)]
        self.wrong = set()             # 틀린 채로 남아 있는 칸
        self.cursor = self._first_empty()
        self.note_mode = False
        self.mistakes = 0
        self.hints = 0
        self.filled = sum(1 for v in self.puzzle if v)
        self.undo_stack = []
        self.elapsed = 0.0
        self.score = 0
        self.state = "play"
        self.over = False
        self.solved = False
        self.clear_ms = 0.0            # 다 풀기까지 걸린 시간
        self.msg = ""
        self.msg_t = 0.0

    @property
    def limit(self):
        """몇 번 틀리면 끝인지. 0 이면 제한이 없다.

        설정을 그때그때 읽는다. 판을 새로 만들지 않고도 제한을 바꿀 수 있고,
        이미 그만큼 틀린 상태에서 제한을 줄이면 다음 실수에서 끝난다.
        """
        try:
            n = int(self.opt("mistakes"))
        except (TypeError, ValueError):
            n = SUD_MISTAKES
        return max(0, n)

    def mistake_text(self):
        """"2 / 3" 또는 제한이 없으면 "2" 만."""
        return ("%d / %d" % (self.mistakes, self.limit) if self.limit
                else str(self.mistakes))

    def _first_empty(self):
        for i in range(81):
            if not self.grid[i]:
                return i
        return 0

    # --------------------------------------------------------------- 조작
    def move_cursor(self, dx, dy):
        if self.over:
            return
        r, c = divmod(self.cursor, SUD_N)
        c = max(0, min(SUD_N - 1, c + dx))
        r = max(0, min(SUD_N - 1, r + dy))
        self.cursor = r * SUD_N + c

    def select(self, index):
        if 0 <= index < 81 and not self.over:
            self.cursor = index

    def toggle_note_mode(self):
        if self.over:
            return
        self.note_mode = not self.note_mode
        self.flash(tr("메모 ") + (tr("켜짐") if self.note_mode else tr("꺼짐")))

    def _push(self, kind, i, before_v, before_notes, before_wrong):
        self.undo_stack.append((kind, i, before_v, set(before_notes),
                                bool(before_wrong)))
        if len(self.undo_stack) > 200:
            self.undo_stack.pop(0)

    def enter(self, value):
        """숫자를 넣는다. 메모 모드면 연필 표시를 켜고 끈다."""
        if self.over or not (1 <= value <= 9):
            return
        i = self.cursor
        if self.given[i]:
            self.flash(tr("처음부터 있던 숫자입니다"))
            return
        if self.note_mode:
            if self.grid[i]:
                return
            self._push("note", i, self.grid[i], self.notes[i], i in self.wrong)
            if value in self.notes[i]:
                self.notes[i].discard(value)
            else:
                self.notes[i].add(value)
            return

        if self.grid[i] == value:
            return
        self._push("set", i, self.grid[i], self.notes[i], i in self.wrong)
        was_filled = bool(self.grid[i])
        self.grid[i] = value
        self.notes[i] = set()
        if value == self.answer[i]:
            self.wrong.discard(i)
            if not was_filled:
                self.filled += 1
            self._clear_peer_notes(i, value)
            if self.filled >= 81:
                self._finish()
        else:
            # sudoku.com 과 같이 틀린 숫자는 남겨 두고 실수로 센다
            self.wrong.add(i)
            if not was_filled:
                self.filled += 1
            self.mistakes += 1
            self.flash(tr("실수 %s") % self.mistake_text())
            if self.limit and self.mistakes >= self.limit:
                self.state = "over"
                self.over = True

    def _clear_peer_notes(self, i, value):
        for p in SUD_PEERS[i]:
            self.notes[p].discard(value)

    def erase(self):
        if self.over:
            return
        i = self.cursor
        if self.given[i]:
            return
        if not self.grid[i] and not self.notes[i]:
            return
        self._push("erase", i, self.grid[i], self.notes[i], i in self.wrong)
        if self.grid[i]:
            self.filled -= 1
        self.grid[i] = 0
        self.notes[i] = set()
        self.wrong.discard(i)

    def undo(self):
        if self.over or not self.undo_stack:
            return
        kind, i, before_v, before_notes, before_wrong = self.undo_stack.pop()
        if bool(self.grid[i]) != bool(before_v):
            self.filled += 1 if before_v else -1
        self.grid[i] = before_v
        self.notes[i] = set(before_notes)
        if before_wrong:
            self.wrong.add(i)
        else:
            self.wrong.discard(i)
        self.cursor = i

    def hint(self):
        """고른 칸에 정답을 넣는다. 실수로 세지 않고 힌트 수만 올라간다."""
        if self.over:
            return
        i = self.cursor
        if self.given[i] or (self.grid[i] and i not in self.wrong):
            i = self._first_empty_or_wrong()
            if i is None:
                return
            self.cursor = i
        self._push("hint", i, self.grid[i], self.notes[i], i in self.wrong)
        was_filled = bool(self.grid[i])
        self.grid[i] = self.answer[i]
        self.notes[i] = set()
        self.wrong.discard(i)
        if not was_filled:
            self.filled += 1
        self.hints += 1
        self._clear_peer_notes(i, self.grid[i])
        self.flash(tr("힌트 %d번째") % self.hints)
        if self.filled >= 81:
            self._finish()

    def _first_empty_or_wrong(self):
        for i in range(81):
            if not self.grid[i] or i in self.wrong:
                return i
        return None

    def _finish(self):
        if self.wrong:
            return
        self.solved = True
        self.over = True
        self.state = "over"
        # 끝난 뒤에는 시계가 멈추므로 elapsed 가 그대로 "시작부터 클리어까지"다.
        self.clear_ms = self.elapsed
        self.score = self.final_score()
        self.flash(tr("완성!"))

    def final_score(self):
        """sudoku.com 은 점수 식을 공개하지 않는다. 난이도를 바탕으로
        시간·실수·힌트를 깎는 방식으로 비슷하게 맞춰 두었다."""
        base = SUD_BASE_SCORE.get(self.level, 1000)
        minutes = self.elapsed / 60000.0
        time_keep = max(0.35, 1.0 - minutes * 0.03)
        penalty = self.mistakes * 0.12 + self.hints * 0.08
        return max(0, int(base * time_keep * max(0.2, 1.0 - penalty)))

    # ------------------------------------------------------------ 도움 정보
    def remaining(self, value):
        """그 숫자를 앞으로 몇 개 더 놓아야 하나 (sudoku.com 숫자판 표시)."""
        done = sum(1 for i in range(81)
                   if self.grid[i] == value and i not in self.wrong)
        return max(0, SUD_N - done)

    def highlight(self):
        """지금 고른 칸 때문에 밝게 보여 줄 칸들."""
        i = self.cursor
        same = set()
        v = self.grid[i]
        if v:
            same = {j for j in range(81) if self.grid[j] == v}
        return SUD_PEERS[i], same

    def flash(self, text, ms=1300.0):
        self.msg = text
        self.msg_t = ms

    # ------------------------------------------------- 창이 요구하는 이름들
    def move(self, dx):
        self.move_cursor(dx, 0)

    def update(self, dt):
        if self.over:
            return
        self.elapsed += dt
        if self.msg_t > 0:
            self.msg_t = max(0.0, self.msg_t - dt)
            if self.msg_t == 0:
                self.msg = ""

    def time_text(self, ms=None):
        t = int((self.elapsed if ms is None else ms) // 1000)
        return "%d:%02d" % (t // 60, t % 60)


def sudoku_dump(g):
    """풀던 판을 그대로 담는다 (끝난 판은 담지 않는다).

    되돌리기 기록은 담지 않는다. 판을 이어 푸는 데는 필요 없는데 크기만
    커지고, 앱을 껐다 켠 뒤에 "한 수 전"이 무엇인지도 모호하다.
    """
    if g.over or not any(g.grid[i] != g.puzzle[i] for i in range(81)):
        return None               # 끝났거나, 아직 한 수도 안 둔 판
    return {
        "v": 1,
        "level": g.level,
        "tech": g.tech,
        "puzzle": list(g.puzzle),
        "answer": list(g.answer),
        "grid": list(g.grid),
        "notes": [sorted(n) for n in g.notes],
        "wrong": sorted(g.wrong),
        "mistakes": g.mistakes,
        "hints": g.hints,
        "cursor": g.cursor,
        "note_mode": g.note_mode,
        "elapsed": int(g.elapsed),
    }


def sudoku_restore(g, d):
    """담아 둔 판을 되살린다. 조금이라도 어긋나면 손대지 않고 물러난다."""
    try:
        if d.get("v") != 1:
            return False
        puzzle, answer, grid = d["puzzle"], d["answer"], d["grid"]
        notes, wrong = d["notes"], d["wrong"]
        if not all(isinstance(x, list) and len(x) == 81
                   for x in (puzzle, answer, grid, notes)):
            return False
        if not all(isinstance(v, int) and 0 <= v <= 9
                   for v in puzzle + answer + grid):
            return False
        if not sud_valid_solution(answer):
            return False
        # 주어진 숫자는 정답과 같아야 한다 — 딴 데서 온 파일이면 여기서 걸린다
        if any(puzzle[i] and puzzle[i] != answer[i] for i in range(81)):
            return False
        if d.get("level") not in SUD_LEVEL_INFO:
            return False
    except (KeyError, TypeError, ValueError):
        return False

    g.level = d["level"]
    g.tech = int(d.get("tech", 0))
    g.puzzle, g.answer = list(puzzle), list(answer)
    g.grid = list(grid)
    g.given = [v != 0 for v in puzzle]
    g.notes = [set(v for v in n if isinstance(v, int) and 1 <= v <= 9)
               for n in notes]
    # 틀린 칸은 저장값을 믿지 않고 판에서 다시 센다 — 늘 판과 맞는다
    g.wrong = {i for i in range(81)
               if g.grid[i] and not g.given[i] and g.grid[i] != g.answer[i]}
    g.filled = sum(1 for v in g.grid if v)
    g.mistakes = max(0, int(d.get("mistakes", 0)))
    g.hints = max(0, int(d.get("hints", 0)))
    g.cursor = d["cursor"] if isinstance(d.get("cursor"), int) and         0 <= d["cursor"] < 81 else g._first_empty()
    g.note_mode = bool(d.get("note_mode"))
    g.elapsed = float(max(0, int(d.get("elapsed", 0))))
    g.undo_stack = []
    g.score = 0
    g.state = "play"
    g.over = g.solved = False
    g.clear_ms = 0.0
    return True


def sud_valid_solution(cells):
    """완성된 판인지 — 모든 줄·칸·박스에 1~9 가 한 번씩."""
    if any(not (1 <= v <= 9) for v in cells):
        return False
    return all(len({cells[i] for i in unit}) == SUD_N for unit in SUD_UNITS)


# =============================================================== 스도쿠 화면
class SudokuBoard(QWidget):
    """9x9 판. sudoku.com 처럼 고른 칸의 줄·칸·박스와 같은 숫자를 밝게 보여 준다."""

    def __init__(self, win):
        super().__init__(win)
        self.win = win
        self.resync()

    def cell(self):
        # 스도쿠는 9칸이라 낙하 퍼즐과 같은 셀 크기를 쓰면 창이 너무 커진다
        return max(14, int(self.win.cfg.s["cell"] * 0.85))

    def resync(self):
        c = self.cell()
        self.setFixedSize(c * SUD_N, c * SUD_N)
        self.update()

    def cell_at(self, pos):
        c = self.cell()
        col, row = int(pos.x()) // c, int(pos.y()) // c
        if 0 <= col < SUD_N and 0 <= row < SUD_N:
            return row * SUD_N + col
        return None

    def mousePressEvent(self, event):
        if self.win.eat_click_while_paused(event):
            return
        i = self.cell_at(event.pos())
        if i is None:
            event.ignore()
            return
        g = self.win.game
        g.select(i)
        self.update()
        event.accept()

    def paintEvent(self, _event):
        g = self.win.game
        s = self.win.cfg.s
        c = self.cell()
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)

        bg = QColor(s["bg_color"])
        bg.setAlpha(int(s["bg_alpha"]))
        if bg.alpha():
            p.setPen(Qt.NoPen)
            p.setBrush(bg)
            p.drawRoundedRect(QRectF(0, 0, self.width(), self.height()),
                              c * 0.16, c * 0.16)

        peers, same = g.highlight()
        cur = g.cursor
        cur_v = g.grid[cur]
        # 메모 모드에서는 판 전체의 강조색을 바꾼다
        noting = g.note_mode and not g.over
        cur_rgb = (255, 194, 71) if noting else (90, 130, 255)
        # 칸 바탕 — 고른 칸 / 같은 줄·칸·박스 / 같은 숫자
        for i in range(81):
            r, col = divmod(i, SUD_N)
            rect = QRectF(col * c, r * c, c, c)
            fill = None
            if i == cur:
                fill = QColor(*cur_rgb, 120) if noting else QColor(90, 130, 255, 110)
            elif cur_v and i in same:
                fill = QColor(*cur_rgb, 64) if noting else QColor(90, 130, 255, 70)
            elif i in peers:
                fill = QColor(255, 255, 255, 18)
            if i in g.wrong:
                fill = QColor(255, 90, 90, 90)
            if fill:
                p.setPen(Qt.NoPen)
                p.setBrush(fill)
                p.drawRect(rect)

        # 메모 모드면 고른 칸을 점선으로 둘러 준다. 색만으로는 색약인 눈에
        # 안 걸릴 수 있으니 모양도 함께 바꾼다.
        if noting:
            r, col = divmod(cur, SUD_N)
            pen = QPen(QColor(SUD_NOTE_ACCENT), max(1.6, c * 0.07),
                       Qt.DashLine)
            pen.setDashPattern([2.2, 1.8])
            p.setPen(pen)
            p.setBrush(Qt.NoBrush)
            inset = max(1.4, c * 0.06)
            p.drawRect(QRectF(col * c + inset, r * c + inset,
                              c - inset * 2, c - inset * 2))

        # 격자 — 3칸마다 굵게. 박스를 가르는 굵은 선이 이 판의 구분선이라
        # 배경이 옅어도 반드시 보여야 한다.
        lift = faint_strength(s)
        ga = grid_alpha_of(s)
        thin = QColor(255, 255, 255, max(26, ga + 14))
        thick = QColor(255, 255, 255, max(120, ga + 100))
        for k in range(SUD_N + 1):
            box = (k % SUD_BOX == 0)
            # 박스를 가르는 굵은 선은 격자를 꺼도 남긴다. 이게 없으면 3x3 이
            # 어디서 갈리는지 알 수 없어 스도쿠 판 구실을 못 한다.
            if not box and not ga:
                continue
            draw_lines(p, [(k * c, 0, k * c, self.height()),
                           (0, k * c, self.width(), k * c)],
                       thick if box else thin, lift,
                       max(2.0, c * 0.08) if box else 1)
        draw_board_edge(p, self.width(), self.height(), c * 0.16, lift)

        # 숫자와 메모
        num = QFont(UI_FONT)
        num.setPixelSize(max(9, int(c * 0.62)))
        note_font = QFont(UI_FONT)
        note_font.setPixelSize(max(6, int(c * 0.26)))
        # 배경을 옅게 두면 뒤쪽 창이 비쳐, 밝은 문서 위에서는 숫자가 묻힌다.
        # 그럴 때만 숫자 뒤에 어두운 그림자를 한 겹 깔아 준다.
        faint = faint_bg(s)
        # 보조를 켜면 조금 더 진하게. 끄면 예전 값 그대로 둔다.
        shadow = QColor(0, 0, 0, 225 if faint_strength(s) else 190)
        for i in range(81):
            r, col = divmod(i, SUD_N)
            rect = QRectF(col * c, r * c, c, c)
            v = g.grid[i]
            if v:
                if i in g.wrong:
                    color = QColor(SUD_WRONG_COLOR)
                elif g.given[i]:
                    color = QColor(SUD_GIVEN_COLOR)
                else:
                    color = QColor(SUD_CELL_COLOR)
                num.setBold(True)
                p.setFont(num)
                if faint:
                    p.setPen(shadow)
                    p.drawText(rect.translated(1, 1), Qt.AlignCenter, str(v))
                p.setPen(color)
                p.drawText(rect, Qt.AlignCenter, str(v))
            elif g.notes[i]:
                p.setFont(note_font)
                for n in g.notes[i]:
                    nr, nc = divmod(n - 1, 3)
                    sub = QRectF(col * c + nc * c / 3.0, r * c + nr * c / 3.0,
                                 c / 3.0, c / 3.0)
                    if faint:
                        p.setPen(shadow)
                        p.drawText(sub.translated(1, 1), Qt.AlignCenter, str(n))
                    p.setPen(QColor(SUD_NOTE_COLOR))
                    p.drawText(sub, Qt.AlignCenter, str(n))

        # 안내 문구는 판을 가리지 않도록 상단바로 보낸다 (sudoku_stats 참고)
        if self.win.paused and not g.over:
            self._veil(p)
            self._center_text(p, tr("일시정지"), c * 0.62, self.height() * 0.47,
                              QColor("#ffffff"))
            self._center_text(p, tr("%s 또는 클릭으로 재개")
                              % self.win.key_hint("pause"),
                              c * 0.32, self.height() * 0.56, QColor("#c9d1e0"))
        elif g.over:
            self._veil(p)
            if g.solved:
                self._center_text(p, tr("완성!"), c * 0.62, self.height() * 0.34,
                                  QColor("#9cf0a6"))
                self._center_text(p, tr("걸린 시간 %s")
                                  % g.time_text(g.clear_ms),
                                  c * 0.44, self.height() * 0.45,
                                  QColor("#ffffff"))
                best = -int(self.win.cfg.rec.get("best_time_" + g.level, 0))
                secs = int(g.clear_ms // 1000)
                if best <= 0 or secs <= best:
                    tail = tr("최고 기록!")
                    tint = QColor("#ffd97a")
                else:
                    tail = tr("최고 %d:%02d") % (best // 60, best % 60)
                    tint = QColor("#c9d1e0")
                self._center_text(p, tail, c * 0.32, self.height() * 0.53, tint)
                self._center_text(p, tr("%s점") % format(g.score, ","),
                                  c * 0.32, self.height() * 0.61,
                                  QColor("#c9d1e0"))
                tail_y = 0.74
            else:
                out = (tr("실수 1번") if g.limit == 1
                       else tr("실수 %d번") % g.limit)
                self._center_text(p, out, c * 0.60,
                                  self.height() * 0.42, QColor("#ff8a95"))
                tail_y = 0.60
            self._center_text(p, tr("%s / %s 새 문제")
                              % (self.win.key_hint("new_game"),
                                 self.win.key_hint("restart")),
                              c * 0.32, self.height() * tail_y,
                              QColor("#c9d1e0"))
        p.end()

    def _veil(self, p):
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(8, 10, 16, 175))
        p.drawRoundedRect(QRectF(0, 0, self.width(), self.height()),
                          self.cell() * 0.16, self.cell() * 0.16)

    def _center_text(self, p, text, size, y, color):
        f = QFont(UI_FONT)
        f.setPixelSize(max(9, int(size)))
        f.setBold(True)
        p.setFont(f)
        fm = p.fontMetrics()
        try:
            tw = fm.horizontalAdvance(text)
        except AttributeError:
            tw = fm.width(text)
        path = QPainterPath()
        path.addText(QPointF((self.width() - tw) / 2.0, y), f, text)
        p.setPen(QPen(QColor(0, 0, 0, 200), max(2.0, size * 0.12)))
        p.setBrush(Qt.NoBrush)
        p.drawPath(path)
        p.setPen(Qt.NoPen)
        p.setBrush(color)
        p.drawPath(path)


class SudokuPad(QWidget):
    """숫자판 — sudoku.com 처럼 아직 몇 개 남았는지 함께 보여 준다.

    메모·지우개·힌트·되돌리기도 여기서 누를 수 있다.
    """

    # 두 줄로 놓는다. 세로로 네 줄을 쓰면 옆 칸에 남는 높이가 모자라 점수 글이
    # 잘린다.
    #   윗줄  메모 스위치 — 켜고 끄는 것이라 눌러서 끝나는 나머지와 성격이
    #         다르다. 폭을 다 써서 스위치 모양을 제대로 그린다.
    #   아랫줄 지우기 · 힌트 · 되돌리기
    #            긴 이름    동작     짧은 이름  줄  칸  차지하는 칸 수
    # 한 줄에 셋을 놓으면 기본 셀 크기에서 "되돌리기" 가 안 들어가 줄임말을
    # 써야 한다. 두 줄로 나눠 이름을 그대로 쓴다.
    BUTTONS = [("지우기", "erase", "지움", 0, 0, 1),
               ("힌트", "hint", "힌", 0, 1, 1),
               ("되돌리기", "undo", "되돌", 1, 0, 2)]
    BTN_COLS = 2
    BTN_ROWS = 3            # 메모 스위치 한 줄 + 버튼 두 줄
    # 줄이 하나 늘어난 만큼 촘촘하게 — 안 그러면 옆 칸 점수 글에서 "최고 기록"
    # 줄이 밀려 잘린다. (셀 크기의 배수)
    BTN_GAP = 0.24          # 숫자칸과 첫 줄 사이
    BTN_PITCH = 0.72        # 줄 간격
    BTN_H = 0.62            # 버튼 높이

    def __init__(self, win):
        super().__init__(win)
        self.win = win
        self.resync()

    def cell(self):
        return max(16, int(self.win.cfg.s["cell"] * 0.72))

    def resync(self):
        c = self.cell()
        self.setFixedSize(c * 3, int(c * 3 + c * self.BTN_GAP
                                     + c * self.BTN_PITCH * self.BTN_ROWS))
        self.update()

    def _rows_top(self):
        return self.cell() * (3 + self.BTN_GAP)

    def _row_rect(self, row, col=0, span=None):
        c = self.cell()
        span = self.BTN_COLS if span is None else span
        bw = self.width() / self.BTN_COLS
        return QRectF(col * bw + 1, self._rows_top() + row * c * self.BTN_PITCH,
                      bw * span - 2, c * self.BTN_H)

    def _switch_rect(self):
        return self._row_rect(0, 0, self.BTN_COLS)

    def _hit(self, pos):
        c = self.cell()
        x, y = int(pos.x()), int(pos.y())
        if y < c * 3:
            col, row = x // c, y // c
            if 0 <= col < 3 and 0 <= row < 3:
                return ("num", row * 3 + col + 1)
            return None
        top = self._rows_top()
        if y < top:
            return None          # 숫자칸과 스위치 사이 빈 띠 — 아무것도 아니다
        row = int((y - top) // (c * self.BTN_PITCH))
        if row == 0:
            return ("act", "note")           # 윗줄은 전부 메모 스위치
        col = min(self.BTN_COLS - 1, int(x // (self.width() / self.BTN_COLS)))
        for _l, act, _s, br, bc, span in self.BUTTONS:
            if br == row - 1 and bc <= col < bc + span:
                return ("act", act)
        return None

    def mousePressEvent(self, event):
        if self.win.eat_click_while_paused(event):
            return
        hit = self._hit(event.pos())
        if not hit:
            event.ignore()
            return
        kind, what = hit
        if kind == "num":
            self.win.run_action("num_%d" % what)
        else:
            self.win.run_action(what)
        self.update()
        event.accept()

    def paintEvent(self, _event):
        g = self.win.game
        c = self.cell()
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        f = QFont(UI_FONT)
        f.setPixelSize(max(10, int(c * 0.52)))
        f.setBold(True)
        small = QFont(UI_FONT)
        small.setPixelSize(max(7, int(c * 0.26)))
        # 판과 같은 이유로, 배경이 옅으면 글자 뒤에 그림자를 깐다.
        faint = faint_bg(self.win.cfg.s)
        shadow = QColor(0, 0, 0, 190)

        def text(rect, font, color, msg):
            p.setFont(font)
            if faint:
                p.setPen(shadow)
                p.drawText(rect.translated(1, 1), Qt.AlignCenter, msg)
            p.setPen(color)
            p.drawText(rect, Qt.AlignCenter, msg)

        noting = g.note_mode and not g.over
        accent = QColor(SUD_NOTE_ACCENT)
        # 메모일 때는 숫자를 칸 가운데가 아니라 "그 숫자가 메모로 적힐 자리"에
        # 작게 그린다. 누르면 무엇이 어디에 써지는지 눌러 보기 전에 보인다.
        note_key = QFont(UI_FONT)
        note_key.setPixelSize(max(8, int(c * 0.30)))
        note_key.setBold(True)

        for n in range(1, 10):
            row, col = divmod(n - 1, 3)
            rect = QRectF(col * c + 1, row * c + 1, c - 2, c - 2)
            left = g.remaining(n)
            p.setPen(Qt.NoPen)
            if noting:
                p.setBrush(QColor(255, 194, 71, 34 if left else 12))
            else:
                p.setBrush(QColor(255, 255, 255, 16 if left else 6))
            p.drawRoundedRect(rect, c * 0.16, c * 0.16)
            if noting:
                p.setPen(QPen(QColor(255, 194, 71, 110 if left else 45),
                              max(1.0, c * 0.035)))
                p.setBrush(Qt.NoBrush)
                p.drawRoundedRect(rect, c * 0.16, c * 0.16)
                nr, nc = divmod(n - 1, 3)
                grid_h = rect.height() * 0.74
                slot = QRectF(rect.x() + nc * rect.width() / 3.0,
                              rect.y() + nr * grid_h / 3.0,
                              rect.width() / 3.0, grid_h / 3.0)
                text(slot, note_key,
                     accent if left else QColor(SUD_NOTE_ACCENT_DIM), str(n))
                if left:
                    text(QRectF(rect.x(), rect.y() + grid_h,
                                rect.width(), rect.height() - grid_h),
                         small, QColor("#9aa5ba"), str(left))
            else:
                text(rect, f, QColor("#e8ecf4") if left else QColor("#6f7b93"),
                     str(n))
                if left:
                    text(QRectF(rect.x(), rect.y() + rect.height() * 0.62,
                                rect.width(), rect.height() * 0.36),
                         small, QColor("#9aa5ba"), str(left))

        self._paint_switch(p, self._switch_rect(), noting, text, small)

        # 셀을 작게 줄이면 "되돌리기" 같은 긴 말이 칸에 안 들어간다.
        # 들어가는 크기를 찾고, 그래도 안 되면 줄임말로 바꾼다.
        bw = self.width() / self.BTN_COLS
        full = [tr("힌트 %d") % g.hints if a == "hint" else tr(l)
                for l, a, _s, _r, _c, _sp in self.BUTTONS]
        short = [tr("힌%d") % g.hints if a == "hint" else tr(sh)
                 for _l, a, sh, _r, _c, _sp in self.BUTTONS]
        # 폭은 한 칸짜리 버튼이 기준이다 — 거기에 들어가면 다 들어간다
        labels, tiny = self._fit_labels(full, short, bw - 6, c * 0.26)
        for idx, (_label, _act, _sh, br, bc, span) in enumerate(self.BUTTONS):
            rect = self._row_rect(br + 1, bc, span)
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(255, 255, 255, 16))
            p.drawRoundedRect(rect, c * 0.14, c * 0.14)
            p.save()
            p.setClipRect(rect)
            text(rect, tiny, QColor("#e8ecf4"), labels[idx])
            p.restore()
        p.end()

    def _fit_labels(self, full, short, width, ceiling):
        """칸에 들어가는 이름과 글자 크기를 고른다.

        긴 이름을 되도록 크게 쓰고, 그래도 안 들어가면 짧은 이름으로 바꾼다.
        글자를 무작정 줄이면 읽을 수 없는 크기가 되고, 그냥 잘라 내면 "되돌리기"
        가 "되돌리" 처럼 잘려 나간다.
        """
        top = max(7, int(ceiling))
        for labels, floor in ((full, 7), (short, 5)):
            for size in range(top, floor - 1, -1):
                f = QFont(UI_FONT)
                f.setPixelSize(size)
                if self._widest(f, labels) <= width:
                    return labels, f
        f = QFont(UI_FONT)
        f.setPixelSize(5)
        return short, f

    @staticmethod
    def _widest(font, labels):
        fm = QFontMetrics(font)
        try:
            return max(fm.horizontalAdvance(t) for t in labels)
        except AttributeError:
            return max(fm.width(t) for t in labels)

    def _paint_switch(self, p, rect, on, text, font):
        """메모 켜고 끄기 — 눌러서 끝나는 버튼이 아니라 지금 어느 쪽인지
        보여 주는 스위치라, 손잡이가 움직이는 모양으로 그린다."""
        c = self.cell()
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(255, 194, 71, 46) if on
                   else QColor(255, 255, 255, 14))
        p.drawRoundedRect(rect, c * 0.14, c * 0.14)
        if on:
            p.setPen(QPen(QColor(255, 194, 71, 150), max(1.0, c * 0.04)))
            p.setBrush(Qt.NoBrush)
            p.drawRoundedRect(rect, c * 0.14, c * 0.14)

        # 오른쪽에 손잡이가 든 홈, 왼쪽에 이름
        pad = rect.height() * 0.20
        track_h = rect.height() - pad * 2
        track_w = track_h * 1.85
        track = QRectF(rect.right() - pad - track_w, rect.top() + pad,
                       track_w, track_h)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(SUD_NOTE_ACCENT) if on else QColor(255, 255, 255, 40))
        p.drawRoundedRect(track, track_h / 2.0, track_h / 2.0)
        knob = track_h - max(2.0, track_h * 0.22)
        kx = (track.right() - knob - (track_h - knob) / 2.0 if on
              else track.left() + (track_h - knob) / 2.0)
        p.setBrush(QColor("#1b2028") if on else QColor("#c9d1e0"))
        p.drawEllipse(QRectF(kx, track.center().y() - knob / 2.0, knob, knob))

        label = QRectF(rect.left() + pad, rect.top(),
                       track.left() - rect.left() - pad * 2, rect.height())
        p.save()
        p.setClipRect(label)
        text(label, font,
             QColor(SUD_NOTE_ACCENT) if on else QColor("#c9d1e0"), tr("메모"))
        p.restore()


def sudoku_settings_tab(dlg):
    w = dlg.w
    page = QWidget()
    form = QFormLayout(page)

    level = QComboBox()
    for key in SUD_LEVEL_KEYS:
        band, need, cap = SUD_LEVEL_INFO[key]
        level.addItem(tr("%s (%d~%d칸)") % (tr(SUD_LEVEL_LABEL[key]), band[0], band[1]),
                      key)
    cur = w.cfg.opt("level")
    if cur not in SUD_LEVEL_KEYS:
        cur = "easy"
    level.setCurrentIndex(SUD_LEVEL_KEYS.index(cur))
    level.currentIndexChanged.connect(
        lambda i: dlg._set_game("level", level.itemData(i)))
    form.addRow(tr("난이도"), level)

    miss = QComboBox()
    for n in SUD_MISTAKE_CHOICES:
        miss.addItem(tr("제한 없음") if n == 0 else tr("%d번") % n, n)
    cur_m = w.cfg.opt("mistakes")
    miss.setCurrentIndex(SUD_MISTAKE_CHOICES.index(cur_m)
                         if cur_m in SUD_MISTAKE_CHOICES else 1)
    miss.currentIndexChanged.connect(
        lambda i: dlg._set_game("mistakes", miss.itemData(i)))
    form.addRow(tr("실수 허용"), miss)

    note = QLabel(
        tr("sudoku.com 방식이다. 메모(연필)·힌트·되돌리기가 있고,\n"
        "숫자를 넣으면 같은 줄·칸·박스의 그 숫자 메모가 자동으로 지워진다.\n"
        "고른 칸의 줄·칸·박스와 같은 숫자를 함께 밝게 보여 준다.\n"
        "난이도는 주어진 숫자 개수와 풀이에 필요한 기법으로 가른다.\n"
        "난이도를 바꾸면 다음 문제부터 적용된다 (F2 / R 로 새 문제).\n"
        "실수 허용 횟수는 지금 풀던 판에도 바로 적용된다.\n"
        "풀던 판은 앱을 껐다 켜도 그대로 이어서 푼다."))
    note.setWordWrap(True)
    form.addRow(tr("규칙"), note)
    return page


def sudoku_stats(win, g, compact):
    left = 81 - g.filled
    info = tr("%s · 실수 %s") % (g.time_text(g.clear_ms if g.solved else None),
                                g.mistake_text())
    if g.note_mode and not g.over:
        info = tr("✎ 메모 · %s") % info
    if g.msg:
        info = g.msg              # 판을 가리는 대신 상단바에 띄운다
    # 시간 기록은 음수로 담아 둔다 (창은 큰 값으로만 갱신하므로, 음수로 넣어야
    # 더 짧은 시간이 이긴다). 보여 줄 때 되돌린다.
    best = -int(win.cfg.rec.get("best_time_" + g.level, 0))
    best_txt = "-" if best <= 0 else "%d:%02d" % (best // 60, best % 60)
    if compact:
        # 스도쿠 판은 정사각이라 옆 칸이 짧다. 간략형에도 꼭 필요한 것은 담는다.
        return (tr("<b>%s</b>"
                "<br>%s"
                "<br>남은 <b>%d</b>"
                "<br>실수 <b>%s</b>"
                "<br>힌트 <b>%d</b>"
                "<br>최고 <b>%s</b>")
                % (g.time_text(g.clear_ms if g.solved else None),
                   tr(SUD_LEVEL_LABEL.get(g.level, g.level)),
                   left, g.mistake_text(), g.hints, best_txt), info)
    # 다 풀었으면 시계는 멈춰 있으니, 그대로 "시작부터 클리어까지" 걸린 시간이다
    time_row = (tr("걸린 시간<br><b>%s</b>") % g.time_text(g.clear_ms)
                if g.solved else tr("시간<br><b>%s</b>") % g.time_text())
    return (tr("%s"
            "<br><br>난이도<br><b>%s</b>"
            "<br>필요 기법<br><b>%s</b>"
            "<br><br>남은 칸 <b>%d</b>"
            "<br>실수 <b>%s</b>"
            "<br>힌트 <b>%d</b>"
            "<br><br>최고 기록<br><b>%s</b>")
            % (time_row, tr(SUD_LEVEL_LABEL.get(g.level, g.level)),
               tr(SUD_TECH_NAME.get(g.tech, "?")), left, g.mistake_text(),
               g.hints, best_txt), info)


def sudoku_records(g):
    """다 푼 경우에만 기록한다. 시간은 짧을수록 좋으므로 따로 다룬다."""
    if not g.solved:
        return {}
    secs = max(1, int(g.clear_ms // 1000))
    return {"best": g.score, "best_time_" + g.level: -secs}


SUDOKU = register_game(GameSpec(
    key="sudoku",
    label="스도쿠",
    defaults={"level": "easy", "mistakes": SUD_MISTAKES},
    engine=SudokuGame,
    board=SudokuBoard,
    side=SudokuPad,
    stats=sudoku_stats,
    settings_tab=sudoku_settings_tab,
    actions=set(["cur_left", "cur_right", "cur_up", "cur_down",
                 "note", "erase", "erase2", "undo", "hint"]
                + ["num_%d" % n for n in range(1, 10)]),
    records=sudoku_records,
    wants_mouse=True,
    dump=sudoku_dump,
    restore=sudoku_restore,
))


# ================================================================= 지뢰찾기
# 원작(윈도우 지뢰찾기)의 규칙과 조작을 그대로 옮겼다.
#
#   · 난이도 세 단계 — 초급 9x9/10, 중급 16x16/40, 고급 30x16/99
#   · 왼쪽 클릭으로 열고, 오른쪽 클릭으로 깃발 -> 물음표 -> 해제
#   · 숫자 칸에서 양쪽 버튼(또는 가운데 버튼)으로 주변 한꺼번에 열기
#   · 0 이면 주변이 줄줄이 열린다
#   · 첫 칸은 절대 지뢰가 아니다
#   · 남은 지뢰 수 = 지뢰 - 깃발 (원작처럼 음수까지 내려간다)
#   · 다 열면 남은 지뢰에 깃발이 자동으로 꽂히고 시간이 멈춘다
#   · 지뢰를 밟으면 밟은 칸이 빨갛게, 나머지 지뢰가 모두 드러나고
#     잘못 꽂은 깃발에는 X 가 그려진다
MINE_LEVELS = [
    # key,            라벨,     가로, 세로, 지뢰
    ("beginner",     "초급", 9, 9, 10),
    ("intermediate", "중급", 16, 16, 40),
    ("expert",       "고급", 30, 16, 99),
]
MINE_LEVEL_KEYS = [k for k, _l, _w, _h, _m in MINE_LEVELS]
MINE_LEVEL_LABEL = {k: l for k, l, _w, _h, _m in MINE_LEVELS}
MINE_LEVEL_INFO = {k: (w, h, m) for k, _l, w, h, m in MINE_LEVELS}

# 칸 상태
MINE_HIDDEN = 0
MINE_OPEN = 1
MINE_FLAG = 2
MINE_QUESTION = 3

# 원작의 숫자 색을 그대로 쓴다. 다만 원작은 밝은 회색 판이라 4(남색)·7(검정)
# 이 이 앱의 어두운 판에서는 안 보인다. 그 둘만 같은 계열의 밝은 쪽으로
# 올렸다 — 색이 주는 뜻(몇 번인지 색으로 외운 기억)은 그대로 남는다.
MINE_NUM_COLORS = {
    1: "#4f9bff",   # 원작 파랑 (#0000ff) — 어두운 판에 맞게 밝게
    2: "#3fbf5f",   # 초록 (#008000)
    3: "#ff6b6b",   # 빨강 (#ff0000)
    4: "#9d8bff",   # 남색 (#000080) -> 밝은 남보라
    5: "#d9744f",   # 진한 빨강 (#800000)
    6: "#3fc9c9",   # 청록 (#008080)
    7: "#e8ecf4",   # 검정 (#000000) -> 흰색
    8: "#a8b0c0",   # 회색 (#808080)
}
MINE_FLAG_COLOR = "#ff5a5a"
MINE_BOOM_COLOR = "#c0392b"
MINE_MAX_TIME = 999          # 원작 표시 한계


class MineGame:
    """화면과 무관한 지뢰찾기 규칙."""

    def __init__(self, opt):
        self.opt = opt
        self.reset()

    def reset(self):
        self.level = self.opt("level")
        if self.level not in MINE_LEVEL_INFO:
            self.level = "beginner"
        self.w, self.h, self.mines = MINE_LEVEL_INFO[self.level]
        self.n = self.w * self.h
        self.mine = [False] * self.n
        self.cell_state = [MINE_HIDDEN] * self.n
        self.num = [0] * self.n
        self.opened = 0
        self.cursor = 0
        self.started = False          # 첫 칸을 열었는가 (시계도 여기서 돈다)
        self.elapsed = 0.0
        self.clear_ms = 0.0
        self.score = 0
        self.state = "play"
        self.over = False
        self.won = False
        self.boom = -1                # 밟은 지뢰
        self.msg = ""
        self.msg_t = 0.0
        self._scatter()
        self._count()

    # ------------------------------------------------------------ 판 만들기
    def _scatter(self):
        """지뢰를 흩뿌린다. 원작처럼 판을 만들 때 미리 깔아 둔다."""
        for i in random.sample(range(self.n), self.mines):
            self.mine[i] = True

    def _count(self):
        for i in range(self.n):
            self.num[i] = sum(1 for j in self.neighbors(i) if self.mine[j])

    def _first_click_safe(self, i):
        """원작(winmine) 그대로 — 첫 칸이 지뢰면 왼쪽 위부터 훑어 옮긴다.

        지뢰를 미리 깔고 첫 칸만 비켜 주는 방식이다. 첫 칸이 반드시 0 이 되도록
        주변까지 비워 주는 요즘 방식과는 다르다. 원작 그대로 두었다.
        """
        if not self.mine[i]:
            return
        self.mine[i] = False
        for j in range(self.n):
            if j != i and not self.mine[j]:
                self.mine[j] = True
                break
        self._count()

    def neighbors(self, i):
        r, c = divmod(i, self.w)
        for dr in (-1, 0, 1):
            for dc in (-1, 0, 1):
                if dr == 0 and dc == 0:
                    continue
                rr, cc = r + dr, c + dc
                if 0 <= rr < self.h and 0 <= cc < self.w:
                    yield rr * self.w + cc

    # --------------------------------------------------------------- 조작
    def move_cursor(self, dx, dy):
        if self.over:
            return
        r, c = divmod(self.cursor, self.w)
        c = max(0, min(self.w - 1, c + dx))
        r = max(0, min(self.h - 1, r + dy))
        self.cursor = r * self.w + c

    def select(self, i):
        if 0 <= i < self.n and not self.over:
            self.cursor = i

    def reveal(self, i=None):
        """칸을 연다. 깃발이 꽂힌 칸은 열리지 않는다 (물음표는 열린다)."""
        if self.over:
            return
        i = self.cursor if i is None else i
        if not (0 <= i < self.n):
            return
        st = self.cell_state[i]
        if st == MINE_FLAG or st == MINE_OPEN:
            return
        if not self.started:
            self.started = True
            self._first_click_safe(i)
        if self.mine[i]:
            self._blow(i)
            return
        self._flood(i)
        self._check_win()

    def _flood(self, start):
        """0 이면 주변이 줄줄이 열린다. 깃발이 꽂힌 칸은 건너뛴다."""
        stack = [start]
        while stack:
            i = stack.pop()
            if self.cell_state[i] in (MINE_OPEN, MINE_FLAG):
                continue
            self.cell_state[i] = MINE_OPEN
            self.opened += 1
            if self.num[i] == 0:
                stack.extend(self.neighbors(i))

    def chord(self, i=None):
        """숫자 칸 주변을 한꺼번에 열기 (원작의 양쪽 버튼 누르기).

        꽂아 둔 깃발 수가 그 숫자와 같을 때만 열린다. 깃발이 틀렸으면
        그대로 지뢰를 밟는다 — 원작도 봐주지 않는다.
        """
        if self.over:
            return
        i = self.cursor if i is None else i
        if not (0 <= i < self.n) or self.cell_state[i] != MINE_OPEN:
            return
        if self.num[i] == 0:
            return
        around = list(self.neighbors(i))
        flags = sum(1 for j in around if self.cell_state[j] == MINE_FLAG)
        if flags != self.num[i]:
            return
        for j in around:
            if self.cell_state[j] in (MINE_OPEN, MINE_FLAG):
                continue
            if self.mine[j]:
                self._blow(j)
                return
            self._flood(j)
        self._check_win()

    def flag(self, i=None):
        """깃발 -> 물음표 -> 해제. 물음표는 설정에서 끌 수 있다 (원작도 옵션)."""
        if self.over:
            return
        i = self.cursor if i is None else i
        if not (0 <= i < self.n) or self.cell_state[i] == MINE_OPEN:
            return
        marks = bool(self.opt("marks"))
        st = self.cell_state[i]
        if st == MINE_HIDDEN:
            self.cell_state[i] = MINE_FLAG
        elif st == MINE_FLAG:
            self.cell_state[i] = MINE_QUESTION if marks else MINE_HIDDEN
        else:
            self.cell_state[i] = MINE_HIDDEN

    # --------------------------------------------------------------- 판정
    def _blow(self, i):
        self.boom = i
        self.over = True
        self.won = False
        self.state = "over"
        self.flash(tr("지뢰!"))

    def _check_win(self):
        if self.opened < self.n - self.mines:
            return
        # 원작처럼 남은 지뢰에 깃발을 꽂아 주고 시계를 멈춘다
        for j in range(self.n):
            if self.mine[j] and self.cell_state[j] != MINE_OPEN:
                self.cell_state[j] = MINE_FLAG
        self.won = True
        self.over = True
        self.state = "over"
        self.clear_ms = self.elapsed
        self.score = self.final_score()
        self.flash(tr("클리어!"))

    def final_score(self):
        """원작에는 점수가 없고 시간만 남는다. 이 앱은 점수 칸을 쓰므로
        난이도와 걸린 시간으로 견줄 수 있는 값을 만들어 둔다."""
        base = {"beginner": 1000, "intermediate": 3000, "expert": 8000}
        secs = max(1, int(self.clear_ms // 1000))
        keep = max(0.3, 1.0 - secs / 600.0)
        return max(1, int(base.get(self.level, 1000) * keep))

    # ------------------------------------------------------------ 표시용
    def flags_used(self):
        return sum(1 for st in self.cell_state if st == MINE_FLAG)

    def left(self):
        """남은 지뢰 수. 원작처럼 깃발을 많이 꽂으면 음수가 된다."""
        return self.mines - self.flags_used()

    def time_text(self, ms=None):
        t = int((self.elapsed if ms is None else ms) // 1000)
        return "%d" % min(MINE_MAX_TIME, t)

    def flash(self, text, ms=1300.0):
        self.msg = text
        self.msg_t = ms

    # ------------------------------------------- 창이 요구하는 이름들
    def move(self, dx):
        self.move_cursor(dx, 0)

    def update(self, dt):
        if self.over:
            return
        if self.started:
            self.elapsed += dt
        if self.msg_t > 0:
            self.msg_t = max(0.0, self.msg_t - dt)
            if self.msg_t == 0:
                self.msg = ""


class MineBoard(QWidget):
    """원작처럼 안 열린 칸은 도톰하게, 열린 칸은 납작하게 그린다."""

    def __init__(self, win):
        super().__init__(win)
        self.win = win
        self._chording = False
        # 오른쪽 클릭은 깃발이다. 창이 오른쪽 클릭에 메뉴를 달아 두었으므로,
        # 판에서는 메뉴 요청 자체가 생기지 않게 막는다. PreventContextMenu 는
        # 부모로 넘기지도 않는다 — Ignored 로 두면 창까지 올라가 메뉴가 뜬다.
        # 메뉴는 상단바·점수 칸에서 오른쪽 클릭하거나 Ctrl+R, 트레이로 연다.
        self.setContextMenuPolicy(Qt.PreventContextMenu)
        self.resync()

    def cell(self):
        # 고급은 가로 30칸이라 낙하 퍼즐과 같은 셀 크기를 쓰면 창이 화면을 넘는다
        return max(11, int(self.win.cfg.s["cell"] * 0.62))

    def resync(self):
        g = self.win.game
        c = self.cell()
        self.setFixedSize(c * g.w, c * g.h)
        self.update()

    def cell_at(self, pos):
        g = self.win.game
        c = self.cell()
        col, row = int(pos.x()) // c, int(pos.y()) // c
        if 0 <= col < g.w and 0 <= row < g.h:
            return row * g.w + col
        return None

    # --------------------------------------------------------------- 마우스
    def mousePressEvent(self, event):
        if self.win.eat_click_while_paused(event):
            return
        i = self.cell_at(event.pos())
        if i is None:
            event.ignore()
            return
        g = self.win.game
        g.select(i)
        both = bool(event.buttons() & Qt.LeftButton) and \
            bool(event.buttons() & Qt.RightButton)
        if both or event.button() == Qt.MiddleButton:
            # 원작의 양쪽 버튼 누르기. 여기서 바로 열고, 떼는 순간은 무시한다.
            self._chording = True
            g.chord(i)
        elif event.button() == Qt.LeftButton:
            g.reveal(i)
        elif event.button() == Qt.RightButton:
            g.flag(i)
        self.update()
        event.accept()

    def mouseReleaseEvent(self, event):
        # 양쪽 누르기로 이미 열었으면, 버튼을 하나씩 뗄 때 또 열지 않는다
        if not event.buttons():
            self._chording = False
        event.accept()

    def paintEvent(self, _event):
        g = self.win.game
        s = self.win.cfg.s
        c = self.cell()
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)

        bg = QColor(s["bg_color"])
        bg.setAlpha(int(s["bg_alpha"]))
        if bg.alpha():
            p.setPen(Qt.NoPen)
            p.setBrush(bg)
            p.drawRoundedRect(QRectF(0, 0, self.width(), self.height()),
                              c * 0.16, c * 0.16)

        lift = faint_strength(s)
        faint = faint_bg(s)
        shadow = QColor(0, 0, 0, 225 if lift else 190)
        num_font = QFont(UI_FONT)
        num_font.setPixelSize(max(8, int(c * 0.66)))
        num_font.setBold(True)

        for i in range(g.n):
            r, col = divmod(i, g.w)
            rect = QRectF(col * c, r * c, c, c)
            st = g.cell_state[i]
            shown_mine = g.over and not g.won and g.mine[i] and st != MINE_FLAG
            if st == MINE_OPEN or shown_mine:
                self._draw_open(p, rect, c, i, g, num_font, faint, shadow)
            else:
                self._draw_tile(p, rect, c, lift)
                if st == MINE_FLAG:
                    wrong = g.over and not g.won and not g.mine[i]
                    self._draw_flag(p, rect, c, wrong)
                elif st == MINE_QUESTION:
                    self._draw_glyph(p, rect, num_font, "?",
                                     QColor("#dfe5f0"), faint, shadow)

        ga = grid_alpha_of(s)
        if ga:
            lines = [(x * c, 0, x * c, self.height()) for x in range(g.w + 1)]
            lines += [(0, y * c, self.width(), y * c) for y in range(g.h + 1)]
            draw_lines(p, lines, grid_color(ga, lift), lift)

        # 키보드로 고른 칸
        if not g.over:
            r, col = divmod(g.cursor, g.w)
            p.setBrush(Qt.NoBrush)
            p.setPen(QPen(QColor(255, 255, 255, 210), max(1.4, c * 0.09)))
            p.drawRect(QRectF(col * c + 1, r * c + 1, c - 2, c - 2))

        draw_board_edge(p, self.width(), self.height(), c * 0.16, lift)

        if self.win.paused and not g.over:
            self._veil(p)
            self._center(p, tr("일시정지"), c * 0.9, 0.46, QColor("#ffffff"))
            self._center(p, tr("%s 또는 클릭으로 재개")
                         % self.win.key_hint("pause"),
                         c * 0.46, 0.56, QColor("#c9d1e0"))
        elif g.over:
            self._veil(p)
            if g.won:
                self._center(p, tr("클리어!"), c * 0.95, 0.38,
                             QColor("#9cf0a6"))
                self._center(p, tr("걸린 시간 %s초") % g.time_text(g.clear_ms),
                             c * 0.6, 0.49, QColor("#ffffff"))
                best = -int(self.win.cfg.rec.get("best_time_" + g.level, 0))
                secs = int(g.clear_ms // 1000)
                if best <= 0 or secs <= best:
                    self._center(p, tr("최고 기록!"), c * 0.46, 0.57,
                                 QColor("#ffd97a"))
                else:
                    self._center(p, tr("최고 %d초") % best, c * 0.46, 0.57,
                                 QColor("#c9d1e0"))
            else:
                self._center(p, tr("지뢰를 밟았다"), c * 0.8, 0.45,
                             QColor("#ff8a95"))
            self._center(p, tr("%s / %s 새 게임")
                         % (self.win.key_hint("new_game"),
                            self.win.key_hint("restart")),
                         c * 0.44, 0.66, QColor("#c9d1e0"))
        p.end()

    # ------------------------------------------------------------- 칸 그리기
    def _draw_tile(self, p, rect, c, lift):
        """안 열린 칸 — 원작처럼 도톰하게 (왼쪽 위 밝게, 오른쪽 아래 어둡게).

        베벨은 칸 안쪽으로만 그린다. 칸 경계에 걸쳐 그리면 옆 칸의 밝은 면과
        이 칸의 어두운 면이 맞닿아, 도톰해 보이는 대신 지저분한 줄만 남는다.
        """
        p.setPen(Qt.NoPen)
        inner = rect.adjusted(0.5, 0.5, -0.5, -0.5)
        p.setBrush(QColor(255, 255, 255, 74 if lift else 62))
        p.drawRect(inner)
        bevel = max(1.0, c * 0.13)
        p.setBrush(QColor(255, 255, 255, 92))
        p.drawRect(QRectF(inner.x(), inner.y(), inner.width(), bevel))
        p.drawRect(QRectF(inner.x(), inner.y(), bevel, inner.height()))
        p.setBrush(QColor(0, 0, 0, 110))
        p.drawRect(QRectF(inner.x(), inner.bottom() - bevel,
                          inner.width(), bevel))
        p.drawRect(QRectF(inner.right() - bevel, inner.y(),
                          bevel, inner.height()))

    def _draw_open(self, p, rect, c, i, g, font, faint, shadow):
        p.setPen(Qt.NoPen)
        if g.mine[i]:
            if i == g.boom:
                p.setBrush(QColor(MINE_BOOM_COLOR))
                p.drawRect(rect)
            self._draw_mine(p, rect, c)
            return
        v = g.num[i]
        if v:
            self._draw_glyph(p, rect, font, str(v),
                             QColor(MINE_NUM_COLORS[v]), faint, shadow)

    def _draw_glyph(self, p, rect, font, text, color, faint, shadow):
        p.setFont(font)
        if faint:
            p.setPen(shadow)
            p.drawText(rect.translated(1, 1), Qt.AlignCenter, text)
        p.setPen(color)
        p.drawText(rect, Qt.AlignCenter, text)

    def _draw_flag(self, p, rect, c, wrong):
        x, y, w = rect.x(), rect.y(), rect.width()
        pole = QRectF(x + w * 0.46, y + w * 0.22, max(1.0, w * 0.09), w * 0.56)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor("#e8ecf4"))
        p.drawRect(pole)
        p.drawRect(QRectF(x + w * 0.26, y + w * 0.72,
                          w * 0.48, max(1.0, w * 0.1)))
        tri = QPainterPath()
        tri.moveTo(x + w * 0.46, y + w * 0.2)
        tri.lineTo(x + w * 0.2, y + w * 0.36)
        tri.lineTo(x + w * 0.46, y + w * 0.52)
        tri.closeSubpath()
        p.setBrush(QColor(MINE_FLAG_COLOR))
        p.drawPath(tri)
        if wrong:
            # 잘못 꽂은 깃발 — 원작처럼 X 로 알려 준다
            p.setBrush(Qt.NoBrush)
            p.setPen(QPen(QColor("#ff4d4d"), max(1.4, c * 0.1)))
            p.drawLine(QPointF(x + w * 0.16, y + w * 0.16),
                       QPointF(x + w * 0.84, y + w * 0.84))
            p.drawLine(QPointF(x + w * 0.84, y + w * 0.16),
                       QPointF(x + w * 0.16, y + w * 0.84))

    def _draw_mine(self, p, rect, c):
        """지뢰. 원작은 밝은 회색 판 위의 검은 공인데, 이 앱은 판이 어두우니
        밝고 어두운 쪽을 맞바꿨다 (검게 그리면 판에 묻혀 안 보인다)."""
        cx, cy = rect.center().x(), rect.center().y()
        rad = rect.width() * 0.24
        body = QColor("#f2f4f8")
        p.setPen(QPen(body, max(1.2, c * 0.08)))
        for dx, dy in ((1, 0), (0, 1), (0.72, 0.72), (0.72, -0.72)):
            p.drawLine(QPointF(cx - dx * rad * 1.75, cy - dy * rad * 1.75),
                       QPointF(cx + dx * rad * 1.75, cy + dy * rad * 1.75))
        p.setPen(QPen(QColor(0, 0, 0, 160), max(1.0, c * 0.05)))
        p.setBrush(body)
        p.drawEllipse(QRectF(cx - rad, cy - rad, rad * 2, rad * 2))
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(0, 0, 0, 150))
        p.drawEllipse(QRectF(cx - rad * 0.15, cy - rad * 0.15,
                             rad * 0.5, rad * 0.5))

    def _veil(self, p):
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(8, 10, 16, 180))
        p.drawRoundedRect(QRectF(0, 0, self.width(), self.height()),
                          self.cell() * 0.16, self.cell() * 0.16)

    def _center(self, p, text, size, ratio, color):
        f = QFont(UI_FONT)
        f.setPixelSize(max(9, int(size)))
        f.setBold(True)
        p.setFont(f)
        fm = p.fontMetrics()
        try:
            tw = fm.horizontalAdvance(text)
        except AttributeError:
            tw = fm.width(text)
        path = QPainterPath()
        path.addText(QPointF((self.width() - tw) / 2.0,
                             self.height() * ratio), f, text)
        p.setPen(QPen(QColor(0, 0, 0, 200), max(2.0, size * 0.12)))
        p.setBrush(Qt.NoBrush)
        p.drawPath(path)
        p.setPen(Qt.NoPen)
        p.setBrush(color)
        p.drawPath(path)


def mine_settings_tab(dlg):
    w = dlg.w
    page = QWidget()
    form = QFormLayout(page)

    level = QComboBox()
    for key in MINE_LEVEL_KEYS:
        ww, hh, mm = MINE_LEVEL_INFO[key]
        level.addItem(tr("%s (%dx%d, 지뢰 %d)")
                      % (tr(MINE_LEVEL_LABEL[key]), ww, hh, mm), key)
    cur = w.cfg.opt("level")
    if cur not in MINE_LEVEL_KEYS:
        cur = "beginner"
    level.setCurrentIndex(MINE_LEVEL_KEYS.index(cur))
    level.currentIndexChanged.connect(
        lambda i: dlg._set_game("level", level.itemData(i)))
    form.addRow(tr("난이도"), level)

    marks = QCheckBox(tr("물음표 표시 쓰기"))
    marks.setChecked(bool(w.cfg.opt("marks")))
    marks.toggled.connect(lambda on: dlg._set_game("marks", bool(on)))
    form.addRow("", marks)

    note = QLabel(
        tr("원작 규칙 그대로다. 왼쪽 클릭으로 열고 오른쪽 클릭으로 깃발을 꽂는다.\n"
        "숫자 칸에서 양쪽 버튼(또는 가운데 버튼)을 누르면 주변이 한꺼번에 열린다.\n"
        "꽂아 둔 깃발 수가 그 숫자와 같을 때만 열리고, 깃발이 틀렸으면 터진다.\n"
        "첫 칸은 절대 지뢰가 아니다.\n"
        "난이도를 바꾸면 다음 게임부터 적용된다 (F2 / R 로 새 게임)."))
    note.setWordWrap(True)
    form.addRow(tr("규칙"), note)
    return page


def mine_stats(win, g, compact):
    info = tr("%s초 · 남은 지뢰 %d") % (g.time_text(), g.left())
    if g.msg:
        info = g.msg
    best = -int(win.cfg.rec.get("best_time_" + g.level, 0))
    best_txt = "-" if best <= 0 else tr("%d초") % best
    time_row = (tr("걸린 시간<br><b>%s초</b>") % g.time_text(g.clear_ms)
                if g.won else tr("시간<br><b>%s초</b>") % g.time_text())
    if compact:
        return (tr("<b>%s초</b>"
                "<br>%s"
                "<br><br>지뢰 <b>%d</b>"
                "<br>깃발 <b>%d</b>"
                "<br>최고 <b>%s</b>")
                % (g.time_text(g.clear_ms if g.won else None),
                   tr(MINE_LEVEL_LABEL.get(g.level, g.level)),
                   g.left(), g.flags_used(), best_txt), info)
    return (tr("%s"
            "<br><br>난이도<br><b>%s</b>"
            "<br><br>남은 지뢰 <b>%d</b>"
            "<br>꽂은 깃발 <b>%d</b>"
            "<br>연 칸 <b>%d</b>/%d"
            "<br><br>최고 기록<br><b>%s</b>")
            % (time_row, tr(MINE_LEVEL_LABEL.get(g.level, g.level)),
               g.left(), g.flags_used(), g.opened, g.n - g.mines,
               best_txt), info)


def mine_records(g):
    """이긴 경우에만 기록한다. 원작처럼 시간이 기록이라 짧을수록 좋다."""
    if not g.won:
        return {}
    secs = max(1, int(g.clear_ms // 1000))
    return {"best": g.score, "best_time_" + g.level: -secs}


MINESWEEPER = register_game(GameSpec(
    key="mine",
    label="지뢰찾기",
    defaults={"level": "beginner", "marks": True},
    engine=MineGame,
    board=MineBoard,
    stats=mine_stats,
    settings_tab=mine_settings_tab,
    actions={"cur_left", "cur_right", "cur_up", "cur_down",
             "reveal", "flag", "chord"},
    records=mine_records,
    wants_mouse=True,
))


def force_fusion_style():
    """Qt 스타일을 Fusion 으로 고정한다.

    윈도우 기본 스타일(windowsvista)은 스타일시트를 준 위젯과 그렇지 않은
    위젯을 섞어 그려서, 버튼 같은 일부만 시스템 흰색으로 남는다. 글자색은
    밝은 색이 적용되므로 흰 바탕에 흰 글씨가 되어 읽을 수 없다. Fusion 은
    팔레트를 그대로 따르므로 이런 뒤섞임이 생기지 않는다.
    """
    QApplication.setStyle("Fusion")


def dialog_palette():
    """스타일시트가 닿지 않는 곳까지 어두운 색으로 맞춘다."""
    pal = QPalette()
    bg, field, ink = QColor(DLG_BG), QColor(DLG_FIELD), QColor(DLG_INK)
    for role in (QPalette.Window, QPalette.Button, QPalette.ToolTipBase):
        pal.setColor(role, bg)
    pal.setColor(QPalette.Base, field)
    pal.setColor(QPalette.AlternateBase, bg)
    for role in (QPalette.WindowText, QPalette.Text, QPalette.ButtonText,
                 QPalette.ToolTipText, QPalette.BrightText):
        pal.setColor(role, ink)
    pal.setColor(QPalette.PlaceholderText, QColor(DLG_DIM))
    pal.setColor(QPalette.Highlight, QColor(DLG_SEL))
    pal.setColor(QPalette.HighlightedText, QColor("#ffffff"))
    pal.setColor(QPalette.Disabled, QPalette.WindowText, QColor(DLG_DIM))
    pal.setColor(QPalette.Disabled, QPalette.Text, QColor(DLG_DIM))
    pal.setColor(QPalette.Disabled, QPalette.ButtonText, QColor(DLG_DIM))
    return pal


def dialog_stylesheet():
    """바탕색을 지정하는 곳에서는 글자색도 반드시 같이 지정한다."""
    return """
    QDialog { background-color: %(bg)s; }
    QLabel, QCheckBox { background-color: transparent; color: %(ink)s; }
    QTabWidget::pane { background-color: %(bg)s; border: 1px solid %(line)s;
                       border-radius: 4px; }
    QTabBar::tab { background-color: %(btn)s; color: %(dim)s;
                   padding: 6px 16px; margin-right: 2px;
                   border: 1px solid %(line)s; border-bottom: none;
                   border-top-left-radius: 4px; border-top-right-radius: 4px; }
    QTabBar::tab:selected { background-color: %(bg)s; color: #ffffff; }
    QTabBar::tab:hover { background-color: #39404f; color: %(ink)s; }
    QScrollArea { background-color: %(bg)s; border: none; }
    QScrollArea > QWidget > QWidget { background-color: %(bg)s; color: %(ink)s; }
    QLineEdit, QSpinBox, QDoubleSpinBox, QComboBox, QKeySequenceEdit {
        background-color: %(field)s; color: %(ink)s;
        border: 1px solid %(line)s; border-radius: 4px; padding: 3px;
        selection-background-color: %(sel)s; selection-color: #ffffff; }
    QComboBox QAbstractItemView {
        background-color: %(field)s; color: %(ink)s;
        border: 1px solid %(line)s; outline: none;
        selection-background-color: %(sel)s; selection-color: #ffffff; }
    QPushButton { background-color: %(btn)s; color: %(ink)s;
                  border: 1px solid #3a4152; border-radius: 4px;
                  padding: 4px 8px; }
    QPushButton:hover { background-color: #39404f; }
    QPushButton:pressed { background-color: #222736; }
    QScrollBar:vertical { background: %(bg)s; width: 10px; margin: 0; }
    QScrollBar:horizontal { background: %(bg)s; height: 10px; margin: 0; }
    QScrollBar::handle { background: #3a4152; border-radius: 5px; }
    QScrollBar::handle:vertical { min-height: 24px; }
    QScrollBar::handle:horizontal { min-width: 24px; }
    QScrollBar::add-line, QScrollBar::sub-line { width: 0; height: 0; }
    QScrollBar::add-page, QScrollBar::sub-page { background: none; }
    QToolTip { background-color: %(btn)s; color: %(ink)s;
               border: 1px solid #3a4152; }
    """ % {"bg": DLG_BG, "field": DLG_FIELD, "btn": DLG_BTN, "line": DLG_LINE,
           "ink": DLG_INK, "dim": DLG_DIM, "sel": DLG_SEL}


def style_dialog(dlg):
    """설정 창을 어둡게 칠한다.

    팔레트를 자손 위젯 하나하나에 직접 넣는 것이 핵심이다. 부모에게만 주고
    상속에 맡기면, 스크롤 영역 안쪽 위젯이나 QKeySequenceEdit 내부처럼 Qt 가
    자기 팔레트를 따로 들고 있는 위젯이 시스템 기본 흰색으로 남는다. 글자색은
    밝은 색이 적용되어 흰 바탕에 흰 글씨가 되고, 그 항목은 읽을 수도 없고
    무엇을 고르는지도 알 수 없게 된다.
    """
    pal = dialog_palette()
    dlg.setPalette(pal)
    for child in dlg.findChildren(QWidget):
        child.setPalette(pal)
    dlg.setStyleSheet(dialog_stylesheet())


class ShortcutRow(QWidget):
    """동작 하나에 대한 키 입력칸 + 해제 / 기본값 버튼."""

    changed = pyqtSignal(str, str)          # (동작 id, 새 키)

    def __init__(self, action_id, default_seq, current_seq, parent=None):
        super().__init__(parent)
        self.action_id = action_id
        self.default_seq = default_seq

        self.edit = QKeySequenceEdit(QKeySequence(current_seq))
        self.edit.setMinimumWidth(96)
        self.edit.editingFinished.connect(self._emit)
        self.edit.keySequenceChanged.connect(self._emit)

        clear = QPushButton(tr("해제"))
        clear.setToolTip(tr("이 동작에 키를 지정하지 않음"))
        clear.setFixedWidth(42)
        clear.setFocusPolicy(Qt.NoFocus)
        clear.clicked.connect(lambda: self.set_seq(""))

        reset = QPushButton(tr("기본"))
        reset.setToolTip(tr("기본값 %s 으로") % (default_seq or tr("없음")))
        reset.setFixedWidth(42)
        reset.setFocusPolicy(Qt.NoFocus)
        reset.clicked.connect(lambda: self.set_seq(default_seq))

        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(4)
        row.addWidget(self.edit, 1)
        row.addWidget(clear)
        row.addWidget(reset)

    def set_seq(self, seq_text):
        self.edit.setKeySequence(QKeySequence(seq_text))
        self.changed.emit(self.action_id, seq_text)

    def _emit(self):
        self.changed.emit(self.action_id, self.edit.keySequence().toString())


class SettingsDialog(QDialog):
    """화면 / 게임 / 단축키 세 묶음. 바꾸는 즉시 창에 반영된다."""

    def __init__(self, win):
        super().__init__(win)
        self.w = win
        self.setWindowTitle(tr("설정"))
        self.setMinimumSize(520, 560)

        tabs = QTabWidget()
        tabs.addTab(self._screen_tab(), tr("화면"))
        tabs.addTab(self._game_tab(), tr("게임"))
        tabs.addTab(self._keys_tab(), tr("단축키"))

        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        # 표준 버튼 글은 Qt 가 제 나름의 언어로 넣는다 — 앱 언어에 맞춘다
        buttons.button(QDialogButtonBox.Close).setText(tr("닫기"))
        buttons.rejected.connect(self.accept)

        lay = QVBoxLayout(self)
        lay.addWidget(tabs)
        lay.addWidget(buttons)

    # ------------------------------------------------------------- 화면 탭
    def _screen_tab(self):
        s = self.w.cfg.s
        page = QWidget()
        form = QFormLayout(page)

        cell = QSpinBox()
        cell.setRange(12, 48)
        cell.setValue(int(s["cell"]))
        cell.setSuffix(" px")
        cell.valueChanged.connect(self._set_cell)
        form.addRow(tr("셀 크기"), cell)

        color = QPushButton(s["bg_color"])
        color.clicked.connect(lambda: self._pick_color(color))
        form.addRow(tr("배경색"), color)

        self.bg_slider = QSlider(Qt.Horizontal)
        self.bg_slider.setRange(0, 255)
        self.bg_slider.setValue(int(s["bg_alpha"]))
        self.bg_slider.valueChanged.connect(self._set_bg_alpha)
        form.addRow(tr("배경 진하기 (0 = 배경 지우기)"), self.bg_slider)

        aid = QCheckBox(tr("배경이 옅을 때 또렷하게"))
        aid.setChecked(bool(s.get("visibility_aid", True)))
        aid.setToolTip(tr("끄면 보조선과 받침 없이 예전처럼 그린다."))
        aid.toggled.connect(lambda on: self._set_flag_restyle("visibility_aid", on))
        form.addRow("", aid)

        grid_on = QCheckBox(tr("격자선 보이기"))
        grid_on.setChecked(bool(s.get("show_grid", True)))
        form.addRow("", grid_on)

        self.grid_slider = QSlider(Qt.Horizontal)
        self.grid_slider.setRange(0, 90)
        self.grid_slider.setValue(int(s["grid_alpha"]))
        self.grid_slider.valueChanged.connect(self._set_grid_alpha)
        self.grid_slider.setEnabled(grid_on.isChecked())
        form.addRow(tr("격자선 진하기"), self.grid_slider)

        grid_on.toggled.connect(self._set_show_grid)

        self.op_slider = QSlider(Qt.Horizontal)
        self.op_slider.setRange(15, 100)
        self.op_slider.setValue(int(float(s["opacity"]) * 100))
        self.op_slider.valueChanged.connect(
            lambda v: self.w.set_opacity(v / 100.0, from_dialog=True))
        form.addRow(tr("창 투명도"), self.op_slider)

        for key, label in (("always_on_top", tr("항상 위")),
                           ("frameless", tr("테두리 없음")),
                           ("show_topbar", tr("상단바 표시")),
                           ("show_panel", tr("사이드 패널 표시")),
                           ("show_ghost", tr("착지 위치 표시"))):
            chk = QCheckBox(label)
            chk.setChecked(bool(s[key]))
            chk.toggled.connect(lambda on, k=key: self._set_flag(k, on))
            form.addRow(chk)

        icons = QHBoxLayout()
        holder = QWidget()
        holder.setLayout(icons)
        for key, label in (("btn_hide", tr("숨기기")), ("btn_settings", tr("설정")),
                           ("btn_restart", tr("재시작")),
                           ("btn_pause", tr("일시정지 / 재개"))):
            chk = QCheckBox(label)
            chk.setChecked(bool(s[key]))
            chk.toggled.connect(lambda on, k=key: self._set_flag(k, on))
            icons.addWidget(chk)
        form.addRow(QLabel(tr("상단바 아이콘")))
        form.addRow(holder)

        lang = QComboBox()
        for code, name in LANG_NAMES:
            lang.addItem(name, code)
        lang.setCurrentIndex([c for c, _n in LANG_NAMES].index(LANG))
        lang.currentIndexChanged.connect(
            lambda i: self._set_language(lang.itemData(i)))
        form.addRow(tr("표시 언어"), lang)

        mode = QComboBox()
        mode.addItem(tr("숨은 창 (작업 표시줄·Alt+Tab 에서 제외)"), "hidden")
        mode.addItem(tr("위장 창 (다른 제목으로 표시)"), "disguise")
        mode.addItem(tr("보통 창"), "normal")
        mode.setCurrentIndex(["hidden", "disguise", "normal"].index(
            s["window_mode"]))
        mode.currentIndexChanged.connect(
            lambda i: self._set_window_mode(mode.itemData(i)))
        form.addRow(tr("창 모드"), mode)

        title = QLineEdit(s["disguise_title"])
        title.textChanged.connect(self._set_title)
        form.addRow(tr("위장 제목"), title)

        blur = QCheckBox(tr("포커스를 잃으면 자동으로 숨기기"))
        blur.setChecked(bool(s["hide_on_blur"]))
        blur.toggled.connect(lambda on: self._set_flag("hide_on_blur", on))
        form.addRow(blur)

        pause = QCheckBox(tr("숨길 때 자동 일시정지"))
        pause.setChecked(bool(s["pause_on_hide"]))
        pause.toggled.connect(lambda on: self._set_flag("pause_on_hide", on))
        form.addRow(pause)
        return page

    # ------------------------------------------------------------- 게임 탭
    # ------------------------------------------------------------- 게임 탭
    def _game_tab(self):
        """게임 선택과 공용 항목을 얹고, 그 아래에 게임별 설정을 붙인다."""
        page = QWidget()
        outer = QVBoxLayout(page)
        outer.setContentsMargins(0, 0, 0, 0)

        head = QWidget()
        form = QFormLayout(head)

        picker = QComboBox()
        keys = list(GAMES)
        for k in keys:
            picker.addItem(tr(GAMES[k].label), k)
        picker.setCurrentIndex(keys.index(self.w.cfg.game))
        picker.setEnabled(len(keys) > 1)
        picker.currentIndexChanged.connect(
            lambda i: self._pick_game(picker.itemData(i)))
        form.addRow(tr("게임"), picker)

        speed = QDoubleSpinBox()
        speed.setRange(0.3, 3.0)
        speed.setSingleStep(0.1)
        speed.setValue(float(self.w.cfg.s["speed"]))
        speed.setSuffix(" ×")
        speed.valueChanged.connect(lambda v: self._set_game("speed", v))
        form.addRow(tr("낙하 속도"), speed)

        outer.addWidget(head)
        outer.addWidget(self.w.cfg.spec.settings_tab(self), 1)
        return page

    def _set_language(self, code):
        if code == LANG:
            return
        self.w.cfg.s["lang"] = set_language(code)
        self.w.retranslate()
        self.w.schedule_save()
        # 이미 만들어 둔 글은 그대로라, 설정 창은 닫고 다시 열게 한다
        self.accept()

    def _pick_game(self, key):
        if key == self.w.cfg.game:
            return
        self.w.switch_game(key)
        # 아래에 붙인 게임별 설정이 이전 게임 것이라 그대로 두면 헷갈린다
        self.accept()

    # ----------------------------------------------------------- 단축키 탭
    def _keys_tab(self):
        page = QWidget()
        outer = QVBoxLayout(page)
        area = QScrollArea()
        area.setWidgetResizable(True)
        area.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        inner = QWidget()
        form = QFormLayout(inner)

        form.addRow(QLabel(tr("<b>앱 단축키</b> (창이 활성일 때)")))
        for action_id, label, default in LOCAL_ACTIONS:
            if not self.w.action_available(action_id):
                continue          # 지금 게임에 없는 조작은 보여 주지 않는다
            row = ShortcutRow(action_id, default,
                              self.w.cfg.keys.get(action_id, default))
            row.changed.connect(self._key_changed)
            form.addRow(tr(label), row)

        form.addRow(QLabel(tr("<b>전역 핫키</b> (다른 창에 있어도 동작 · 수식키 필수)")))
        for action_id, label, default in GLOBAL_ACTIONS:
            if not self.w.action_available(action_id):
                continue
            row = ShortcutRow(action_id, default,
                              self.w.cfg.keys.get(action_id, default))
            row.changed.connect(self._key_changed)
            form.addRow(tr(label), row)

        area.setWidget(inner)
        outer.addWidget(area)

        reset = QPushButton(tr("모든 키를 기본값으로"))
        reset.clicked.connect(self._reset_keys)
        outer.addWidget(reset)
        return page

    # ------------------------------------------------------------- 콜백
    def _set_cell(self, v):
        self.w.cfg.s["cell"] = int(v)
        self.w.resync_size()
        self.w.schedule_save()

    def _pick_color(self, button):
        c = QColorDialog.getColor(QColor(self.w.cfg.s["bg_color"]), self,
                                  tr("배경색"))
        if c.isValid():
            self.w.cfg.s["bg_color"] = c.name()
            button.setText(c.name())
            self.w.apply_style()
            self.w.schedule_save()

    def _set_bg_alpha(self, v):
        self.w.cfg.s["bg_alpha"] = int(v)
        self.w.apply_style()
        self.w.schedule_save()

    def _set_grid_alpha(self, v):
        self.w.cfg.s["grid_alpha"] = int(v)
        self.w.apply_style()
        self.w.schedule_save()

    def _set_flag_restyle(self, key, on):
        self.w.cfg.s[key] = bool(on)
        self.w.apply_style()
        self.w.schedule_save()

    def _set_show_grid(self, on):
        self.w.set_show_grid(bool(on))
        sl = getattr(self, "grid_slider", None)
        if sl is not None:
            sl.setEnabled(bool(on))
            sl.blockSignals(True)
            sl.setValue(int(self.w.cfg.s["grid_alpha"]))
            sl.blockSignals(False)

    def _set_flag(self, key, on):
        self.w.cfg.s[key] = bool(on)
        if key in ("always_on_top", "frameless"):
            self.w.apply_window_mode()
        elif key in ("show_topbar", "show_panel"):
            self.w.resync_size()
        else:
            self.w.apply_style()
        self.w.schedule_save()

    def _set_window_mode(self, value):
        self.w.cfg.s["window_mode"] = value
        self.w.apply_window_mode()
        self.w.schedule_save()

    def _set_title(self, text):
        self.w.cfg.s["disguise_title"] = text
        self.w.apply_window_mode()
        self.w.schedule_save()

    def _set_game(self, key, value):
        self.w.cfg.set_opt(key, value)
        self.w.schedule_save()
        if key == "num_colors":
            # 색 세트는 한 판 동안 고정이다. 아직 아무것도 놓지 않았으면 바로
            # 새 색으로 다시 깔고, 진행 중인 판은 건드리지 않는다.
            g = self.w.game
            if g.score == 0 and g.board_empty():
                g.reset()
            else:
                self.w.flash(tr("색 수는 다음 게임부터 적용됩니다"))
        self.w.board.update()

    def _key_changed(self, action_id, seq_text):
        self.w.cfg.keys[action_id] = seq_text
        self.w.rebuild_keymap()
        if action_id.startswith("g_"):
            self.w.apply_global_hotkeys()
        self.w.schedule_save()

    def _reset_keys(self):
        self.w.cfg.data["keys"] = dict(DEFAULT_KEYS)
        self.w.rebuild_keymap()
        self.w.apply_global_hotkeys()
        self.w.schedule_save()
        self.w.flash(tr("단축키를 기본값으로 되돌렸습니다 — 설정을 다시 열면 반영됩니다"))


# ================================================================ 메인 창
class PuyoWindow(QWidget):
    hotkey_pressed = pyqtSignal(int)

    def __init__(self, cfg):
        super().__init__()
        self.cfg = cfg
        self.paused = False
        self._drag_last = None        # 드래그 중 마지막 전역 좌표
        self._drag_from = None        # 드래그를 시작한 전역 좌표
        self._dragging = False
        self._dialog_open = False
        self._hotkey_failures = []
        self._flash_text = ""

        self.spec = cfg.spec
        self.game = self.spec.engine(cfg.opt)
        self._resumed = self._load_save()   # 껐을 때 풀던 판이 있으면 이어서
        self._save_due = 0.0
        self._over_seen = bool(self.game.over)

        self.setObjectName("puyoRoot")
        self.setWindowTitle(cfg.s["disguise_title"])
        self.setMouseTracking(True)

        # -------------------------------------------------------- 상단바
        self.topbar = QWidget(self)
        self.topbar.setFixedHeight(20)
        self.info_label = QLabel(tr("Esc 숨기기 · Ctrl+Alt+Z 복귀"))
        self.info_label.setMouseTracking(True)
        # QLabel 기본값(LinksAccessibleByMouse)이 마우스를 삼켜 그 위에서는
        # 창을 끌 수 없게 된다. 글자는 읽기용이니 마우스를 통과시킨다.
        self.info_label.setTextInteractionFlags(Qt.NoTextInteraction)
        self.info_label.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        # 라벨의 자연 너비가 창 폭을 끌어올리지 않도록 최소 너비를 직접 준다.
        # 넘치는 글자는 _set_info() 에서 잘라 준다.
        self.info_label.setMinimumWidth(1)
        self.info_label.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)

        self.buttons = {}
        for kind, tip, slot in (
                ("pause", tr("일시정지 / 재개 (P)"), self.toggle_pause),
                ("restart", tr("새 게임 (F2 / R)"), self.new_game),
                ("settings", tr("설정 (F1)"), self.open_settings),
                ("hide", tr("숨기기 (Esc / Ctrl+Alt+Z)"), self.panic_hide)):
            btn = QPushButton()
            btn.setFlat(True)
            btn.setFixedSize(20, 18)
            btn.setToolTip(tip)
            btn.setCursor(Qt.ArrowCursor)
            btn.setFocusPolicy(Qt.NoFocus)
            btn.clicked.connect(slot)
            self.buttons[kind] = btn

        bar = QHBoxLayout(self.topbar)
        bar.setContentsMargins(4, 0, 2, 0)
        bar.setSpacing(1)
        bar.addWidget(self.info_label, 1)
        for kind in ("pause", "restart", "settings", "hide"):
            bar.addWidget(self.buttons[kind])

        # ---------------------------------------------------- 필드 / 패널
        self.board = self.spec.board(self)
        self.next_view = self.spec.side(self) if self.spec.side else None
        self._apply_mouse_mode()
        self.stat_label = QLabel()
        self.stat_label.setAlignment(Qt.AlignLeft | Qt.AlignTop)
        self.stat_label.setMouseTracking(True)
        self.stat_label.setWordWrap(True)
        self.stat_label.setTextFormat(Qt.RichText)
        self.stat_label.setTextInteractionFlags(Qt.NoTextInteraction)
        self.stat_label.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self.stat_label.setMinimumWidth(1)
        # 창 크기는 resync_size() 가 보드 기준으로 정한다. 글자가 몇 줄로
        # 접히든 창을 늘리지 않도록 크기 요구를 아예 내놓지 않게 한다.
        stat_policy = QSizePolicy(QSizePolicy.Ignored, QSizePolicy.Ignored)
        # 줄바꿈 라벨은 '이 폭이면 이만큼 높아야 한다'를 레이아웃에 요구한다.
        # 글자를 새로 넣을 때마다 그 요구가 지연 처리되어, 게임을 갈아끼운 뒤
        # 이미 맞춰 놓은 창 크기를 옛 값으로 되돌린다. 높이는 resync_size 가
        # 보드 기준으로 못박으므로, 라벨은 레이아웃에 아무 요구도 하지 않게 한다.
        stat_policy.setHeightForWidth(False)
        self.stat_label.setSizePolicy(stat_policy)
        self.stat_label.setMinimumSize(1, 0)

        self.panel = QWidget(self)
        self.panel.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        panel_lay = QVBoxLayout(self.panel)
        panel_lay.setContentsMargins(6, 2, 4, 2)
        panel_lay.setSpacing(6)
        self.panel_lay = panel_lay
        if self.next_view is not None:
            panel_lay.addWidget(self.next_view)
        panel_lay.addWidget(self.stat_label, 1)

        mid = QHBoxLayout()
        mid.setContentsMargins(0, 0, 0, 0)
        mid.setSpacing(self.MID_GAP)
        self.mid = mid
        mid.addWidget(self.board)
        mid.addWidget(self.panel)

        root = QVBoxLayout(self)
        root.setContentsMargins(5, 4, 5, 5)
        root.setSpacing(2)
        root.addWidget(self.topbar)
        root.addLayout(mid)

        self.setContextMenuPolicy(Qt.CustomContextMenu)
        self.customContextMenuRequested.connect(
            lambda p: self.show_menu(self.mapToGlobal(p)))

        # -------------------------------------------------------- 타이머
        self.save_timer = QTimer(self)
        self.save_timer.setSingleShot(True)
        self.save_timer.setInterval(1500)
        self.save_timer.timeout.connect(self.save_state)

        self.tick = QTimer(self)
        self.tick.setTimerType(Qt.PreciseTimer)
        self.tick.setInterval(16)
        self.tick.timeout.connect(self._on_tick)
        self._last_ms = 0

        self.hotkeys = HotkeyManager()
        self.rebuild_keymap()
        self._build_tray()

        self.apply_style()
        self.apply_window_mode()
        self.resync_size()
        # 창 크기가 정해진 뒤에 위치를 잡는다 — 화면 밖 좌표는 보정된다
        pos = cfg.s.get("pos") or COMMON_DEFAULTS["pos"]
        self.move(self.sane_pos(pos[0], pos[1]))

        self.hotkey_pressed.connect(self.on_global_hotkey)
        self.tick.start()

    # 게임 조작 동작 — 규칙 객체에 무엇을 시킬지만 적는다. 어느 게임이든
    # 이 이름들을 제공하면 그대로 붙는다. 게임이 실제로 쓰는 것은
    # GameSpec.actions 로 골라진다.
    GAME_VERBS = {
        "left": lambda g: g.move(-1),
        "right": lambda g: g.move(1),
        "soft": lambda g: g.soft_drop(),
        "rot_cw": lambda g: g.rotate(1),
        "rot_cw2": lambda g: g.rotate(1),
        "rot_ccw": lambda g: g.rotate(-1),
        "hard": lambda g: g.hard_drop(),
        "hold": lambda g: g.hold(),
        "rot_180": lambda g: g.rotate_180(),
        # 스도쿠
        "cur_left": lambda g: g.move_cursor(-1, 0),
        "cur_right": lambda g: g.move_cursor(1, 0),
        "cur_up": lambda g: g.move_cursor(0, -1),
        "cur_down": lambda g: g.move_cursor(0, 1),
        "note": lambda g: g.toggle_note_mode(),
        "erase": lambda g: g.erase(),
        "erase2": lambda g: g.erase(),
        "undo": lambda g: g.undo(),
        "hint": lambda g: g.hint(),
        # 지뢰찾기
        "reveal": lambda g: g.reveal(),
        "flag": lambda g: g.flag(),
        "chord": lambda g: g.chord(),
    }
    for _n in range(1, 10):
        GAME_VERBS["num_%d" % _n] = (lambda n: lambda g: g.enter(n))(_n)
    del _n
    # 전역 핫키 쪽 이름 -> 같은 일을 하는 앱 단축키 이름
    GLOBAL_VERB_ALIAS = {"g_left": "left", "g_right": "right", "g_soft": "soft",
                         "g_rot": "rot_cw", "g_hard": "hard", "g_hold": "hold"}

    def retranslate(self):
        """언어를 바꾼 뒤, 한 번 만들어 두고 계속 쓰는 글을 다시 만든다.

        매 틱 새로 그리는 것(판·점수 칸·상단바)은 그냥 두면 알아서 바뀐다.
        트레이 메뉴와 버튼 설명처럼 한 번만 만드는 것들만 손보면 된다.
        """
        for kind, tip in (("pause", "일시정지 / 재개 (P)"),
                          ("restart", "새 게임 (F2 / R)"),
                          ("settings", "설정 (F1)"),
                          ("hide", "숨기기 (Esc / Ctrl+Alt+Z)")):
            if kind in self.buttons:
                self.buttons[kind].setToolTip(tr(tip))
        old = self.tray_menu
        self.tray.setContextMenu(None)
        self._build_tray_menu()
        old.deleteLater()
        self.note_hotkey_status()
        self._update_stats()
        self.board.update()
        if self.next_view is not None:
            self.next_view.update()

    def _apply_mouse_mode(self):
        """필드가 마우스를 받을지 정한다.

        낙하 퍼즐은 필드를 눌러도 창이 끌리는 편이 낫다. 스도쿠처럼 칸을
        눌러야 하는 게임은 그 반대라, 게임이 스스로 필요하다고 밝힌 경우에만
        필드와 옆 위젯에 클릭을 넘긴다. 상단바·점수 칸·창 가장자리는 어느
        쪽이든 그대로 끌 수 있다.
        """
        through = not self.spec.wants_mouse
        self.board.setAttribute(Qt.WA_TransparentForMouseEvents, through)
        if self.next_view is not None:
            self.next_view.setAttribute(Qt.WA_TransparentForMouseEvents, through)

    def action_available(self, action_id):
        """지금 고른 게임에서 쓰이는 동작인가.

        조작 동작(이동·회전·홀드 등)은 게임마다 다르다. 창 조작(숨기기·투명도)
        은 언제나 쓰인다. 설정 창에서 쓰지도 않는 키를 보여 주지 않으려고 쓴다.
        """
        if action_id in self.GAME_VERBS or action_id in self.GLOBAL_VERB_ALIAS:
            return action_id in self.spec.actions
        return True

    # ------------------------------------------------------------- 단축키
    def _handlers(self):
        """동작 id -> 실행할 함수. 앱 단축키와 전역 핫키가 이 표를 함께 쓴다."""
        table = {
            "pause": self.toggle_pause,
            "new_game": self.new_game,
            "restart": self.new_game,
            "hide": self.panic_hide,
            "settings": self.open_settings,
            "menu": lambda: self.show_menu(QCursor.pos()),
            "toggle_bg": self.toggle_bg,
            "toggle_grid": self.toggle_grid,
            "opacity_up": lambda: self.bump_opacity(+0.05),
            "opacity_down": lambda: self.bump_opacity(-0.05),
            "cell_up": lambda: self.bump_cell(+2),
            "cell_down": lambda: self.bump_cell(-2),
            "toggle_top": self.toggle_on_top,
            "toggle_panel": lambda: self.toggle_part("show_panel", tr("사이드 패널")),
            "toggle_topbar": lambda: self.toggle_part("show_topbar", tr("상단바")),
            # 전역 전용
            "g_hide": self.toggle_visible,
            "g_restart": self.new_game,
            "g_bg": self.toggle_bg,
            "g_pause": self.toggle_pause,
            "g_quit": self.quit_app,
        }
        # 지금 게임이 쓰는 조작만 붙인다
        for action_id in self.spec.actions:
            verb = self.GLOBAL_VERB_ALIAS.get(action_id, action_id)
            fn = self.GAME_VERBS.get(verb)
            if fn:
                table[action_id] = (lambda f=fn: f(self.game))
        return table

    # 일시정지 중에도 받아야 하는 동작 — 나머지 조작키는 막는다
    ALWAYS_ON = {"pause", "new_game", "restart", "hide", "settings", "menu",
                 "toggle_bg", "toggle_grid", "opacity_up", "opacity_down",
                 "cell_up",
                 "cell_down", "toggle_top", "toggle_panel", "toggle_topbar",
                 "g_hide", "g_restart", "g_bg", "g_pause", "g_quit"}

    def rebuild_keymap(self):
        """설정에 저장된 키로 '키 조합 -> 동작 id' 표를 다시 만든다.

        QAction 대신 직접 표를 만든다 — 게임 조작은 키 반복(누르고 있기)을
        그대로 받아야 하고, 일시정지 중에 조작키만 막아야 하기 때문이다.
        """
        self.key_map = {}
        for action_id, _label, _default in LOCAL_ACTIONS:
            # 게임마다 같은 키를 다른 일에 쓴다. 예를 들어 ↓ 는 뿌요·테트리스
            # 에서 빠른 낙하지만 스도쿠에서는 칸 커서를 내린다. 지금 게임이
            # 쓰지 않는 동작은 표에 넣지 않아야 키가 겹치지 않는다.
            if not self.action_available(action_id):
                continue
            combo = seq_to_combo(self.cfg.keys.get(action_id) or "")
            if combo is not None:
                self.key_map[combo] = action_id
        self.handlers = self._handlers()

    def key_hint(self, action_id):
        return (self.cfg.keys.get(action_id) or "").strip() or tr("(없음)")

    def keyPressEvent(self, event):
        combo = int(event.modifiers() & MOD_MASK) | int(event.key())
        action_id = self.key_map.get(combo)
        if action_id is None:
            return super().keyPressEvent(event)
        self.run_action(action_id)
        event.accept()

    def run_action(self, action_id):
        if self.paused and action_id not in self.ALWAYS_ON:
            return
        if self.game.over and action_id not in self.ALWAYS_ON:
            return
        fn = self.handlers.get(action_id)
        if fn:
            fn()
            self.board.update()
            if self.next_view is not None:
                self.next_view.update()

    def apply_global_hotkeys(self):
        """전역 핫키를 설정대로 다시 등록하고, 실패한 것을 알려 준다."""
        failed = self.hotkeys.apply(self.cfg.keys)
        self._hotkey_failures = failed
        self.note_hotkey_status()
        if failed:
            self.flash(tr("전역 키 등록 실패: ")
                       + ", ".join(tr(ACTION_LABELS[a]) for a in failed))

    def on_global_hotkey(self, hotkey_id):
        action_id = self.hotkeys.action_for(hotkey_id)
        if action_id:
            self.run_action(action_id)

    # -------------------------------------------------------------- 트레이
    def _make_icon(self):
        pix = QPixmap(32, 32)
        pix.fill(Qt.transparent)
        p = QPainter(pix)
        p.setRenderHint(QPainter.Antialiasing)
        p.setBrush(QColor(self.cfg.s["bg_color"]))
        p.setPen(QColor("#5b6272"))
        p.drawRoundedRect(2, 2, 28, 28, 6, 6)
        f = QFont()
        f.setPointSize(14)
        f.setBold(True)
        p.setFont(f)
        p.setPen(QColor("#c8ced6"))
        p.drawText(QRect(0, 0, 32, 32), Qt.AlignCenter, "T")
        p.end()
        return QIcon(pix)

    def _build_tray(self):
        self.tray = QSystemTrayIcon(self._make_icon(), self)
        self._build_tray_menu()
        self.tray.activated.connect(self._tray_activated)
        self.tray.show()
        self._sync_tray_menu()
        self.note_hotkey_status()

    def _build_tray_menu(self):
        menu = QMenu()
        menu.addAction(tr("보이기 / 숨기기"), self.toggle_visible)

        # 창을 꺼내지 않고도 트레이에서 바로 게임을 고를 수 있게 한다
        self.tray_game_acts = {}
        if len(GAMES) > 1:
            sub = menu.addMenu(tr("게임"))
            for key, spec in GAMES.items():
                act = sub.addAction(tr(spec.label),
                                    lambda checked=False, k=key: self.switch_game(k))
                act.setCheckable(True)
                self.tray_game_acts[key] = act
            self.tray_game_menu = sub
        else:
            self.tray_game_menu = None

        menu.addAction(tr("새 게임 / 재시작"), self.new_game)
        menu.addAction(tr("설정…"), self.open_settings)
        menu.addSeparator()
        menu.addAction(tr("종료"), self.quit_app)
        # 열릴 때마다 지금 게임에 체크를 다시 찍는다 (메뉴는 한 번만 만든다)
        menu.aboutToShow.connect(self._sync_tray_menu)
        menu.setStyleSheet(menu_stylesheet(self.cfg.s["bg_color"]))
        if self.tray_game_menu is not None:
            self.tray_game_menu.setStyleSheet(
                menu_stylesheet(self.cfg.s["bg_color"]))
        self.tray_menu = menu
        self.tray.setContextMenu(menu)

    def _sync_tray_menu(self):
        for key, act in self.tray_game_acts.items():
            act.setChecked(key == self.cfg.game)

    def _tray_activated(self, reason):
        if reason in (QSystemTrayIcon.DoubleClick, QSystemTrayIcon.Trigger):
            self.toggle_visible()

    def note_hotkey_status(self):
        """등록된 전역 키와 실패한 키를 트레이 툴팁에 정리해 보여 준다."""
        rows = [APP_NAME]
        for action_id, label, _default in GLOBAL_ACTIONS:
            seq = (self.cfg.keys.get(action_id) or "").strip()
            if not seq:
                continue
            mark = tr("  (등록 실패)") if action_id in self._hotkey_failures else ""
            rows.append("%s : %s%s" % (seq, label, mark))
        self.tray.setToolTip(chr(10).join(rows))

    # -------------------------------------------------------------- 스타일
    def apply_style(self):
        s = self.cfg.s
        ink = QColor("#e8ecf4")
        dim = mix(QColor(s["bg_color"]), ink, 0.62)
        self.setStyleSheet("#puyoRoot, #puyoRoot * { font-family: '%s'; }" % UI_FONT)
        # 배경이 옅으면 뒤 창이 비쳐 글이 묻힌다. 판의 숫자와 같은 이유로
        # 옆 칸·상단바 글에는 어두운 받침을 깔아 준다.
        lift = faint_strength(s)
        plate = text_plate_css(s["bg_color"], lift)
        # 받침을 깔면 배경색에 묻히지 않게 글자도 또렷한 쪽으로 올린다
        if lift > 0:
            dim = mix(QColor(s["bg_color"]), ink, 0.82)
        self.info_label.setStyleSheet(
            "color: %s; font-size: 10px; %s" % (dim.name(), plate))
        stat_px = max(9, min(13, int(int(s["cell"]) * 0.36)))
        self.stat_label.setStyleSheet("color: %s; font-size: %dpx; %s"
                                      % (ink.name(), stat_px, plate))
        hover = mix(QColor(s["bg_color"]), ink, 0.22)
        for kind, btn in self.buttons.items():
            btn.setStyleSheet(
                "QPushButton { border: none; background: transparent; }"
                "QPushButton:hover { background: %s; border-radius: 3px; }"
                % hover.name())
            # 멈춤 버튼은 지금 상태에 따라 모양이 바뀐다 — 멈춰 있으면 재생
            # 삼각형을 보여 줘야 "누르면 다시 시작한다"가 읽힌다.
            icon = kind
            if kind == "pause":
                icon = "play" if self.paused else "pause"
            btn.setIcon(tool_icon(icon, ink.name()))
            btn.setVisible(bool(s.get({"hide": "btn_hide",
                                       "settings": "btn_settings",
                                       "restart": "btn_restart",
                                       "pause": "btn_pause"}[kind], True)))
        self.setWindowOpacity(float(s["opacity"]))
        if hasattr(self, "tray_menu"):
            sheet = menu_stylesheet(s["bg_color"])
            self.tray_menu.setStyleSheet(sheet)
            if getattr(self, "tray_game_menu", None):
                self.tray_game_menu.setStyleSheet(sheet)
            self.tray.setIcon(self._make_icon())
        self.board.update()
        if self.next_view is not None:
            self.next_view.update()
        self.update()

    def resync_size(self):
        """셀 크기·패널 표시 상태에 맞춰 창을 다시 재단한다.

        창 크기를 sizeHint 에 맡기지 않고 눈에 보이는 부분에서 직접 계산한다.
        sizeHint 는 점수 글자가 몇 줄로 접히는지에 따라 달라지는데, 점수가
        길어지면 창이 내용보다 커지고 아래쪽에 아무것도 그려지지 않는 띠가
        남는다. 그 띠도 창이라서 클릭을 먹어 버린다.
        """
        s = self.cfg.s
        self.topbar.setVisible(bool(s["show_topbar"]))
        self.panel.setVisible(bool(s["show_panel"]))
        self.board.resync()
        side_h = 0
        side_w = 0
        if self.next_view is not None:
            self.next_view.resync()
            side_w = self.next_view.width()
            side_h = self.next_view.height() + self.panel_lay.spacing()
        panel_w = max(74, side_w + 16)
        # 폭만 정해 두면 높이는 레이아웃이 정하는데, 그때 심어 놓은 최소 높이가
        # 게임을 갈아끼운 뒤에도 남아 창이 옛 크기 아래로 줄지 않는다.
        # 패널은 언제나 보드와 같은 높이이므로 둘 다 못박는다.
        self.panel.setFixedSize(panel_w, self.board.height())
        self.stat_label.setFixedWidth(panel_w - 12)
        pm = self.panel_lay.contentsMargins()
        stat_h = self.board.height() - pm.top() - pm.bottom() - side_h
        self.stat_label.setFixedHeight(max(0, stat_h))

        # isVisible() 이 아니라 설정값으로 판단한다 — 창이 아직 화면에 올라오기
        # 전에는 자식이 모두 '안 보임' 이어서 첫 계산이 어긋난다.
        show_panel = bool(s["show_panel"])
        show_topbar = bool(s["show_topbar"])
        lay = self.layout()
        m = lay.contentsMargins()
        gap = lay.spacing()
        width = m.left() + self.board.width() + m.right()
        if show_panel:
            width += self.MID_GAP + panel_w
        height = m.top() + self.board.height() + m.bottom()
        if show_topbar:
            height += self.topbar.height() + gap
        # 레이아웃은 최소 크기를 캐시해 둔다. 게임을 갈아끼워 자식이 작아졌을
        # 때 그 캐시가 남아 있으면, setFixedSize 로 줄여 놓아도 activate() 가
        # 옛 최소 크기로 창을 도로 늘린다. 캐시를 버리고 한계를 푼 뒤 다시 잰다.
        # 레이아웃은 최소 크기를 캐시해 둔다. 게임을 갈아끼워 자식이 작아졌을
        # 때 그 캐시가 남아 있으면, setFixedSize 로 줄여 놓아도 activate() 가
        # 옛 최소 크기로 창을 도로 늘린다. invalidate() 는 그 레이아웃만 비우고
        # 안에 든 레이아웃은 건드리지 않으므로, 중첩된 것까지 모두 비운다.
        for nested in (self.panel_lay, self.mid, lay):
            nested.invalidate()
        self.setFixedSize(width, height)
        lay.activate()
        self.apply_style()

    def paintEvent(self, _event):
        """창 배경을 직접 칠한다 — 알파를 0 으로 두면 그대로 '배경 지우기'."""
        s = self.cfg.s
        bg = QColor(s["bg_color"])
        bg.setAlpha(min(255, int(s["bg_alpha"]) + 40))
        if bg.alpha() <= 40 and int(s["bg_alpha"]) == 0:
            return                          # 완전 투명 — 아무것도 칠하지 않는다
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setPen(Qt.NoPen)
        p.setBrush(bg)
        p.drawRoundedRect(QRectF(0, 0, self.width(), self.height()), 8, 8)
        p.end()

    # ---------------------------------------------------------- 화면 조절
    def set_opacity(self, value, from_dialog=False):
        value = max(0.15, min(1.0, value))
        self.cfg.s["opacity"] = value
        self.setWindowOpacity(value)
        if not from_dialog:
            self.flash(tr("투명도 %d%%") % round(value * 100))
        self.schedule_save()

    def bump_opacity(self, delta):
        self.set_opacity(float(self.cfg.s["opacity"]) + delta)

    def bump_cell(self, delta):
        self.cfg.s["cell"] = max(12, min(48, int(self.cfg.s["cell"]) + delta))
        self.resync_size()
        self.flash(tr("셀 %dpx") % self.cfg.s["cell"])
        self.schedule_save()

    def set_show_grid(self, on):
        """격자선 켜고 끄기.

        진하기를 0 까지 내려 둔 채로 껐다 켜면 켜도 안 보인다. 켤 때는
        최소한 눈에 걸리는 값까지 올려 준다.
        """
        s = self.cfg.s
        s["show_grid"] = bool(on)
        if on and int(s["grid_alpha"]) < 8:
            s["grid_alpha"] = COMMON_DEFAULTS["grid_alpha"]
        self.apply_style()
        self.schedule_save()

    def toggle_grid(self):
        self.set_show_grid(not self.cfg.s.get("show_grid", True))
        self.flash(tr("격자선 ON") if self.cfg.s["show_grid"]
                   else tr("격자선 OFF"))

    def toggle_bg(self):
        """배경 지우기 — 0 과 직전 값을 왕복한다."""
        s = self.cfg.s
        if int(s["bg_alpha"]) > 0:
            self._bg_backup = int(s["bg_alpha"])
            s["bg_alpha"] = 0
            self.flash(tr("배경 지우기 ON"))
        else:
            s["bg_alpha"] = getattr(self, "_bg_backup", COMMON_DEFAULTS["bg_alpha"])
            self.flash(tr("배경 지우기 OFF"))
        self.apply_style()
        self.schedule_save()

    def toggle_part(self, key, label):
        self.cfg.s[key] = not self.cfg.s[key]
        self.resync_size()
        self.flash("%s %s" % (label, "ON" if self.cfg.s[key] else "OFF"))
        self.schedule_save()

    def toggle_on_top(self):
        self.cfg.s["always_on_top"] = not self.cfg.s["always_on_top"]
        self.apply_window_mode()
        self.flash(tr("항상 위 ") + ("ON" if self.cfg.s["always_on_top"] else "OFF"))
        self.schedule_save()

    # --------------------------------------------------------- 창 모드/플래그
    def sane_pos(self, x, y):
        """화면 밖 좌표를 보이는 화면 안으로 되돌린다.

        창 모드를 바꿀 때 HWND 가 새로 만들어지면서 Qt 가 엉뚱한 좌표를 주는
        일이 있고, 모니터를 빼면 저장해 둔 위치가 아무 화면에도 없게 된다.
        그대로 두면 실행해도 창이 보이지 않으므로, 상단바를 잡을 수 있는지를
        기준으로 확인하고 안 되면 주 화면에 다시 앉힌다.
        """
        w = max(80, self.width())
        h = max(80, self.height())
        grab = QPoint(int(x) + w // 2, int(y) + 10)     # 상단바 가운데
        for screen in QApplication.screens():
            if screen.availableGeometry().contains(grab):
                return QPoint(int(x), int(y))
        g = QApplication.primaryScreen().availableGeometry()
        return QPoint(g.x() + max(0, (g.width() - w) // 2),
                      g.y() + max(0, (g.height() - h) // 3))

    def apply_window_mode(self):
        s = self.cfg.s
        flags = Qt.Tool if s["window_mode"] == "hidden" else Qt.Window
        if s["frameless"]:
            flags |= Qt.FramelessWindowHint
        if s["always_on_top"]:
            flags |= Qt.WindowStaysOnTopHint

        pos = self.pos()
        was_visible = self.isVisible()
        self.setWindowFlags(flags)
        # 테두리가 없을 때만 진짜 투명 배경을 쓸 수 있다 (배경 지우기의 전제)
        self.setAttribute(Qt.WA_TranslucentBackground, bool(s["frameless"]))
        self.setWindowTitle(s["disguise_title"] if s["window_mode"] == "disguise"
                            else APP_NAME)
        self.move(self.sane_pos(pos.x(), pos.y()))
        if was_visible:
            self.show()

    # --------------------------------------------------------------- 숨기기
    def panic_hide(self):
        """즉시 숨기기. 진행 중이던 판은 그대로 얼려 둔다."""
        if self.cfg.s["pause_on_hide"]:
            self.paused = True
            self.refresh_pause_button()
        self.save_state()
        self.hide()

    def toggle_visible(self):
        if self.isVisible():
            self.panic_hide()
        else:
            self.show()
            self.raise_()
            self.activateWindow()
            if self.paused:
                self.flash(tr("%s 로 재개") % self.key_hint("pause"))

    def toggle_pause(self):
        if self.game.over:
            return
        self.paused = not self.paused
        self.flash(tr("일시정지") if self.paused else tr("재개"))
        self.refresh_pause_button()
        self.board.update()
        if self.next_view is not None:
            self.next_view.update()

    def refresh_pause_button(self):
        """멈춤 버튼의 모양과 설명을 지금 상태에 맞춘다."""
        btn = self.buttons.get("pause")
        if btn is None:
            return
        btn.setIcon(tool_icon("play" if self.paused else "pause", "#e8ecf4"))
        btn.setToolTip(tr("재개 (P)") if self.paused else tr("일시정지 (P)"))

    def eat_click_while_paused(self, event):
        """멈춰 있는 동안 판을 누르면 수를 두지 않고 다시 시작한다.

        여태 키보드만 막고 마우스는 그냥 통과시켜서, 멈춰 놓고도 스도쿠 숫자가
        써지고 지뢰찾기 칸이 열렸다. 겸사겸사 덮개 전체가 '재개' 버튼이 된다.
        """
        if not self.paused or self.game.over:
            return False
        self.toggle_pause()
        event.accept()
        return True

    def changeEvent(self, event):
        # 포커스를 잃으면 자동으로 숨긴다 — 설정 창을 여는 동안은 예외
        if (event.type() == QEvent.ActivationChange
                and not self.isActiveWindow()
                and self.cfg.s["hide_on_blur"]
                and not self._dialog_open
                and self.isVisible()):
            QTimer.singleShot(0, self.panic_hide)
        super().changeEvent(event)

    # ------------------------------------------------------------ 이어하기
    def _load_save(self):
        """저장해 둔 판을 지금 게임에 되살린다. 없거나 어긋나면 새 판 그대로."""
        if not self.spec.resumable:
            return False
        data = self.cfg.take_save(self.spec.key)
        if not data:
            return False
        try:
            return bool(self.spec.restore(self.game, data))
        except Exception:
            # 저장이 깨졌다고 앱이 안 뜨면 안 된다 — 새 판으로 간다
            return False

    def _stash_save(self):
        """지금 판을 설정에 담아 둔다 (파일로 쓰는 것은 save_state 가 한다)."""
        if not self.spec.resumable:
            return
        try:
            self.cfg.put_save(self.spec.key, self.spec.dump(self.game))
        except Exception:
            self.cfg.put_save(self.spec.key, None)

    # ------------------------------------------------------------- 게임 진행
    def _on_tick(self):
        import time
        now = time.perf_counter() * 1000.0
        if not self._last_ms:
            self._last_ms = now
            return
        dt = min(120.0, now - self._last_ms)     # 창을 오래 숨겼다가 와도 안전
        self._last_ms = now

        if not self.paused and not self.game.over:
            self.game.update(dt)
            # 오래 붙들고 푸는 판은 틈틈이 담아 둔다. 갑자기 꺼져도 몇 초치만
            # 잃는다. 파일 쓰기는 save_timer 가 모아서 한 번만 한다.
            if self.spec.resumable:
                self._save_due -= dt
                if self._save_due <= 0:
                    self._save_due = 10000.0
                    self.schedule_save()

        # 판이 끝나는 순간 기록을 남긴다. 끝나는 계기는 게임마다 다르다 —
        # 낙하 퍼즐은 시간이 흐르다 끝나지만, 스도쿠·지뢰찾기는 누르는 순간
        # 끝난다. 그래서 update() 안에서만 보지 않고, 끝났는지를 매 틱 본다.
        if self.game.over and not self._over_seen:
            self._over_seen = True
            self._record_best()
        elif not self.game.over:
            self._over_seen = False

        if self.isVisible():
            self.board.update()
            if self.next_view is not None:
                self.next_view.update()
            self._update_stats()

    def _record_best(self):
        """이 게임의 기록만 큰 값으로 갱신한다.

        처음 세우는 기록은 견주지 않고 그대로 받는다. 시간 기록은 짧을수록
        좋으라고 음수로 담는데(sudoku_records 참고), 없는 값을 0 으로 치고
        max() 를 걸면 0 이 이겨서 최고 기록이 영영 안 남는다.
        """
        rec = self.cfg.rec
        for name, value in self.spec.records(self.game).items():
            value = int(value)
            rec[name] = value if name not in rec else max(int(rec[name]), value)
        self.save_state()

    def switch_game(self, key):
        """게임을 갈아끼운다 — 창·은폐 계층은 그대로 두고 규칙과 화면만 바꾼다."""
        # 설정이 아니라 지금 창이 들고 있는 게임과 견준다. 설정만 먼저 바뀐
        # 상태에서 부르면, 설정 기준으로는 "이미 그 게임"이라 그냥 돌아가
        # 버려서 규칙 객체와 설정이 어긋난다.
        if key not in GAMES or key == self.spec.key:
            return
        if self.game.score:
            self._record_best()
        self._stash_save()              # 떠나는 게임의 판을 담아 두고
        self.cfg.set_game(key)
        self.spec = self.cfg.spec
        self.game = self.spec.engine(self.cfg.opt)
        self._resumed = self._load_save()   # 돌아온 게임의 판을 되살린다
        self._save_due = 0.0
        self._over_seen = bool(self.game.over)

        self.mid.removeWidget(self.board)
        self.board.setParent(None)
        self.board.deleteLater()
        self.board = self.spec.board(self)
        self.mid.insertWidget(0, self.board)

        if self.next_view is not None:
            self.panel_lay.removeWidget(self.next_view)
            self.next_view.setParent(None)
            self.next_view.deleteLater()
        self.next_view = self.spec.side(self) if self.spec.side else None
        if self.next_view is not None:
            self.panel_lay.insertWidget(0, self.next_view)
        self._apply_mouse_mode()

        # 숨어 있는 동안 트레이에서 바꿨다면 새 판을 얼려 둔다 (새 게임과 같은 규칙)
        self.paused = (not self.isVisible()) and bool(self.cfg.s["pause_on_hide"])
        self.refresh_pause_button()
        self.rebuild_keymap()
        self.resync_size()
        self.flash(tr("이어서 풀기") if self._resumed
                   else tr("%s 시작") % tr(self.spec.label))
        self.schedule_save()

    def new_game(self):
        """빠른 재시작 — 확인 절차 없이 즉시 새 판. 일시정지·게임 오버 중에도 된다."""
        if self.game.score:
            self._record_best()
        self.game.reset()
        # 숨어 있는 동안 전역 키로 재시작했다면 그대로 얼려 둔다
        self.paused = (not self.isVisible()) and bool(self.cfg.s["pause_on_hide"])
        self.refresh_pause_button()
        self.flash(tr("새 게임"))
        self.resync_size()

    def _update_stats(self):
        g = self.game
        if not self.cfg.s["show_panel"] and not self.cfg.s["show_topbar"]:
            return
        # 셀을 작게 줄이면 패널도 얇아진다 — 그때는 간략형을 쓴다
        compact = self.stat_label.height() < 200
        panel_html, info = self.spec.stats(self, g, compact)
        self.stat_label.setText(panel_html)
        if self._flash_text:
            return
        self._set_info(info)

    def _set_info(self, text):
        """상단바가 좁아도 창이 늘어나지 않게 글자를 잘라서 넣는다."""
        used = sum(20 for k, b in self.buttons.items() if b.isVisibleTo(self))
        width = max(24, self.width() - used - 14)
        fm = QFontMetrics(self.info_label.font())
        self.info_label.setText(fm.elidedText(text, Qt.ElideRight, width))
        self.info_label.setToolTip(text)

    def flash(self, message, ms=1400):
        """상단바에 잠깐 메시지를 띄우고 되돌린다."""
        self._flash_text = message
        self._set_info(message)
        QTimer.singleShot(ms, self._clear_flash)

    def _clear_flash(self):
        self._flash_text = ""
        self._update_stats()

    # -------------------------------------------------------- 컨텍스트 메뉴
    def show_menu(self, global_pos):
        s = self.cfg.s
        menu = QMenu(self)
        menu.setStyleSheet(menu_stylesheet(s["bg_color"]))

        menu.addAction(tr("새 게임\t%s / %s") % (self.key_hint("new_game"),
                                          self.key_hint("restart")),
                       self.new_game)
        menu.addAction((tr("재개") if self.paused else tr("일시정지"))
                       + "\t%s" % self.key_hint("pause"), self.toggle_pause)
        if len(GAMES) > 1:
            sub = menu.addMenu(tr("게임"))
            for key, spec in GAMES.items():
                act = sub.addAction(tr(spec.label),
                                    lambda k=key: self.switch_game(k))
                act.setCheckable(True)
                act.setChecked(key == self.cfg.game)
            sub.setStyleSheet(menu_stylesheet(s["bg_color"]))
        menu.addSeparator()

        act_bg = menu.addAction(tr("배경 지우기\t%s") % self.key_hint("toggle_bg"),
                                self.toggle_bg)
        act_bg.setCheckable(True)
        act_bg.setChecked(int(s["bg_alpha"]) == 0)

        act_grid = menu.addAction(tr("격자선	%s") % self.key_hint("toggle_grid"),
                                  self.toggle_grid)
        act_grid.setCheckable(True)
        act_grid.setChecked(bool(s.get("show_grid", True)))

        act_top = menu.addAction(tr("항상 위\t%s") % self.key_hint("toggle_top"),
                                 self.toggle_on_top)
        act_top.setCheckable(True)
        act_top.setChecked(bool(s["always_on_top"]))

        act_panel = menu.addAction(tr("사이드 패널\t%s") % self.key_hint("toggle_panel"),
                                   lambda: self.toggle_part("show_panel", tr("사이드 패널")))
        act_panel.setCheckable(True)
        act_panel.setChecked(bool(s["show_panel"]))

        act_bar = menu.addAction(tr("상단바\t%s") % self.key_hint("toggle_topbar"),
                                 lambda: self.toggle_part("show_topbar", tr("상단바")))
        act_bar.setCheckable(True)
        act_bar.setChecked(bool(s["show_topbar"]))

        menu.addSeparator()
        menu.addAction(tr("설정…\t%s") % self.key_hint("settings"), self.open_settings)
        menu.addAction(tr("숨기기\t%s / %s") % (self.key_hint("hide"),
                                          self.key_hint("g_hide")),
                       self.panic_hide)
        menu.addAction(tr("종료\t%s") % self.key_hint("g_quit"), self.quit_app)
        menu.exec_(global_pos)

    def open_settings(self):
        self._dialog_open = True
        was_paused = self.paused
        self.paused = True
        dlg = SettingsDialog(self)
        style_dialog(dlg)
        dlg.exec_()
        # 설정을 여는 동안만 멈춰 둔다. 그 사이 apply_style 이 돌면 버튼이
        # 재생 모양으로 바뀌어 있으므로, 되돌린 뒤 모양도 맞춰 준다.
        self.paused = was_paused
        self.refresh_pause_button()
        self._dialog_open = False
        self.save_state()
        self.activateWindow()

    MID_GAP = 4                   # 보드와 사이드 패널 사이 간격

    # ------------------------------------------------------- 창 끌어 옮기기
    DRAG_SLOP = 3                 # 이만큼 끌기 전에는 창을 움직이지 않는다

    def mousePressEvent(self, event):
        """본문 어디를 잡아도 창을 옮길 수 있다 (상단바 아이콘은 제외)."""
        if event.button() == Qt.LeftButton:
            self._drag_last = event.globalPos()
            self._drag_from = event.globalPos()
            self._dragging = False
            event.accept()

    def mouseMoveEvent(self, event):
        if self._drag_last is None or not (event.buttons() & Qt.LeftButton):
            return
        if not self._dragging:
            moved = event.globalPos() - self._drag_from
            if abs(moved.x()) < self.DRAG_SLOP and abs(moved.y()) < self.DRAG_SLOP:
                return                        # 손떨림으로 창이 밀리지 않게
            self._dragging = True
        # 절대 좌표 대신 '움직인 만큼'을 더한다. 배율이 다른 모니터로 넘어가며
        # 창이 한 번 튀어도 그 뒤로 계속 어긋나지 않는다.
        delta = event.globalPos() - self._drag_last
        self._drag_last = event.globalPos()
        self.move(self.pos() + delta)
        event.accept()

    def mouseReleaseEvent(self, event):
        if self._drag_last is not None:
            self._drag_last = None
            if self._dragging:
                self._dragging = False
                self.move(self.sane_pos(self.x(), self.y()))
                self.schedule_save()
        super().mouseReleaseEvent(event)

    def wheelEvent(self, event):
        """Ctrl+휠로 창 투명도를 조절한다.

        수식키를 걸어 둔다. 그냥 휠에 걸어 두면 창 위에서 무심코 스크롤한
        것만으로 화면이 사라질 만큼 투명해진다.
        """
        if event.modifiers() & Qt.ControlModifier:
            self.bump_opacity(0.04 if event.angleDelta().y() > 0 else -0.04)
            event.accept()
        else:
            event.ignore()

    # --------------------------------------------------------------- 저장
    def schedule_save(self):
        self.save_timer.start()

    def save_state(self):
        # 화면 밖 좌표는 저장하지 않는다 — 다음 실행에서 창을 못 찾게 된다
        p = self.sane_pos(self.x(), self.y())
        self.cfg.s["pos"] = [p.x(), p.y()]
        self._stash_save()
        self.cfg.save()

    def closeEvent(self, event):
        """닫기 = 숨기기. 종료는 트레이나 Ctrl+Alt+Q 로만."""
        event.ignore()
        self.panic_hide()

    def quit_app(self):
        self._record_best()
        self.save_state()
        self.tray.hide()
        QApplication.quit()


# ==================================================================== main
def main():
    QApplication.setAttribute(Qt.AA_EnableHighDpiScaling, True)
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    force_fusion_style()
    app.setFont(QFont(resolve_ui_font(), 9))

    # ---- 단일 인스턴스: 이미 떠 있으면 그 창을 꺼내고 끝낸다 ----
    probe = QLocalSocket()
    probe.connectToServer(IPC_KEY)
    if probe.waitForConnected(300):
        stream = QDataStream(probe)
        stream.writeQString("show")
        probe.flush()
        probe.waitForBytesWritten(300)
        probe.disconnectFromServer()
        return 0
    QLocalServer.removeServer(IPC_KEY)

    cfg = Config()
    set_language(cfg.s.get("lang", "ko"))
    window = PuyoWindow(cfg)

    server = QLocalServer(app)
    server.listen(IPC_KEY)

    def on_new_connection():
        sock = server.nextPendingConnection()

        def on_ready():
            QDataStream(sock).readQString()
            if not window.isVisible():
                window.toggle_visible()
            else:
                window.raise_()
                window.activateWindow()
            sock.deleteLater()

        sock.readyRead.connect(on_ready)

    server.newConnection.connect(on_new_connection)

    # ---- 전역 핫키 ----
    hotkey_filter = HotkeyFilter(window.hotkey_pressed.emit)
    app.installNativeEventFilter(hotkey_filter)
    window.apply_global_hotkeys()
    app.aboutToQuit.connect(window.hotkeys.clear)

    window.show()
    if window._resumed:
        window.flash(tr("이어서 풀기"))
    return app.exec_()


if __name__ == "__main__":
    sys.exit(main())
