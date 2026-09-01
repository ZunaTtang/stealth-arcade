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
import json
import math
import os
import random
import sys
import tempfile

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
    ("pause",          "일시정지",          "P"),
    ("new_game",       "새 게임",           "F2"),
    ("restart",        "빠른 재시작",       "R"),
    ("hide",           "즉시 숨기기",       "Esc"),
    ("settings",       "설정 열기",         "F1"),
    ("menu",           "메뉴 열기",         "Ctrl+R"),
    ("toggle_bg",      "배경 지우기 토글",  "Ctrl+B"),
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

# 게임이 무엇이든 그대로인 설정. 화면·은폐·창 위치가 여기 들어간다.
COMMON_DEFAULTS = {
    # ---- 화면 ----
    "cell": 30,                   # 셀 한 변 픽셀 — 창 크기가 여기서 결정된다
    "bg_color": "#101418",
    "bg_alpha": 78,               # 0~255. 0 이면 '배경 지우기'
    "grid_alpha": 20,             # 격자선 알파
    "opacity": 0.94,              # 창 전체 투명도
    "always_on_top": True,
    "frameless": True,
    "show_topbar": True,
    "show_panel": True,
    "show_ghost": True,           # 착지 위치 표시
    "btn_hide": True,
    "btn_settings": True,
    "btn_restart": True,
    "window_mode": "hidden",      # hidden | disguise | normal
    "disguise_title": "메모장",
    # ---- 은폐 ----
    "pause_on_hide": True,        # 숨기면 자동 일시정지
    "hide_on_blur": False,        # 포커스를 잃으면 자동으로 숨기기
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
    """

    def __init__(self, key, label, defaults, engine, board, stats,
                 settings_tab, actions, records, side=None):
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
            self.flash("%d 연쇄!" % self.chain)

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
                self.flash("싹쓸이 보너스! +%d, 방해뿌요 +%d"
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
            self.flash("전체 지우기! 다음 공격에 보너스")

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
                self.flash("방해뿌요 %d개" % dropped)
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
        ga = int(s["grid_alpha"])
        if ga:
            line = QColor(255, 255, 255, ga)
            p.setPen(QPen(line, 1))
            for x in range(COLS + 1):
                p.drawLine(int(x * c), int(self.y_of(1)),
                           int(x * c), self.height())
            for row in range(1, ROWS + 1):
                yy = int(self.y_of(row))
                p.drawLine(0, yy, self.width(), yy)
            # 숨은 줄 경계 — 여기 넘어가면 위험하다는 표시
            p.setPen(QPen(QColor(255, 120, 120, min(255, ga * 5)), 1, Qt.DashLine))
            p.drawLine(0, int(self.y_of(1)), self.width(), int(self.y_of(1)))

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
            self._center_text(p, "일시정지", c * 0.72, self.height() * 0.46,
                              QColor("#ffffff"))
            self._center_text(p, self.win.key_hint("pause") + " 로 재개",
                              c * 0.38, self.height() * 0.56, QColor("#c9d1e0"))
        if g.over:
            self._veil(p)
            self._center_text(p, "GAME OVER", c * 0.68, self.height() * 0.40,
                              QColor("#ff8a95"))
            self._center_text(p, "%s점" % format(g.score, ","), c * 0.46,
                              self.height() * 0.50, QColor("#ffffff"))
            self._center_text(p, "%s / %s 다시 시작"
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
                       Qt.AlignLeft | Qt.AlignVCenter, "예고 %d" % g.pending)
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
        colors.addItem("%d색" % n, n)
    current = max(3, min(len(PUYO_COLORS), int(w.cfg.opt("num_colors"))))
    colors.setCurrentIndex(choices.index(current))
    colors.currentIndexChanged.connect(
        lambda i: dlg._set_game("num_colors", colors.itemData(i)))
    form.addRow("뿌요 색 수", colors)

    mode = QComboBox()
    mode.addItem("엔드리스 (혼자 연쇄 연습)", "endless")
    mode.addItem("방해뿌요 (일정 간격으로 방해뿌요가 쏟아진다)", "garbage")
    mode.setCurrentIndex(["endless", "garbage"].index(w.cfg.opt("mode")))
    mode.currentIndexChanged.connect(
        lambda i: dlg._set_game("mode", mode.itemData(i)))
    form.addRow("모드", mode)

    margin = QCheckBox("마진 타임 (시간이 지나면 상쇄가 어려워진다)")
    margin.setChecked(bool(w.cfg.opt("margin_time")))
    margin.toggled.connect(lambda on: dlg._set_game("margin_time", bool(on)))
    form.addRow(margin)

    note = QLabel(
        "6×12 필드 + 숨은 13번째 줄, 3열이 막히면 게임 오버.\n"
        "본가 점수식(연쇄·색·덩어리 보너스), 퀵턴·벽 밀기·바닥 밀기,\n"
        "싹쓸이는 다음 공격에 얹히고, 방해뿌요 상쇄와 마진 타임을 따른다.")
    note.setWordWrap(True)
    form.addRow("규칙", note)
    return page


def puyo_stats(win, g, compact):
    """사이드 패널 글과 상단바 글. (패널 HTML, 상단바 글) 을 돌려준다."""
    mode = "방해뿌요" if win.cfg.opt("mode") == "garbage" else "엔드리스"
    # 싹쓸이 보너스는 다음 공격에 나가므로 대기 중임을 계속 보여 준다
    zen = ("<br><br><span style='color:#ffe066'>싹쓸이 대기<br>"
           "+%d</span>" % ALL_CLEAR_BONUS) if g.zenkeshi else ""
    info = "%s점 · %d연쇄" % (format(g.score, ","), g.chain)
    if compact:
        return ("<b>%s</b><br>연쇄 <b>%d</b><br>Lv <b>%d</b>%s"
                % (format(g.score, ","), g.chain, g.level(), zen), info)
    return ("점수<br><b>%s</b>"
            "<br><br>연쇄 <b>%d</b>"
            "<br>최고연쇄 <b>%d</b>"
            "<br>레벨 <b>%d</b>"
            "<br><br>최고점수<br><b>%s</b>"
            "<br><br>%s<br>전송 <b>%d</b><br>예고 <b>%d</b>%s"
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

        clear = QPushButton("해제")
        clear.setToolTip("이 동작에 키를 지정하지 않음")
        clear.setFixedWidth(42)
        clear.setFocusPolicy(Qt.NoFocus)
        clear.clicked.connect(lambda: self.set_seq(""))

        reset = QPushButton("기본")
        reset.setToolTip("기본값 %s 으로" % (default_seq or "없음"))
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
        self.setWindowTitle("설정")
        self.setMinimumSize(520, 560)

        tabs = QTabWidget()
        tabs.addTab(self._screen_tab(), "화면")
        tabs.addTab(self._game_tab(), "게임")
        tabs.addTab(self._keys_tab(), "단축키")

        buttons = QDialogButtonBox(QDialogButtonBox.Close)
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
        form.addRow("셀 크기", cell)

        color = QPushButton(s["bg_color"])
        color.clicked.connect(lambda: self._pick_color(color))
        form.addRow("배경색", color)

        self.bg_slider = QSlider(Qt.Horizontal)
        self.bg_slider.setRange(0, 255)
        self.bg_slider.setValue(int(s["bg_alpha"]))
        self.bg_slider.valueChanged.connect(self._set_bg_alpha)
        form.addRow("배경 진하기 (0 = 배경 지우기)", self.bg_slider)

        grid = QSlider(Qt.Horizontal)
        grid.setRange(0, 90)
        grid.setValue(int(s["grid_alpha"]))
        grid.valueChanged.connect(self._set_grid_alpha)
        form.addRow("격자선", grid)

        self.op_slider = QSlider(Qt.Horizontal)
        self.op_slider.setRange(15, 100)
        self.op_slider.setValue(int(float(s["opacity"]) * 100))
        self.op_slider.valueChanged.connect(
            lambda v: self.w.set_opacity(v / 100.0, from_dialog=True))
        form.addRow("창 투명도", self.op_slider)

        for key, label in (("always_on_top", "항상 위"),
                           ("frameless", "테두리 없음"),
                           ("show_topbar", "상단바 표시"),
                           ("show_panel", "사이드 패널 표시"),
                           ("show_ghost", "착지 위치 표시")):
            chk = QCheckBox(label)
            chk.setChecked(bool(s[key]))
            chk.toggled.connect(lambda on, k=key: self._set_flag(k, on))
            form.addRow(chk)

        icons = QHBoxLayout()
        holder = QWidget()
        holder.setLayout(icons)
        for key, label in (("btn_hide", "숨기기"), ("btn_settings", "설정"),
                           ("btn_restart", "재시작")):
            chk = QCheckBox(label)
            chk.setChecked(bool(s[key]))
            chk.toggled.connect(lambda on, k=key: self._set_flag(k, on))
            icons.addWidget(chk)
        form.addRow(QLabel("상단바 아이콘"))
        form.addRow(holder)

        mode = QComboBox()
        mode.addItem("숨은 창 (작업 표시줄·Alt+Tab 에서 제외)", "hidden")
        mode.addItem("위장 창 (다른 제목으로 표시)", "disguise")
        mode.addItem("보통 창", "normal")
        mode.setCurrentIndex(["hidden", "disguise", "normal"].index(
            s["window_mode"]))
        mode.currentIndexChanged.connect(
            lambda i: self._set_window_mode(mode.itemData(i)))
        form.addRow("창 모드", mode)

        title = QLineEdit(s["disguise_title"])
        title.textChanged.connect(self._set_title)
        form.addRow("위장 제목", title)

        blur = QCheckBox("포커스를 잃으면 자동으로 숨기기")
        blur.setChecked(bool(s["hide_on_blur"]))
        blur.toggled.connect(lambda on: self._set_flag("hide_on_blur", on))
        form.addRow(blur)

        pause = QCheckBox("숨길 때 자동 일시정지")
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
            picker.addItem(GAMES[k].label, k)
        picker.setCurrentIndex(keys.index(self.w.cfg.game))
        picker.setEnabled(len(keys) > 1)
        picker.currentIndexChanged.connect(
            lambda i: self._pick_game(picker.itemData(i)))
        form.addRow("게임", picker)

        speed = QDoubleSpinBox()
        speed.setRange(0.3, 3.0)
        speed.setSingleStep(0.1)
        speed.setValue(float(self.w.cfg.s["speed"]))
        speed.setSuffix(" ×")
        speed.valueChanged.connect(lambda v: self._set_game("speed", v))
        form.addRow("낙하 속도", speed)

        outer.addWidget(head)
        outer.addWidget(self.w.cfg.spec.settings_tab(self), 1)
        return page

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

        form.addRow(QLabel("<b>앱 단축키</b> (창이 활성일 때)"))
        for action_id, label, default in LOCAL_ACTIONS:
            if not self.w.action_available(action_id):
                continue          # 지금 게임에 없는 조작은 보여 주지 않는다
            row = ShortcutRow(action_id, default,
                              self.w.cfg.keys.get(action_id, default))
            row.changed.connect(self._key_changed)
            form.addRow(label, row)

        form.addRow(QLabel("<b>전역 핫키</b> (다른 창에 있어도 동작 · 수식키 필수)"))
        for action_id, label, default in GLOBAL_ACTIONS:
            if not self.w.action_available(action_id):
                continue
            row = ShortcutRow(action_id, default,
                              self.w.cfg.keys.get(action_id, default))
            row.changed.connect(self._key_changed)
            form.addRow(label, row)

        area.setWidget(inner)
        outer.addWidget(area)

        reset = QPushButton("모든 키를 기본값으로")
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
                                  "배경색")
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
                self.w.flash("색 수는 다음 게임부터 적용됩니다")
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
        self.w.flash("단축키를 기본값으로 되돌렸습니다 — 설정을 다시 열면 반영됩니다")


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

        self.setObjectName("puyoRoot")
        self.setWindowTitle(cfg.s["disguise_title"])
        self.setMouseTracking(True)

        # -------------------------------------------------------- 상단바
        self.topbar = QWidget(self)
        self.topbar.setFixedHeight(20)
        self.info_label = QLabel("Esc 숨기기 · Ctrl+Alt+Z 복귀")
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
                ("restart", "새 게임 (F2 / R)", self.new_game),
                ("settings", "설정 (F1)", self.open_settings),
                ("hide", "숨기기 (Esc / Ctrl+Alt+Z)", self.panic_hide)):
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
        for kind in ("restart", "settings", "hide"):
            bar.addWidget(self.buttons[kind])

        # ---------------------------------------------------- 필드 / 패널
        self.board = self.spec.board(self)
        self.next_view = self.spec.side(self) if self.spec.side else None
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
    }
    # 전역 핫키 쪽 이름 -> 같은 일을 하는 앱 단축키 이름
    GLOBAL_VERB_ALIAS = {"g_left": "left", "g_right": "right", "g_soft": "soft",
                         "g_rot": "rot_cw", "g_hard": "hard", "g_hold": "hold"}

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
            "opacity_up": lambda: self.bump_opacity(+0.05),
            "opacity_down": lambda: self.bump_opacity(-0.05),
            "cell_up": lambda: self.bump_cell(+2),
            "cell_down": lambda: self.bump_cell(-2),
            "toggle_top": self.toggle_on_top,
            "toggle_panel": lambda: self.toggle_part("show_panel", "사이드 패널"),
            "toggle_topbar": lambda: self.toggle_part("show_topbar", "상단바"),
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
                 "toggle_bg", "opacity_up", "opacity_down", "cell_up",
                 "cell_down", "toggle_top", "toggle_panel", "toggle_topbar",
                 "g_hide", "g_restart", "g_bg", "g_pause", "g_quit"}

    def rebuild_keymap(self):
        """설정에 저장된 키로 '키 조합 -> 동작 id' 표를 다시 만든다.

        QAction 대신 직접 표를 만든다 — 게임 조작은 키 반복(누르고 있기)을
        그대로 받아야 하고, 일시정지 중에 조작키만 막아야 하기 때문이다.
        """
        self.key_map = {}
        for action_id, _label, _default in LOCAL_ACTIONS:
            combo = seq_to_combo(self.cfg.keys.get(action_id) or "")
            if combo is not None:
                self.key_map[combo] = action_id
        self.handlers = self._handlers()

    def key_hint(self, action_id):
        return (self.cfg.keys.get(action_id) or "").strip() or "(없음)"

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
            self.flash("전역 키 등록 실패: "
                       + ", ".join(ACTION_LABELS[a] for a in failed))

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
        menu = QMenu()
        menu.addAction("보이기 / 숨기기", self.toggle_visible)
        menu.addAction("새 게임 / 재시작", self.new_game)
        menu.addAction("설정…", self.open_settings)
        menu.addSeparator()
        menu.addAction("종료", self.quit_app)
        menu.setStyleSheet(menu_stylesheet(self.cfg.s["bg_color"]))
        self.tray_menu = menu
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(self._tray_activated)
        self.tray.show()
        self.note_hotkey_status()

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
            mark = "  (등록 실패)" if action_id in self._hotkey_failures else ""
            rows.append("%s : %s%s" % (seq, label, mark))
        self.tray.setToolTip(chr(10).join(rows))

    # -------------------------------------------------------------- 스타일
    def apply_style(self):
        s = self.cfg.s
        ink = QColor("#e8ecf4")
        dim = mix(QColor(s["bg_color"]), ink, 0.62)
        self.setStyleSheet("#puyoRoot, #puyoRoot * { font-family: '%s'; }" % UI_FONT)
        self.info_label.setStyleSheet(
            "color: %s; font-size: 10px;" % dim.name())
        stat_px = max(9, min(13, int(int(s["cell"]) * 0.36)))
        self.stat_label.setStyleSheet("color: %s; font-size: %dpx;"
                                      % (ink.name(), stat_px))
        hover = mix(QColor(s["bg_color"]), ink, 0.22)
        for kind, btn in self.buttons.items():
            btn.setStyleSheet(
                "QPushButton { border: none; background: transparent; }"
                "QPushButton:hover { background: %s; border-radius: 3px; }"
                % hover.name())
            btn.setIcon(tool_icon(kind, ink.name()))
            btn.setVisible(bool(s.get({"hide": "btn_hide",
                                       "settings": "btn_settings",
                                       "restart": "btn_restart"}[kind], True)))
        self.setWindowOpacity(float(s["opacity"]))
        if hasattr(self, "tray_menu"):
            self.tray_menu.setStyleSheet(menu_stylesheet(s["bg_color"]))
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
            self.flash("투명도 %d%%" % round(value * 100))
        self.schedule_save()

    def bump_opacity(self, delta):
        self.set_opacity(float(self.cfg.s["opacity"]) + delta)

    def bump_cell(self, delta):
        self.cfg.s["cell"] = max(12, min(48, int(self.cfg.s["cell"]) + delta))
        self.resync_size()
        self.flash("셀 %dpx" % self.cfg.s["cell"])
        self.schedule_save()

    def toggle_bg(self):
        """배경 지우기 — 0 과 직전 값을 왕복한다."""
        s = self.cfg.s
        if int(s["bg_alpha"]) > 0:
            self._bg_backup = int(s["bg_alpha"])
            s["bg_alpha"] = 0
            self.flash("배경 지우기 ON")
        else:
            s["bg_alpha"] = getattr(self, "_bg_backup", COMMON_DEFAULTS["bg_alpha"])
            self.flash("배경 지우기 OFF")
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
        self.flash("항상 위 " + ("ON" if self.cfg.s["always_on_top"] else "OFF"))
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
                self.flash("%s 로 재개" % self.key_hint("pause"))

    def toggle_pause(self):
        if self.game.over:
            return
        self.paused = not self.paused
        self.flash("일시정지" if self.paused else "재개")
        self.board.update()

    def changeEvent(self, event):
        # 포커스를 잃으면 자동으로 숨긴다 — 설정 창을 여는 동안은 예외
        if (event.type() == QEvent.ActivationChange
                and not self.isActiveWindow()
                and self.cfg.s["hide_on_blur"]
                and not self._dialog_open
                and self.isVisible()):
            QTimer.singleShot(0, self.panic_hide)
        super().changeEvent(event)

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
            before_over = self.game.over
            self.game.update(dt)
            if self.game.over and not before_over:
                self._record_best()
        if self.isVisible():
            self.board.update()
            if self.next_view is not None:
                self.next_view.update()
            self._update_stats()

    def _record_best(self):
        """이 게임의 기록만 큰 값으로 갱신한다."""
        rec = self.cfg.rec
        for name, value in self.spec.records(self.game).items():
            rec[name] = max(int(rec.get(name, 0)), int(value))
        self.save_state()

    def switch_game(self, key):
        """게임을 갈아끼운다 — 창·은폐 계층은 그대로 두고 규칙과 화면만 바꾼다."""
        if key not in GAMES or key == self.cfg.game:
            return
        if self.game.score:
            self._record_best()
        self.cfg.set_game(key)
        self.spec = self.cfg.spec
        self.game = self.spec.engine(self.cfg.opt)

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

        self.paused = False
        self.rebuild_keymap()
        self.resync_size()
        self.flash("%s 시작" % self.spec.label)
        self.schedule_save()

    def new_game(self):
        """빠른 재시작 — 확인 절차 없이 즉시 새 판. 일시정지·게임 오버 중에도 된다."""
        if self.game.score:
            self._record_best()
        self.game.reset()
        # 숨어 있는 동안 전역 키로 재시작했다면 그대로 얼려 둔다
        self.paused = (not self.isVisible()) and bool(self.cfg.s["pause_on_hide"])
        self.flash("새 게임")
        self.board.update()

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

        menu.addAction("새 게임\t%s / %s" % (self.key_hint("new_game"),
                                          self.key_hint("restart")),
                       self.new_game)
        menu.addAction(("재개" if self.paused else "일시정지")
                       + "\t%s" % self.key_hint("pause"), self.toggle_pause)
        if len(GAMES) > 1:
            sub = menu.addMenu("게임")
            for key, spec in GAMES.items():
                act = sub.addAction(spec.label,
                                    lambda k=key: self.switch_game(k))
                act.setCheckable(True)
                act.setChecked(key == self.cfg.game)
            sub.setStyleSheet(menu_stylesheet(s["bg_color"]))
        menu.addSeparator()

        act_bg = menu.addAction("배경 지우기\t%s" % self.key_hint("toggle_bg"),
                                self.toggle_bg)
        act_bg.setCheckable(True)
        act_bg.setChecked(int(s["bg_alpha"]) == 0)

        act_top = menu.addAction("항상 위\t%s" % self.key_hint("toggle_top"),
                                 self.toggle_on_top)
        act_top.setCheckable(True)
        act_top.setChecked(bool(s["always_on_top"]))

        act_panel = menu.addAction("사이드 패널\t%s" % self.key_hint("toggle_panel"),
                                   lambda: self.toggle_part("show_panel", "사이드 패널"))
        act_panel.setCheckable(True)
        act_panel.setChecked(bool(s["show_panel"]))

        act_bar = menu.addAction("상단바\t%s" % self.key_hint("toggle_topbar"),
                                 lambda: self.toggle_part("show_topbar", "상단바"))
        act_bar.setCheckable(True)
        act_bar.setChecked(bool(s["show_topbar"]))

        menu.addSeparator()
        menu.addAction("설정…\t%s" % self.key_hint("settings"), self.open_settings)
        menu.addAction("숨기기\t%s / %s" % (self.key_hint("hide"),
                                          self.key_hint("g_hide")),
                       self.panic_hide)
        menu.addAction("종료\t%s" % self.key_hint("g_quit"), self.quit_app)
        menu.exec_(global_pos)

    def open_settings(self):
        self._dialog_open = True
        was_paused = self.paused
        self.paused = True
        dlg = SettingsDialog(self)
        style_dialog(dlg)
        dlg.exec_()
        self.paused = was_paused
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
    return app.exec_()


if __name__ == "__main__":
    sys.exit(main())
