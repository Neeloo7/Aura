#!/usr/bin/env python3
"""
gui.py - Ai-ru HUD v4 (PyQt6): Dust Orb + neon aurora, faceted (cut-corner) shape language.

Backend contract (unchanged, thread-safe):
    update_status(state: str)            IDLE | LISTENING | THINKING | SPEAKING
    add_message(sender, text)            "user"/"you" render right, anything else renders as Ai-ru (left)
    set_callbacks(on_submit, on_mic, on_new_session=None)

Optional extras:
    set_audio_level(0..1)    real mic / TTS RMS; drives the wave line and the orb
    add_thought(text)        a real thought / tool-call line shown under the orb
    notify(text)             pops a toast on the floating orb (and the tray) while you are away
    wake()                   call this when your wake-word detector hears "Ai-ru": turns the mic on
    start()                  show the window, or go straight to the background when launched with --background

Keys: Ctrl+K palette | F11 fullscreen | F2 presence | Ctrl+B slide sidebar | Ctrl+T theme
      Ctrl+Space / Ctrl+M mic | Ctrl+N new session | Ctrl+E export | Ctrl+Shift+S sound
      Ctrl+Shift+O orb mode | Ctrl+Shift+P party | Ctrl+Q quit

Files next to gui.py:
    assets/orb.html, orb.css, orb.js, dust-orb.js     the orb page (Dust Orb recoloured, aurora behind it)
    assets/vendor/three-r180.js                        Three.js, copied from your dust-orb package
    assets/fonts/*.ttf                                 optional: any fonts dropped here are loaded automatically

Run directly for a demo:   python gui.py
Requires: PyQt6, PyQt6-WebEngine, numpy
"""
from __future__ import annotations

import colorsys
import ctypes
import html
import inspect
import json
import math
import os
import random
import shutil
import sys
import tempfile
import time
import traceback
import wave
from pathlib import Path
from brain import Brain
from os_tools import execute_tool
import numpy as np

# Chromium flags must be set before Qt creates the web engine.
os.environ.setdefault("QTWEBENGINE_CHROMIUM_FLAGS", "--ignore-gpu-blocklist --enable-gpu-rasterization")

if sys.platform == "win32":
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("airu.orb.hud.app")
    except Exception:
        pass

from PyQt6.QtCore import (QCoreApplication, QEasingCurve, QEvent, QPointF, QPropertyAnimation, QRect, QRectF, Qt,
                          QTimer, QUrl, QVariantAnimation, pyqtSignal, QThread)
from PyQt6.QtGui import (QColor, QFont, QFontDatabase, QFontMetrics, QIcon, QKeySequence, QLinearGradient, QPainter,
                         QPainterPath, QPen, QPixmap, QPolygonF, QRadialGradient, QRegion, QShortcut, QTextOption)
from PyQt6.QtWidgets import (QAbstractButton, QApplication, QFileDialog, QFrame, QGraphicsOpacityEffect, QHBoxLayout,
                             QLabel, QLineEdit, QMenu, QScrollArea, QSizeGrip, QSizePolicy, QSystemTrayIcon,
                             QVBoxLayout, QWidget)

try:
    from PyQt6.QtMultimedia import QSoundEffect
    HAVE_MM = True
except Exception:  # pragma: no cover
    HAVE_MM = False

try:
    if QCoreApplication.instance() is None:
        QCoreApplication.setAttribute(Qt.ApplicationAttribute.AA_ShareOpenGLContexts)
    from PyQt6.QtWebEngineCore import QWebEnginePage, QWebEngineProfile
    from PyQt6.QtWebEngineWidgets import QWebEngineView
    HAVE_WEB = True
except Exception:  # pragma: no cover
    HAVE_WEB = False
    QWebEnginePage = object

APP_NAME = "Ai-ru"
TAU = math.tau
HOME = Path.home()
ASSETS = Path(__file__).resolve().parent / "assets"
DATA_DIR = HOME / ".airu"
if not DATA_DIR.exists() and (HOME / ".aura").exists():  # migrate old Aura data once
    try:
        shutil.copytree(HOME / ".aura", DATA_DIR)
    except Exception:
        pass
SESSIONS_FILE = DATA_DIR / "sessions.json"
SETTINGS_FILE = DATA_DIR / "settings.json"

ADD = QPainter.CompositionMode.CompositionMode_Plus
SRC = QPainter.CompositionMode.CompositionMode_SourceOver
NOPEN = Qt.PenStyle.NoPen
NOBRUSH = Qt.BrushStyle.NoBrush
ALIGN_C = Qt.AlignmentFlag.AlignCenter
ALIGN_VL = Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft
ALIGN_VR = Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight

# --------------------------------------------------------------------------------------
# States and themes
# --------------------------------------------------------------------------------------
STATE_DEFS = {
    "IDLE":      dict(c1=(26, 170, 255), c2=(0, 76, 210),   speed=0.22, hz=0.16, energy=0.30, label="present"),
    "LISTENING": dict(c1=(255, 72, 168), c2=(255, 150, 60), speed=0.55, hz=0.90, energy=0.75, label="listening"),
    "THINKING":  dict(c1=(150, 104, 255), c2=(70, 40, 230), speed=2.60, hz=2.20, energy=0.95, label="thinking"),
    "SPEAKING":  dict(c1=(48, 242, 172), c2=(0, 168, 124),  speed=0.80, hz=1.40, energy=0.62, label="speaking"),
}
ORB_STATE = {"IDLE": "idle", "LISTENING": "listening", "THINKING": "thinking", "SPEAKING": "speaking"}
ALIASES = {
    "READY": "IDLE", "STANDBY": "IDLE", "SLEEPING": "IDLE", "SLEEP": "IDLE",
    "LISTEN": "LISTENING", "RECORDING": "LISTENING",
    "THINK": "THINKING", "PROCESSING": "THINKING", "BUSY": "THINKING", "WORKING": "THINKING",
    "SPEAK": "SPEAKING", "TALKING": "SPEAKING", "RESPONDING": "SPEAKING",
}
THEMES = {  # recolor IDLE (orb, aurora, accents); the other states keep their meaning
    "Neon":     ((26, 170, 255),  (0, 76, 210)),
    "Cyan":     ((0, 229, 255),   (0, 110, 190)),
    "Samantha": ((255, 112, 98),  (196, 52, 84)),
    "Ice":      ((196, 226, 255), (96, 136, 214)),
    "Void":     ((150, 160, 255), (74, 62, 204)),
    "Mono":     ((236, 238, 244), (110, 114, 140)),
}
THEME_ORDER = list(THEMES)
ORB_MODES = ("background", "always", "minimized")
ORB_MODE_TEXT = {
    "background": "Orb: shows whenever Ai-ru is in the background",
    "always": "Orb: always on",
    "minimized": "Orb: only when minimized or closed",
}
# Bahnschrift (built into Windows 10/11) is a DIN-style face: steady and dependable. Drop any .ttf in
# assets/fonts and put its family name first in this list to swap it.
FONT_FAMILIES = ["Bahnschrift", "Sora", "Segoe UI Variable Display", "Segoe UI", "Inter", "Helvetica Neue",
                 "Noto Sans", "Sans Serif"]
POKE_QUIPS = ["hehe, that tickles", "boop.", "careful, I'm ticklish", "I felt that", "do that again. I dare you.",
              "ow! ...kidding", "you know I'm made of dust, right?", "yes? hello?"]
IDLE_QUIPS = ["still here when you need me", "drifting quietly", "humming softly", "poke me, I dare you",
              "thinking about nothing in particular", "all calm"]


def normalize_state(s) -> str:
    key = ALIASES.get(str(s).strip().upper(), str(s).strip().upper())
    return key if key in STATE_DEFS else "IDLE"


def is_user(sender) -> bool:
    return str(sender).strip().lower() in {"user", "you", "me", "human"}


def is_ai(sender) -> bool:
    return str(sender).strip().lower() in {"aura", "ai-ru", "airu", "ai_ru", "assistant", "ai", "bot"}


# --------------------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------------------
def clamp(v, lo=0.0, hi=1.0):
    return lo if v < lo else hi if v > hi else v


def approach(cur, tgt, rate, dt):
    return cur + (tgt - cur) * (1.0 - math.exp(-rate * dt))


def rgba(c, a=255):
    return QColor(int(clamp(c[0], 0, 255)), int(clamp(c[1], 0, 255)), int(clamp(c[2], 0, 255)), int(clamp(a, 0, 255)))


def lighten(c, k):
    return tuple(v + (255 - v) * k for v in c)


def mix(a, b, k):
    return tuple(a[i] + (b[i] - a[i]) * k for i in range(3))


def hexstr(c):
    return "#%02x%02x%02x" % tuple(int(clamp(v, 0, 255)) for v in c)


def hue_shift(c, deg):
    h, s, v = colorsys.rgb_to_hsv(c[0] / 255.0, c[1] / 255.0, c[2] / 255.0)
    r, g, b = colorsys.hsv_to_rgb((h + deg / 360.0) % 1.0, max(s, 0.7), max(v, 0.85))
    return (r * 255.0, g * 255.0, b * 255.0)


def pen_of(col, a, w):
    pn = QPen(rgba(col, a), w)
    pn.setCapStyle(Qt.PenCapStyle.RoundCap)
    pn.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    return pn


def make_font(size, weight=QFont.Weight.Normal, spacing=0.0, italic=False):
    f = QFont()
    f.setFamilies(FONT_FAMILIES)
    f.setPointSizeF(size)
    f.setWeight(weight)
    f.setItalic(italic)
    if spacing:
        f.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, spacing)
    return f


def load_custom_fonts():
    d = ASSETS / "fonts"
    if d.is_dir():
        for f in list(d.glob("*.ttf")) + list(d.glob("*.otf")):
            QFontDatabase.addApplicationFont(str(f))


def safe_call(cb, *args):
    """Call a backend callback tolerant of arity differences; never let it kill the GUI."""
    if cb is None:
        return
    try:
        try:
            inspect.signature(cb).bind(*args)
            call_args = args
        except TypeError:
            call_args = ()
        except ValueError:
            call_args = args
        cb(*call_args)
    except Exception:
        traceback.print_exc()


def add_action(menu, text, fn):
    a = menu.addAction(text)
    a.triggered.connect(lambda _=False: fn())
    return a


def fmt_ts(ts):
    lt, now = time.localtime(ts), time.localtime()
    return time.strftime("Today %H:%M", lt) if lt[:3] == now[:3] else time.strftime("%d %b %H:%M", lt)


def greeting():
    h = time.localtime().tm_hour
    return "Good morning" if h < 12 else "Good afternoon" if h < 18 else "Good evening"


def build_qss(accent):
    qss = """
    QToolTip{background:#04070d;color:#bfe3ff;border:1px solid rgba(ACC,90);padding:5px 9px;}
    QLabel{background:transparent;}
    QLabel#sessionTitle{color:#7f9bb5;}
    QLabel#sideLabel{color:#4b6580;}
    QLabel#sideFoot{color:#364c63;}
    QLabel#sender{color:rgba(ACC,200);}
    QLabel#body{color:#d6e8f7;}
    QLabel#hint{color:#4a6580;}
    QLabel#liveCaption{color:#cfe4f7;}
    QLineEdit#entry{background:transparent;border:none;color:#eaf5ff;selection-background-color:rgba(ACC,90);}
    QLineEdit#paletteEntry{background:transparent;border:none;color:#f2f9ff;selection-background-color:rgba(ACC,90);}
    QLineEdit#orbEntry{background:transparent;border:none;color:#eaf5ff;selection-background-color:rgba(ACC,90);}
    QScrollArea{background:transparent;border:none;}
    #chatInner,#chronInner{background:transparent;}
    QScrollBar:vertical{background:transparent;width:8px;margin:2px;}
    QScrollBar::handle:vertical{background:rgba(ACC,45);border-radius:1px;min-height:30px;}
    QScrollBar::handle:vertical:hover{background:rgba(ACC,110);}
    QScrollBar::add-line:vertical,QScrollBar::sub-line:vertical{height:0;}
    QScrollBar::add-page:vertical,QScrollBar::sub-page:vertical{background:transparent;}
    QMenu{background:#04070d;color:#cfe3f5;border:1px solid rgba(ACC,70);padding:6px;border-radius:3px;}
    QMenu::item{padding:7px 24px;border-radius:2px;}
    QMenu::item:selected{background:rgba(ACC,40);}
    QMenu::separator{height:1px;background:rgba(ACC,30);margin:5px 8px;}
    """
    return qss.replace("ACC", f"{int(accent[0])},{int(accent[1])},{int(accent[2])}")


# --------------------------------------------------------------------------------------
# Shape language: cut corners and hexagons instead of circles and pills
# --------------------------------------------------------------------------------------
def chamfer(r, tl=0.0, tr=0.0, br=0.0, bl=0.0):
    """Rectangle with diagonal cuts on any corner."""
    p = QPainterPath()
    p.moveTo(r.left() + tl, r.top())
    p.lineTo(r.right() - tr, r.top())
    p.lineTo(r.right(), r.top() + tr)
    p.lineTo(r.right(), r.bottom() - br)
    p.lineTo(r.right() - br, r.bottom())
    p.lineTo(r.left() + bl, r.bottom())
    p.lineTo(r.left(), r.bottom() - bl)
    p.lineTo(r.left(), r.top() + tl)
    p.closeSubpath()
    return p


def hex_path(cx, cy, r, rot=-math.pi / 2):
    path = QPainterPath()
    for i in range(6):
        a = rot + i * TAU / 6
        pt = QPointF(cx + r * math.cos(a), cy + r * math.sin(a))
        path.moveTo(pt) if i == 0 else path.lineTo(pt)
    path.closeSubpath()
    return path


def diamond(r):
    return QPolygonF([QPointF(0, -r), QPointF(r, 0), QPointF(0, r), QPointF(-r, 0)])


def draw_sigil(p, c1, cx, cy, r, t, glow=1.0):
    """Ai-ru's mark: a hexagon holding a slowly turning diamond and a hot core."""
    p.save()
    p.translate(cx, cy)
    p.setPen(NOPEN)
    p.setCompositionMode(ADD)
    g = QRadialGradient(0, 0, r * 1.9)
    g.setColorAt(0, rgba(c1, 95 * glow))
    g.setColorAt(1, rgba(c1, 0))
    p.setBrush(g)
    p.drawEllipse(QPointF(0, 0), r * 1.9, r * 1.9)
    p.setCompositionMode(SRC)
    p.setBrush(NOBRUSH)
    w = max(1.0, r * 0.11)
    p.setPen(pen_of(c1, 235, w))
    p.drawPath(hex_path(0, 0, r))
    p.rotate(math.degrees(t * 0.7))
    p.setPen(pen_of(lighten(c1, 0.45), 200, w * 0.8))
    p.drawPolygon(diamond(r * 0.58))
    p.setPen(NOPEN)
    p.setBrush(rgba(lighten(c1, 0.85), 255))
    p.drawPolygon(diamond(r * 0.22))
    p.restore()


def make_app_icon(accent=(26, 170, 255)):
    """Painted at runtime so the app ships with an icon and no image files."""
    icon = QIcon()
    for s in (16, 24, 32, 48, 64, 128, 256):
        pm = QPixmap(s, s)
        pm.fill(Qt.GlobalColor.transparent)
        p = QPainter(pm)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        body = hex_path(s / 2, s / 2, s * 0.47)
        g = QLinearGradient(0, 0, 0, s)
        g.setColorAt(0, QColor(8, 16, 30))
        g.setColorAt(1, QColor(2, 3, 8))
        p.fillPath(body, g)
        p.setPen(QPen(rgba(accent, 150), max(1.0, s * 0.03)))
        p.drawPath(body)
        draw_sigil(p, accent, s / 2, s / 2, s * 0.25, 0.6)
        p.end()
        icon.addPixmap(pm)
    return icon


# --------------------------------------------------------------------------------------
# Persistence
# --------------------------------------------------------------------------------------
def load_json(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def save_json(path, data):
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)
    except Exception:
        traceback.print_exc()


def load_sessions():
    out = []
    for s in load_json(SESSIONS_FILE, []):
        try:
            out.append({"id": int(s["id"]), "title": str(s["title"]), "ts": float(s["ts"]),
                        "msgs": [(str(a), str(b)) for a, b in s["msgs"]]})
        except Exception:
            continue
    return out


# --------------------------------------------------------------------------------------
# Launch at login
# --------------------------------------------------------------------------------------
def set_autostart(enabled: bool) -> bool:
    """Register (or remove) this program as a login item. It is started with --background so it
    comes up as the floating orb instead of throwing a window at you."""
    try:
        script = os.path.abspath(sys.argv[0]) if sys.argv and sys.argv[0] else ""
        frozen = bool(getattr(sys, "frozen", False))
        if sys.platform == "win32":
            import winreg
            exe = sys.executable
            if frozen:
                cmd = f'"{exe}" --background'
            else:
                pyw = os.path.join(os.path.dirname(exe), "pythonw.exe")
                cmd = f'"{pyw if os.path.exists(pyw) else exe}" "{script}" --background'
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run", 0,
                                winreg.KEY_SET_VALUE) as key:
                if enabled:
                    winreg.SetValueEx(key, APP_NAME, 0, winreg.REG_SZ, cmd)
                else:
                    try:
                        winreg.DeleteValue(key, APP_NAME)
                    except FileNotFoundError:
                        pass
            return True
        if sys.platform.startswith("linux"):
            f = HOME / ".config" / "autostart" / "airu.desktop"
            if enabled:
                f.parent.mkdir(parents=True, exist_ok=True)
                cmd = f'"{sys.executable}" "{script}" --background' if not frozen else f'"{sys.executable}" --background'
                f.write_text(f"[Desktop Entry]\nType=Application\nName={APP_NAME}\nExec={cmd}\n"
                             "X-GNOME-Autostart-enabled=true\n", encoding="utf-8")
            elif f.exists():
                f.unlink()
            return True
    except Exception:
        traceback.print_exc()
    return False


# --------------------------------------------------------------------------------------
# Synthesized tactile audio
# --------------------------------------------------------------------------------------
SR = 44100
SOUND = None


def sfx(name):
    if SOUND is not None:
        SOUND.play(name)


def _click(freq=2200, dur=0.03, noise=0.45, body=160, gain=1.0):
    n = int(SR * dur)
    t = np.arange(n) / SR
    rng = np.random.default_rng()
    sig = (0.6 * np.sin(TAU * freq * t) * np.exp(-t * 190)
           + noise * rng.standard_normal(n) * np.exp(-t * 450)
           + 0.5 * np.sin(TAU * body * t) * np.exp(-t * 100))
    return gain * sig


def _sweep(f0, f1, dur, gain=1.0, decay=10.0):
    n = int(SR * dur)
    t = np.arange(n) / SR
    ph = TAU * np.cumsum(np.linspace(f0, f1, n)) / SR
    return gain * np.sin(ph) * np.minimum(1.0, t / 0.006) * np.exp(-t * decay)


def _mix(parts, level):
    length = max(int(SR * off) + len(a) for off, a in parts)
    out = np.zeros(length)
    for off, a in parts:
        i = int(SR * off)
        out[i:i + len(a)] += a
    peak = float(np.max(np.abs(out))) or 1.0
    return (out / peak * level * 32767).astype(np.int16)


def build_sounds():
    rnd = random.Random(3)
    crackle = [(rnd.uniform(0, 0.16), _click(rnd.uniform(1500, 4200), 0.02, 0.9, 200, rnd.uniform(0.4, 1.0)))
               for _ in range(9)] + [(0, _sweep(300, 1300, 0.22, 0.5, 12))]
    return {
        "key":       _mix([(0, _click(3000, 0.016, 0.3, 220))], 0.20),
        "tick":      _mix([(0, _click(2600, 0.022, 0.35, 140))], 0.30),
        "press":     _mix([(0, _click(1700, 0.030, 0.50, 120))], 0.38),
        "send":      _mix([(0, _click(1500)), (0.045, _click(2300)), (0.05, _sweep(700, 1150, 0.12, 0.5, 14))], 0.42),
        "recv":      _mix([(0, _sweep(520, 780, 0.14, 1.0, 14)), (0, _click(1900, 0.02, 0.2, 140, 0.4))], 0.26),
        "new":       _mix([(0, _click(900, 0.05, 0.6, 90)), (0.03, _sweep(300, 640, 0.16, 0.7, 12))], 0.40),
        "mic_on":    _mix([(0, _click(1900)), (0.03, _sweep(620, 940, 0.10, 0.6, 16))], 0.38),
        "mic_off":   _mix([(0, _click(1500)), (0.03, _sweep(900, 560, 0.10, 0.6, 16))], 0.38),
        "crackle":   _mix(crackle, 0.36),
        "idle":      _mix([(0, _sweep(560, 420, 0.20, 1.0, 12))], 0.22),
        "listening": _mix([(0, _sweep(640, 900, 0.15, 1.0, 14))], 0.24),
        "thinking":  _mix([(0, _sweep(1100, 1100, 0.04, 1, 40)), (0.05, _sweep(1400, 1400, 0.04, 1, 40)),
                           (0.10, _sweep(1750, 1750, 0.05, 1, 40))], 0.20),
        "speaking":  _mix([(0, _sweep(500, 720, 0.12, 1.0, 12)), (0.10, _sweep(720, 620, 0.12, 0.8, 12))], 0.24),
        "whoosh":    _mix([(0, _sweep(180, 900, 0.22, 1.0, 9)), (0.02, _click(800, 0.04, 0.8, 80, 0.5))], 0.34),
        "palette":   _mix([(0, _click(2100, 0.02, 0.3, 150)), (0.03, _sweep(900, 1300, 0.07, 0.5, 20))], 0.30),
        "zap":       _mix([(0, _sweep(2200, 260, 0.13, 1.0, 20)), (0, _click(3500, 0.03, 0.9, 200))], 0.36),
        "pop":       _mix([(0, _click(1100, 0.04, 0.5, 180)), (0.02, _sweep(520, 200, 0.10, 0.8, 22))], 0.32),
        "slide":     _mix([(0, _sweep(240, 520, 0.18, 0.8, 10))], 0.24),
    }


class SoundEngine:
    def __init__(self):
        self.enabled = True
        self._fx, self._idx, self._tmp = {}, {}, None
        if not HAVE_MM:
            return
        try:
            self._tmp = tempfile.TemporaryDirectory(prefix="airu_snd_")
            for name, data in build_sounds().items():
                path = os.path.join(self._tmp.name, name + ".wav")
                with wave.open(path, "wb") as wf:
                    wf.setnchannels(1)
                    wf.setsampwidth(2)
                    wf.setframerate(SR)
                    wf.writeframes(data.tobytes())
                pool = []
                for _ in range(3):
                    fx = QSoundEffect()
                    fx.setSource(QUrl.fromLocalFile(path))
                    fx.setVolume(0.7)
                    pool.append(fx)
                self._fx[name], self._idx[name] = pool, 0
        except Exception:
            traceback.print_exc()
            self._fx = {}

    def play(self, name):
        pool = self._fx.get(name)
        if not self.enabled or not pool:
            return
        i = self._idx[name]
        self._idx[name] = (i + 1) % len(pool)
        pool[i].play()


# --------------------------------------------------------------------------------------
# Presence: the single animated source of truth (shared by window, wave line and floating orb)
# --------------------------------------------------------------------------------------
class Presence:
    def __init__(self):
        d = STATE_DEFS["IDLE"]
        self.state, self.party = "IDLE", False
        self.c1, self.c2 = [float(v) for v in d["c1"]], [float(v) for v in d["c2"]]
        self.speed, self.hz, self.energy = d["speed"], d["hz"], d["energy"]
        self.level = self.kick_v = 0.0
        self.shock_req = 0
        self.weights = {k: (1.0 if k == "IDLE" else 0.0) for k in STATE_DEFS}
        self.rot = self.phase = self.t = self.flash = 0.0
        self.ext_level, self.ext_time = 0.0, -10.0

    def set_state(self, s):
        if s != self.state:
            self.state = s
            self.flash = 1.0
            self.shock_req += 1

    def kick(self, v, shock=False):
        self.kick_v = max(self.kick_v, v)
        if shock:
            self.shock_req += 1

    def push_level(self, v):
        self.ext_level, self.ext_time = clamp(float(v)), self.t

    def targets(self):
        d = STATE_DEFS[self.state]
        if self.party:
            sh = (self.t * 70.0) % 360.0
            return hue_shift(d["c1"], sh), hue_shift(d["c2"], sh + 25.0)
        return d["c1"], d["c2"]

    def _synth(self):
        t, s = self.t, self.state
        if s == "LISTENING":
            burst = max(0.0, math.sin(t * 2.3) * math.sin(t * 0.9 + 1.0))
            return clamp(0.14 + 0.85 * burst * (0.65 + 0.35 * abs(math.sin(t * 11.0))) + 0.05 * random.random())
        if s == "SPEAKING":
            syl = abs(math.sin(t * 6.2)) ** 0.8 * (0.55 + 0.45 * math.sin(t * 1.9) ** 2)
            return clamp(0.15 + 0.75 * syl)
        if s == "THINKING":
            return clamp(0.5 + 0.25 * math.sin(t * 5.0) * math.sin(t * 1.3))
        return 0.0

    def advance(self, dt):
        dt = min(dt, 0.1)
        self.t += dt
        d = STATE_DEFS[self.state]
        t1, t2 = self.targets()
        for i in range(3):
            self.c1[i] = approach(self.c1[i], t1[i], 3.5, dt)
            self.c2[i] = approach(self.c2[i], t2[i], 3.5, dt)
        self.speed = approach(self.speed, d["speed"], 2.5, dt)
        self.hz = approach(self.hz, d["hz"], 2.5, dt)
        self.kick_v = approach(self.kick_v, 0.0, 6.0, dt)
        base = self.ext_level if self.t - self.ext_time < 0.4 else self._synth()
        tgt = clamp(base + self.kick_v)
        self.level = approach(self.level, tgt, 14 if tgt > self.level else 7, dt)
        self.energy = approach(self.energy, d["energy"] + 0.35 * self.level, 5.0, dt)
        for k, v in self.weights.items():
            self.weights[k] = approach(v, 1.0 if k == self.state else 0.0, 3.2, dt)
        self.rot += dt * self.speed
        self.phase += dt * TAU * self.hz
        self.flash = max(0.0, self.flash - dt * 2.2)


# --------------------------------------------------------------------------------------
# Dust cloud: a small Qt-painted echo of the Dust Orb (floating orb + fallback when WebEngine is missing)
# --------------------------------------------------------------------------------------
class DustCloud:
    def __init__(self, n=900):
        rng = np.random.default_rng(5)
        z = rng.uniform(-1, 1, n)
        a = rng.uniform(0, TAU, n)
        s = np.sqrt(1 - z * z)
        layer = rng.random(n)
        r = np.where(layer < 0.8, 1.0 + rng.uniform(-0.07, 0.07, n),
                     np.where(layer < 0.94, rng.random(n) ** 0.4, 1.1 + rng.random(n) * 0.22))
        self.p = np.stack([s * np.cos(a) * r, z * r, s * np.sin(a) * r], axis=1)
        self.seed = rng.random((n, 3))

    def draw(self, p, P, R, size=1.0):
        t, lvl = P.t, P.level
        q, sd = self.p, self.seed
        wob = (1.0 + 0.07 * np.sin(q[:, 1] * 5.0 + t * 0.8 + sd[:, 0] * TAU) * np.cos(q[:, 0] * 4.0 - t * 0.5)
               + 0.05 * lvl * np.sin(t * 8.0 + sd[:, 1] * TAU))
        scale = 1.0 + 0.025 * math.sin(P.phase) + 0.10 * lvl + 0.05 * P.flash
        a = t * 0.22 * (0.6 + 0.4 * P.speed)
        ca, sa = math.cos(a), math.sin(a)
        x = (q[:, 0] * ca + q[:, 2] * sa) * wob * scale
        z = (-q[:, 0] * sa + q[:, 2] * ca) * wob * scale
        y = q[:, 1] * wob * scale
        ct, st = math.cos(0.25), math.sin(0.25)
        y2, z2 = y * ct - z * st, y * st + z * ct
        persp = 3.6 / (3.6 - z2)
        X, Y = x * persp * R, y2 * persp * R
        front = np.clip((z2 + 1.3) / 2.6, 0.0, 1.0)
        c1, c2 = P.c1, P.c2
        p.setPen(NOPEN)
        p.setCompositionMode(ADD)
        g = QRadialGradient(0, 0, R * 0.9)
        g.setColorAt(0, rgba(lighten(c1, 0.5), 70 + 80 * P.energy))
        g.setColorAt(0.5, rgba(c2, 34 + 30 * P.energy))
        g.setColorAt(1, rgba(c2, 0))
        p.setBrush(g)
        p.drawEllipse(QPointF(0, 0), R * 0.9, R * 0.9)
        for lo, hi, alpha, w in ((0.0, 0.38, 70, 1.3), (0.38, 0.68, 125, 1.7), (0.68, 1.01, 200, 2.1)):
            m = (front >= lo) & (front < hi)
            if not m.any():
                continue
            pts = QPolygonF([QPointF(a_, b_) for a_, b_ in zip(X[m].tolist(), Y[m].tolist())])
            pen = QPen(rgba(mix(c2, lighten(c1, 0.35), (lo + hi) / 2), alpha), w * size)
            pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            p.setPen(pen)
            p.drawPoints(pts)
        p.setCompositionMode(SRC)


# --------------------------------------------------------------------------------------
# The orb host: Dust Orb + aurora in a web view. Falls back to a painted cloud if assets are missing.
# --------------------------------------------------------------------------------------
class OrbPage(QWebEnginePage):
    interacted = pyqtSignal(bool)  # True = tap on the orb, False = drag released

    def javaScriptConsoleMessage(self, level, message, line, source):
        if message == "airu:poke":
            self.interacted.emit(True)
        elif message == "airu:release":
            self.interacted.emit(False)


class OrbView(QWidget):
    interacted = pyqtSignal(bool)

    def __init__(self, pres):
        super().__init__()
        self.pres, self.ready, self.pending, self.view, self.reason = pres, False, [], None, ""
        self._last_voice = -1.0
        self.cloud = DustCloud(1100)
        self.setMinimumHeight(200)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        three = ASSETS / "vendor" / "three-r180.js"
        if os.environ.get("AIRU_SOFT_ORB"):
            self.reason = "AIRU_SOFT_ORB is set"
        elif not HAVE_WEB:
            self.reason = "PyQt6-WebEngine is not installed (pip install PyQt6-WebEngine)"
        elif not (ASSETS / "orb.html").exists():
            self.reason = "assets/orb.html is missing"
        elif not three.exists():
            self.reason = "assets/vendor/three-r180.js is missing (copy it from the dust-orb package)"
        else:
            self.view = QWebEngineView(self)
            self.page = OrbPage(QWebEngineProfile(QApplication.instance()), self.view)  # off-the-record profile
            self.page.setBackgroundColor(QColor(3, 3, 5))
            self.page.interacted.connect(self.interacted)
            self.page.loadFinished.connect(self._loaded)
            self.view.setPage(self.page)
            self.view.setContextMenuPolicy(Qt.ContextMenuPolicy.NoContextMenu)
            self.view.setFocusPolicy(Qt.FocusPolicy.NoFocus)  # typing keeps going to the message box
            self.view.setUrl(QUrl.fromLocalFile(str(ASSETS / "orb.html")))
            lay.addWidget(self.view)

    def _loaded(self, ok):
        self.ready = bool(ok)
        if ok:
            for code in self.pending:
                self.page.runJavaScript(code)
        else:
            self.reason = "the orb page failed to load"
        self.pending = []

    def js(self, code):
        if self.view is None:
            return
        if self.ready:
            self.page.runJavaScript(code)
        else:
            self.pending.append(code)

    def set_state(self, s):
        self.js(f"airu.setState('{ORB_STATE.get(s, 'idle')}')")

    def set_theme(self, c1, c2):
        dust = [v / 255.0 for v in mix(c1, (255, 255, 255), 0.10)]
        ember = [v / 255.0 for v in lighten(c1, 0.22)]
        self.js(f"airu.setPalette({json.dumps(dust)},{json.dumps(ember)})")

    def set_aurora(self, c1, c2):
        self.js(f"airu.setAurora('{hexstr(c1)}','{hexstr(c2)}','{hexstr(lighten(c1, 0.55))}')")

    def set_voice(self, v):
        if self.ready and abs(v - self._last_voice) > 0.015:
            self._last_voice = v
            self.page.runJavaScript(f"airu.setVoice({v:.3f})")

    def pulse(self):
        self.js("airu.pulse()")

    def tick(self):
        if self.view is None:
            self.update()

    def paintEvent(self, _):
        if self.view is not None:
            return
        P = self.pres
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = self.width(), self.height()
        p.translate(w / 2, h / 2)
        self.cloud.draw(p, P, max(20.0, min(w, h) / 2 * 0.8))


# --------------------------------------------------------------------------------------
# Wave line (voice print) and state strip
# --------------------------------------------------------------------------------------
class WaveLine(QWidget):
    """A flowing audio line. Faint at rest, full when the mic is on or Ai-ru speaks."""

    def __init__(self, pres):
        super().__init__()
        self.pres = pres
        self.setFixedHeight(46)

    def paintEvent(self, _):
        P = self.pres
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        W, H = self.width(), self.height()
        cy = H / 2
        vis = clamp(P.weights["LISTENING"] + 0.85 * P.weights["SPEAKING"] + 0.35 * P.weights["THINKING"])
        amp = (2.0 + H * 0.46 * P.level * 1.2) * (0.25 + 0.75 * vis)
        x0, x1 = W * 0.14, W * 0.86
        c1, t = P.c1, P.t
        p.setPen(QPen(rgba(c1, 26 + 40 * vis), 1))
        p.drawLine(QPointF(x0, cy), QPointF(x1, cy))
        p.setCompositionMode(ADD)
        n = 110
        for k, (freq, spd, al, w) in enumerate(((1.0, 1.0, 235, 1.8), (1.7, -1.3, 135, 1.2), (2.6, 1.9, 85, 1.0))):
            path = QPainterPath()
            for i in range(n + 1):
                f = i / n
                env = math.sin(math.pi * f) ** 1.6
                y = cy + amp * env * math.sin(f * TAU * 2.5 * freq + t * (2.5 + 4.0 * vis) * spd + k) \
                    * (0.65 + 0.35 * math.sin(f * 7 + t * 3 + k))
                pt = QPointF(x0 + (x1 - x0) * f, y)
                path.moveTo(pt) if i == 0 else path.lineTo(pt)
            fade = 0.30 + 0.70 * vis
            p.setBrush(NOBRUSH)
            p.setPen(pen_of(c1, al * 0.20 * fade, w * 4.5))
            p.drawPath(path)
            p.setPen(pen_of(lighten(c1, 0.35 * (1 - k * 0.3)), al * fade, w))
            p.drawPath(path)
        p.setCompositionMode(SRC)
        p.setPen(NOPEN)
        p.setBrush(rgba(lighten(c1, 0.5), 120 + 120 * vis))
        for x in (x0, x1):
            p.save()
            p.translate(x, cy)
            p.drawPolygon(diamond(3.4))
            p.restore()


class StateStrip(QWidget):
    """Live thought line over a ruled state label. One widget, so the two can never overlap."""

    def __init__(self, pres):
        super().__init__()
        self.pres, self.thought, self.thought_at = pres, "", -100.0
        self.setFixedHeight(64)

    def set_thought(self, text):
        self.thought, self.thought_at = text, time.time()

    def paintEvent(self, _):
        P = self.pres
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        W = self.width()
        c1 = P.c1
        age = time.time() - self.thought_at
        if self.thought and age < 7:
            fade = clamp(age / 0.4) * clamp((7 - age) / 1.5)
            p.setFont(make_font(10, QFont.Weight.Normal, 0.3, italic=True))
            p.setPen(rgba(lighten(c1, 0.5), 205 * fade))
            fm = QFontMetrics(p.font())
            p.drawText(QRectF(0, 0, W, 22), ALIGN_C, fm.elidedText(self.thought, Qt.TextElideMode.ElideRight, int(W * 0.7)))
        label = STATE_DEFS[P.state]["label"]
        font = make_font(11, QFont.Weight.DemiBold, 4)
        p.setFont(font)
        tw = QFontMetrics(font).horizontalAdvance(label)
        cx, cy, gap = W / 2, 43.0, 26.0
        for sgn in (-1, 1):
            xa = cx + sgn * (tw / 2 + gap)
            xb = xa + sgn * 170
            g = QLinearGradient(xa, 0, xb, 0)
            g.setColorAt(0, rgba(c1, 150))
            g.setColorAt(1, rgba(c1, 0))
            p.setPen(QPen(g, 1.2))
            p.drawLine(QPointF(xa, cy), QPointF(xb, cy))
            p.setPen(NOPEN)
            p.setBrush(rgba(lighten(c1, 0.5), 230))
            p.save()
            p.translate(cx + sgn * (tw / 2 + gap * 0.5), cy)
            p.drawPolygon(diamond(2.6 + 0.8 * math.sin(P.phase)))
            p.restore()
        p.setPen(rgba(lighten(c1, 0.4), 225))
        p.drawText(QRectF(0, cy - 13, W, 26), ALIGN_C, label)


# --------------------------------------------------------------------------------------
# Buttons
# --------------------------------------------------------------------------------------
class GlassButton(QAbstractButton):
    def __init__(self, pres, parent=None):
        super().__init__(parent)
        self.pres, self.h, self.down, self.press_sound = pres, 0.0, False, "press"
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._anim = QVariantAnimation(self)
        self._anim.setDuration(160)
        self._anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._anim.valueChanged.connect(self._set_h)

    def _set_h(self, v):
        self.h = float(v)
        self.update()

    def _go(self, target):
        self._anim.stop()
        self._anim.setStartValue(self.h)
        self._anim.setEndValue(target)
        self._anim.start()

    def enterEvent(self, e):
        self._go(1.0)
        super().enterEvent(e)

    def leaveEvent(self, e):
        self._go(0.0)
        super().leaveEvent(e)

    def mousePressEvent(self, e):
        self.down = True
        self.update()
        if self.press_sound:
            sfx(self.press_sound)
        super().mousePressEvent(e)

    def mouseReleaseEvent(self, e):
        self.down = False
        self.update()
        super().mouseReleaseEvent(e)


class IconButton(GlassButton):
    def __init__(self, pres, kind):
        super().__init__(pres)
        self.kind = kind
        self.setFixedSize(34, 34)

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        c = (255, 92, 116) if self.kind == "close" else self.pres.c1
        if self.h > 0:
            p.setPen(QPen(rgba(c, 110 * self.h), 1.0))
            p.setBrush(rgba(c, 34 * self.h))
            p.drawPath(chamfer(QRectF(self.rect()).adjusted(3, 3, -3, -3), 7, 0, 7, 0))
        p.setPen(pen_of(lighten(c, 0.3), 150 + 105 * self.h, 1.6))
        p.setBrush(NOBRUSH)
        p.translate(self.width() / 2, self.height() / 2)
        k = self.kind
        if k == "min":
            p.drawLine(QPointF(-5, 2), QPointF(5, 2))
        elif k == "close":
            p.drawLine(QPointF(-4.5, -4.5), QPointF(4.5, 4.5))
            p.drawLine(QPointF(-4.5, 4.5), QPointF(4.5, -4.5))
        elif k == "menu":
            for y, x2 in ((-4.5, 6), (0, 3.5), (4.5, 6)):
                p.drawLine(QPointF(-6, y), QPointF(x2, y))
        elif k == "search":
            p.drawEllipse(QPointF(-1.5, -1.5), 4.6, 4.6)
            p.drawLine(QPointF(1.8, 1.8), QPointF(5.5, 5.5))
        elif k == "full":
            full = self.window().isFullScreen() if self.window() else False
            s, L = 5.0, 3.6
            for sx, sy in ((-1, -1), (1, -1), (-1, 1), (1, 1)):
                if full:
                    pts = [QPointF(sx * (s - L), sy * s), QPointF(sx * (s - L), sy * (s - L)), QPointF(sx * s, sy * (s - L))]
                else:
                    pts = [QPointF(sx * s, sy * (s - L)), QPointF(sx * s, sy * s), QPointF(sx * (s - L), sy * s)]
                p.drawPolyline(QPolygonF(pts))
        else:  # sound
            p.drawPolygon(QPolygonF([QPointF(-7, -3), QPointF(-4, -3), QPointF(0, -6.5), QPointF(0, 6.5),
                                     QPointF(-4, 3), QPointF(-7, 3)]))
            if SOUND is None or SOUND.enabled:
                p.drawArc(QRectF(-3, -4, 8, 8), -50 * 16, 100 * 16)
                p.drawArc(QRectF(-4, -7.5, 15, 15), -50 * 16, 100 * 16)
            else:
                p.drawLine(QPointF(3, -3), QPointF(8, 3))
                p.drawLine(QPointF(8, -3), QPointF(3, 3))


class PillButton(GlassButton):
    """Cut-corner action button with a plus mark and a shortcut hint."""

    def __init__(self, pres, text, hint=""):
        super().__init__(pres)
        self.label, self.hint = text, hint
        self.setFixedHeight(44)

    def paintEvent(self, _):
        P = self.pres
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = QRectF(self.rect()).adjusted(1.5, 1.5, -1.5, -1.5)
        if self.down:
            r = r.adjusted(1, 1, -1, -1)
        path = chamfer(r, 14, 0, 14, 0)
        g = QLinearGradient(r.topLeft(), r.topRight())
        g.setColorAt(0, rgba(P.c1, 30 + 44 * self.h))
        g.setColorAt(1, rgba(P.c2, 6 + 24 * self.h))
        p.setBrush(g)
        p.setPen(QPen(rgba(P.c1, 90 + 140 * self.h), 1.1))
        p.drawPath(path)
        p.setPen(pen_of(P.c1, 140 + 115 * self.h, 2.2))  # brighter diagonal edges
        p.drawLine(QPointF(r.left(), r.top() + 14), QPointF(r.left() + 14, r.top()))
        p.drawLine(QPointF(r.right() - 14, r.bottom()), QPointF(r.right(), r.bottom() - 14))
        cy = r.center().y()
        p.setPen(pen_of(lighten(P.c1, 0.5), 255, 1.7))
        p.drawLine(QPointF(r.left() + 24, cy), QPointF(r.left() + 34, cy))
        p.drawLine(QPointF(r.left() + 29, cy - 5), QPointF(r.left() + 29, cy + 5))
        p.setFont(make_font(10.5, QFont.Weight.DemiBold, 0.8))
        p.setPen(rgba(lighten(P.c1, 0.6), 255))
        p.drawText(QRectF(r.left() + 46, r.top(), r.width() - 60, r.height()), ALIGN_VL, self.label)
        if self.hint:
            p.setFont(make_font(8.5))
            p.setPen(rgba(P.c1, 130))
            p.drawText(QRectF(r.left(), r.top(), r.width() - 24, r.height()), ALIGN_VR, self.hint)


class MicButton(GlassButton):
    def __init__(self, pres):
        super().__init__(pres)
        self.setCheckable(True)
        self.setFixedSize(56, 56)
        self.press_sound = None

    def paintEvent(self, _):
        P = self.pres
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        c, on = P.c1, self.isChecked()
        p.translate(self.width() / 2, self.height() / 2)
        if on:
            p.setBrush(NOBRUSH)
            for k in range(2):
                pr = (P.t * 0.9 + k * 0.5) % 1.0
                p.setPen(QPen(rgba(c, 150 * (1 - pr)), 1.4))
                p.drawPath(hex_path(0, 0, 21 + 7 * pr))
            p.setCompositionMode(ADD)
            for k in range(6):  # sparks off the hexagon corners
                a = -math.pi / 2 + k * TAU / 6
                ln = 3 + 6 * (0.5 + 0.5 * math.sin(P.t * 11 + k * 2.1))
                p.setPen(pen_of(lighten(c, 0.6), 190, 1.3))
                p.drawLine(QPointF(22 * math.cos(a), 22 * math.sin(a)), QPointF((22 + ln) * math.cos(a), (22 + ln) * math.sin(a)))
            p.setCompositionMode(SRC)
        p.setBrush(rgba(c, (70 if on else 16) + 30 * self.h))
        p.setPen(QPen(rgba(c, (230 if on else 110) + 25 * self.h), 1.3))
        p.drawPath(hex_path(0, 0, 21 - (1 if self.down else 0)))
        p.setPen(pen_of(lighten(c, 0.5), 255 if on else 190, 1.7))
        p.setBrush(NOBRUSH)
        p.drawRoundedRect(QRectF(-3.5, -9, 7, 12), 3.5, 3.5)
        p.drawArc(QRectF(-7, -6, 14, 13), 180 * 16, 180 * 16)
        p.drawLine(QPointF(0, 7), QPointF(0, 10.5))
        p.drawLine(QPointF(-3.5, 10.5), QPointF(3.5, 10.5))


class SendButton(GlassButton):
    def __init__(self, pres):
        super().__init__(pres)
        self.setFixedSize(56, 56)
        self.press_sound = None
        self.ready = False

    def paintEvent(self, _):
        P = self.pres
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        c = P.c1
        p.translate(self.width() / 2, self.height() / 2)
        half = 18 - (1 if self.down else 0)
        shape = chamfer(QRectF(-half, -half, 2 * half, 2 * half), 9, 0, 9, 0)
        if self.ready:
            p.setCompositionMode(ADD)
            g = QRadialGradient(0, 0, 30)
            g.setColorAt(0.55, rgba(c, 70 + 50 * self.h))
            g.setColorAt(1, rgba(c, 0))
            p.setPen(NOPEN)
            p.setBrush(g)
            p.drawEllipse(QPointF(0, 0), 30, 30)
            p.setCompositionMode(SRC)
            p.setBrush(rgba(c, 215 + 40 * self.h))
            p.setPen(NOPEN)
        else:
            p.setBrush(rgba(c, 8))
            p.setPen(QPen(rgba(c, 75), 1.2))
        p.drawPath(shape)
        p.setPen(pen_of((3, 6, 12) if self.ready else c, 255 if self.ready else 120, 2.0))
        p.setBrush(NOBRUSH)
        p.drawPolyline(QPolygonF([QPointF(-6, 1), QPointF(0, -5), QPointF(6, 1)]))
        p.drawLine(QPointF(0, -5), QPointF(0, 7))


# --------------------------------------------------------------------------------------
# Input dock
# --------------------------------------------------------------------------------------
class InputDock(QWidget):
    GM, CUT, CUT_S = 8, 20, 6

    def __init__(self, pres):
        super().__init__()
        self.pres, self.focus = pres, 0.0
        self.setFixedHeight(56 + 2 * self.GM)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(self.GM + 52, self.GM, self.GM + 10, self.GM)
        lay.setSpacing(2)
        self.entry = QLineEdit()
        self.entry.setObjectName("entry")
        self.entry.setFont(make_font(12.5))
        self.entry.setPlaceholderText("Message Ai-ru")
        self.entry.setFrame(False)
        self.entry.installEventFilter(self)
        self.mic = MicButton(pres)
        self.send = SendButton(pres)
        lay.addWidget(self.entry, 1)
        lay.addWidget(self.mic)
        lay.addWidget(self.send)
        self._anim = QVariantAnimation(self)
        self._anim.setDuration(240)
        self._anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._anim.valueChanged.connect(self._set_focus)
        self.entry.textChanged.connect(self._text_changed)

    def _text_changed(self, s):
        self.send.ready = bool(s.strip())
        self.send.update()

    def _set_focus(self, v):
        self.focus = float(v)
        self.update()

    def refresh(self):
        self.update()
        self.mic.update()
        self.send.update()

    def eventFilter(self, obj, ev):
        if obj is self.entry and ev.type() in (QEvent.Type.FocusIn, QEvent.Type.FocusOut):
            self._anim.stop()
            self._anim.setStartValue(self.focus)
            self._anim.setEndValue(1.0 if ev.type() == QEvent.Type.FocusIn else 0.0)
            self._anim.start()
        return super().eventFilter(obj, ev)

    def paintEvent(self, _):
        P, M, C, S = self.pres, self.GM, self.CUT, self.CUT_S
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = QRectF(self.rect()).adjusted(M, M, -M, -M)
        p.setBrush(NOBRUSH)
        for i in range(1, M + 1):  # soft halo that follows the cut shape
            p.setPen(QPen(rgba(P.c1, (10 + 60 * self.focus) * (1 - i / M) ** 2), 1.4))
            k = 0.414 * i
            p.drawPath(chamfer(r.adjusted(-i, -i, i, i), C + k, S + k, C + k, S + k))
        g = QLinearGradient(r.topLeft(), r.bottomLeft())
        g.setColorAt(0, QColor(150, 200, 255, 16))
        g.setColorAt(1, QColor(150, 200, 255, 5))
        p.setBrush(g)
        p.setPen(QPen(rgba(P.c1, 50 + 120 * self.focus), 1.1))
        p.drawPath(chamfer(r, C, S, C, S))
        p.setPen(pen_of(P.c1, 150 + 105 * self.focus, 2.2))  # bright cut edges
        p.drawLine(QPointF(r.left(), r.top() + C), QPointF(r.left() + C, r.top()))
        p.drawLine(QPointF(r.right() - C, r.bottom()), QPointF(r.right(), r.bottom() - C))
        if self.focus > 0.01:  # underline grows from the middle on focus
            half = r.width() / 2 * self.focus
            cx, y = r.center().x(), r.bottom() - 0.5
            ug = QLinearGradient(cx - half, 0, cx + half, 0)
            ug.setColorAt(0, rgba(P.c1, 0))
            ug.setColorAt(0.5, rgba(lighten(P.c1, 0.4), 255))
            ug.setColorAt(1, rgba(P.c1, 0))
            p.setPen(QPen(ug, 2.0))
            p.drawLine(QPointF(cx - half, y), QPointF(cx + half, y))
        draw_sigil(p, P.c1, r.left() + C + 14, r.center().y(), 7.5, P.t, 0.6 + 0.6 * self.focus)


# --------------------------------------------------------------------------------------
# Conversation
# --------------------------------------------------------------------------------------
class Sigil(QWidget):
    """The little mark beside Ai-ru's messages."""

    def __init__(self, pres):
        super().__init__()
        self.pres = pres
        self.setFixedSize(26, 26)

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        draw_sigil(p, self.pres.c1, 13, 13, 9, 0.9, 0.8)


class Bubble(QWidget):
    """Message plate with cut corners. User plates cut top-left/bottom-right, Ai-ru's the mirror."""

    def __init__(self, pres, kind):
        super().__init__()
        self.pres, self.kind = pres, kind

    def paintEvent(self, _):
        P = self.pres
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = QRectF(self.rect()).adjusted(1, 1, -1, -1)
        if self.kind == "user":
            path = chamfer(r, 14, 4, 14, 4)
            g = QLinearGradient(r.topLeft(), r.bottomRight())
            g.setColorAt(0, rgba(P.c1, 34))
            g.setColorAt(1, rgba(P.c2, 10))
            p.setBrush(g)
            p.setPen(QPen(rgba(P.c1, 80), 1.0))
            p.drawPath(path)
            p.setPen(pen_of(P.c1, 200, 1.8))
            p.drawLine(QPointF(r.left(), r.top() + 14), QPointF(r.left() + 14, r.top()))
        else:
            path = chamfer(r, 4, 14, 4, 14)
            g = QLinearGradient(r.topLeft(), r.bottomRight())
            g.setColorAt(0, QColor(150, 200, 255, 16))
            g.setColorAt(1, QColor(150, 200, 255, 4))
            p.setBrush(g)
            p.setPen(QPen(rgba(P.c1, 44), 1.0))
            p.drawPath(path)
            lg = QLinearGradient(0, r.top(), 0, r.bottom())  # glowing spine on the left
            lg.setColorAt(0, rgba(P.c1, 0))
            lg.setColorAt(0.25, rgba(P.c1, 230))
            lg.setColorAt(0.75, rgba(P.c1, 230))
            lg.setColorAt(1, rgba(P.c1, 0))
            p.setPen(QPen(lg, 2.2))
            p.drawLine(QPointF(r.left() + 0.5, r.top() + 4), QPointF(r.left() + 0.5, r.bottom() - 4))
            p.setPen(pen_of(P.c1, 190, 1.8))
            p.drawLine(QPointF(r.right() - 14, r.top()), QPointF(r.right(), r.top() + 14))


class MessageRow(QWidget):
    MAXW = 700

    def __init__(self, pres, sender, text, animate=True):
        super().__init__()
        user = is_user(sender)
        outer = QHBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(12)
        self.body = QLabel()
        self.body.setObjectName("body")
        self.body.setFont(make_font(12.5))
        self.body.setWordWrap(True)
        self.body.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self._full, self._n, self._timer = text, 0, None
        fm = QFontMetrics(self.body.font())
        width = fm.boundingRect(QRect(0, 0, self.MAXW, 100000), int(Qt.TextFlag.TextWordWrap.value), text).width() + 8
        self.body.setMaximumWidth(min(width, self.MAXW))
        self.body.setMinimumWidth(min(width, 360))  # QLabel's own wrap guess is narrow; keep bubbles roomy
        self.body.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Preferred)
        if user:
            self.body.setTextFormat(Qt.TextFormat.PlainText)
            self.body.setText(text)
            bub = Bubble(pres, "user")
            bl = QVBoxLayout(bub)
            bl.setContentsMargins(20, 12, 18, 12)
            bl.addWidget(self.body)
            outer.addStretch(1)
            outer.addWidget(bub, 0)
        else:
            name = QLabel(APP_NAME if is_ai(sender) else str(sender).title())
            name.setObjectName("sender")
            name.setFont(make_font(9.5, QFont.Weight.DemiBold, 1.6))
            bub = Bubble(pres, "ai")
            bl = QVBoxLayout(bub)
            bl.setContentsMargins(20, 11, 20, 13)
            bl.setSpacing(5)
            bl.addWidget(name)
            bl.addWidget(self.body)
            outer.addWidget(Sigil(pres), 0, Qt.AlignmentFlag.AlignTop)
            outer.addWidget(bub, 0, Qt.AlignmentFlag.AlignTop)
            outer.addStretch(1)  # keeps Ai-ru's replies hugging the left edge
            if animate and len(text) <= 1500:
                self.body.setTextFormat(Qt.TextFormat.RichText)
                self._reveal(0)
                self._timer = QTimer(self)
                self._timer.timeout.connect(self._step)
                self._timer.start(18)
            else:
                self.body.setTextFormat(Qt.TextFormat.PlainText)
                self.body.setText(text)
        if animate:
            fx = QGraphicsOpacityEffect(self)
            self.setGraphicsEffect(fx)
            a = QPropertyAnimation(fx, b"opacity", self)
            a.setDuration(320)
            a.setStartValue(0.0)
            a.setEndValue(1.0)
            a.finished.connect(lambda: self.setGraphicsEffect(None))
            a.start(QPropertyAnimation.DeletionPolicy.DeleteWhenStopped)

    def _reveal(self, n):
        esc = lambda s: html.escape(s).replace("\n", "<br>")
        self.body.setText(f'<span>{esc(self._full[:n])}</span><span style="color:rgba(0,0,0,0)">{esc(self._full[n:])}</span>')

    def _step(self):
        self._n += max(2, len(self._full) // 110)
        if self._n >= len(self._full):
            self._timer.stop()
            self.body.setTextFormat(Qt.TextFormat.PlainText)
            self.body.setText(self._full)
        else:
            self._reveal(self._n)


class ChatView(QScrollArea):
    def __init__(self, pres):
        super().__init__()
        self.pres = pres
        self.setWidgetResizable(True)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.viewport().setAutoFillBackground(False)
        self.inner = QWidget()
        self.inner.setObjectName("chatInner")
        self.setWidget(self.inner)
        self.lay = QVBoxLayout(self.inner)
        self.lay.setContentsMargins(4, 10, 12, 10)
        self.lay.setSpacing(16)
        self.lay.addStretch(1)
        self.stick = True
        sb = self.verticalScrollBar()
        sb.rangeChanged.connect(lambda lo, hi: self.stick and sb.setValue(hi))
        sb.valueChanged.connect(lambda v: setattr(self, "stick", v >= sb.maximum() - 48))
        self.hint = QLabel('Nothing here yet.\nSay "Ai-ru", type below, or press Ctrl+K.', self.viewport())
        self.hint.setObjectName("hint")
        self.hint.setFont(make_font(11.5))
        self.hint.setAlignment(ALIGN_C)

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self.hint.setGeometry(self.viewport().rect())

    def clear(self):
        while self.lay.count() > 1:
            w = self.lay.takeAt(1).widget()
            if w:
                w.deleteLater()
        self.stick = True
        self.hint.show()

    def add_message(self, sender, text, animate=True):
        self.hint.hide()
        self.lay.addWidget(MessageRow(self.pres, sender, text, animate))


# --------------------------------------------------------------------------------------
# Sidebar (slides in and out)
# --------------------------------------------------------------------------------------
class Wordmark(QWidget):
    def __init__(self, pres):
        super().__init__()
        self.pres = pres
        self.setFixedHeight(58)

    def paintEvent(self, _):
        P = self.pres
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        cy = self.height() / 2
        draw_sigil(p, P.c1, 28, cy, 14 * (1 + 0.04 * math.sin(P.phase)), P.t)
        p.setFont(make_font(20, QFont.Weight.DemiBold, 3.2))
        p.setPen(QColor(234, 245, 255))
        p.drawText(QRectF(58, 0, self.width() - 58, self.height()), ALIGN_VL, APP_NAME)


class ChronicleItem(QWidget):
    clicked = pyqtSignal(int)
    delete_requested = pyqtSignal(int)

    def __init__(self, pres, session, active):
        super().__init__()
        self.pres, self.s, self.active, self.hover, self.x_hover = pres, session, active, False, False
        self.setFixedHeight(60)
        self.setMouseTracking(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def _x_rect(self):
        return QRectF(self.width() - 34, self.height() / 2 - 12, 24, 24)

    def enterEvent(self, e):
        self.hover = True
        self.update()

    def leaveEvent(self, e):
        self.hover = self.x_hover = False
        self.update()

    def mouseMoveEvent(self, e):
        xh = self._x_rect().contains(e.position())
        if xh != self.x_hover:
            self.x_hover = xh
            self.update()

    def mousePressEvent(self, e):
        if e.button() != Qt.MouseButton.LeftButton:
            return
        if self._x_rect().contains(e.position()):
            self.delete_requested.emit(self.s["id"])
        else:
            self.clicked.emit(self.s["id"])

    def contextMenuEvent(self, e):
        m = QMenu(self)
        add_action(m, "Open", lambda: self.clicked.emit(self.s["id"]))
        add_action(m, "Delete", lambda: self.delete_requested.emit(self.s["id"]))
        m.exec(e.globalPos())

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r, c = QRectF(self.rect()).adjusted(2, 2, -2, -2), self.pres.c1
        shape = chamfer(r, 0, 0, 12, 0)
        p.setPen(NOPEN)
        if self.active:
            g = QLinearGradient(r.left(), 0, r.right(), 0)
            g.setColorAt(0, rgba(c, 44))
            g.setColorAt(1, rgba(c, 2))
            p.setBrush(g)
            p.drawPath(shape)
            p.setBrush(rgba(c, 240))  # tall marker on the left edge
            p.drawRect(QRectF(r.left(), r.top(), 3, r.height()))
        elif self.hover:
            p.setBrush(QColor(150, 200, 255, 12))
            p.drawPath(shape)
        p.setFont(make_font(10.5, QFont.Weight.Medium))
        p.setPen(QColor("#e4f2ff") if self.active else QColor("#8ea9c0"))
        title = p.fontMetrics().elidedText(self.s["title"], Qt.TextElideMode.ElideRight, int(r.width() - (62 if self.hover else 34)))
        p.drawText(QPointF(r.left() + 18, r.top() + 24), title)
        n = len(self.s["msgs"])
        p.setFont(make_font(8.5))
        p.setPen(QColor("#4d6882"))
        p.drawText(QPointF(r.left() + 18, r.top() + 43), f"{fmt_ts(self.s['ts'])}, {n} message{'s' if n != 1 else ''}")
        if self.hover:  # delete cross
            xr = self._x_rect()
            if self.x_hover:
                p.setPen(NOPEN)
                p.setBrush(QColor(255, 92, 116, 60))
                p.drawPath(chamfer(xr, 6, 0, 6, 0))
            p.setPen(pen_of((255, 120, 140) if self.x_hover else (150, 175, 195), 255, 1.5))
            cx, cy = xr.center().x(), xr.center().y()
            p.drawLine(QPointF(cx - 3.5, cy - 3.5), QPointF(cx + 3.5, cy + 3.5))
            p.drawLine(QPointF(cx - 3.5, cy + 3.5), QPointF(cx + 3.5, cy - 3.5))


class Sidebar(QWidget):
    W = 276
    new_requested = pyqtSignal()
    session_selected = pyqtSignal(int)
    session_delete = pyqtSignal(int)

    def __init__(self, pres):
        super().__init__()
        self.pres, self.active_item, self.cut = pres, None, 30
        self.setFixedWidth(self.W)
        self.panel = QWidget(self)  # fixed-width content that slides under the clip
        self.panel.setFixedWidth(self.W)
        lay = QVBoxLayout(self.panel)
        lay.setContentsMargins(14, 18, 10, 16)
        lay.setSpacing(0)
        self.wordmark = Wordmark(pres)
        lay.addWidget(self.wordmark)
        lay.addSpacing(14)
        self.new_btn = PillButton(pres, "New session", "Ctrl+N")
        self.new_btn.press_sound = None
        self.new_btn.clicked.connect(self.new_requested)
        lay.addWidget(self.new_btn)
        lay.addSpacing(22)
        lbl = QLabel("Chronicle")
        lbl.setObjectName("sideLabel")
        lbl.setFont(make_font(9.5, QFont.Weight.DemiBold, 2.4))
        lbl.setContentsMargins(10, 0, 0, 0)
        lay.addWidget(lbl)
        lay.addSpacing(8)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.scroll.viewport().setAutoFillBackground(False)
        inner = QWidget()
        inner.setObjectName("chronInner")
        self.col = QVBoxLayout(inner)
        self.col.setContentsMargins(0, 0, 4, 0)
        self.col.setSpacing(3)
        self.scroll.setWidget(inner)
        lay.addWidget(self.scroll, 1)
        foot = QLabel("Saved on this device in ~/.airu\nhover a session and hit x to delete it")
        foot.setObjectName("sideFoot")
        foot.setFont(make_font(8.5))
        foot.setContentsMargins(10, 10, 0, 0)
        lay.addWidget(foot)

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self.panel.setGeometry(self.width() - self.W, 0, self.W, self.height())

    def set_sessions(self, sessions, current_id):
        while self.col.count():
            it = self.col.takeAt(0)
            if it.widget():
                it.widget().deleteLater()
        self.active_item = None
        for s in sessions:
            if not s["msgs"] and s["id"] != current_id:
                continue
            item = ChronicleItem(self.pres, s, s["id"] == current_id)
            item.clicked.connect(self.session_selected)
            item.delete_requested.connect(self.session_delete)
            if s["id"] == current_id:
                self.active_item = item
            self.col.addWidget(item)
        self.col.addStretch(1)

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = QRectF(self.rect())
        shape = chamfer(r, self.cut, 0, 0, self.cut * 0.4)
        g = QLinearGradient(0, 0, 0, self.height())
        g.setColorAt(0, QColor(150, 200, 255, 14))
        g.setColorAt(1, QColor(150, 200, 255, 4))
        p.setPen(NOPEN)
        p.setBrush(g)
        p.drawPath(shape)
        line = QLinearGradient(0, 0, 0, self.height())
        line.setColorAt(0, rgba(self.pres.c1, 0))
        line.setColorAt(0.5, rgba(self.pres.c1, 70))
        line.setColorAt(1, rgba(self.pres.c1, 0))
        p.fillRect(QRectF(self.width() - 1, 0, 1, self.height()), line)


# --------------------------------------------------------------------------------------
# Overlays: toast (optionally with an action) and command palette
# --------------------------------------------------------------------------------------
class Toast(QWidget):
    def __init__(self, parent, pres):
        super().__init__(parent)
        self.pres, self.text, self.a = pres, "", 0.0
        self.action, self.cb = None, None
        self._anim = QVariantAnimation(self)
        self._anim.valueChanged.connect(self._set)
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(lambda: self._fade(0.0, 300))
        self.hide()

    def _set(self, v):
        self.a = float(v)
        if self.a <= 0.001 and self._anim.endValue() == 0.0:
            self.hide()
        self.update()

    def _fade(self, to, ms):
        self._anim.stop()
        self._anim.setDuration(ms)
        self._anim.setStartValue(self.a)
        self._anim.setEndValue(to)
        self._anim.start()

    def show_text(self, text, ms=2400, action=None, cb=None):
        self.text, self.action, self.cb = text, action, cb
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, action is None)
        fm = QFontMetrics(make_font(10.5))
        lines = text.split("\n")
        w = max(fm.horizontalAdvance(l) for l in lines) + 60 + (fm.horizontalAdvance(action) + 34 if action else 0)
        h = len(lines) * 22 + 22
        self.setFixedSize(w, h)
        par = self.parentWidget()
        self.move((par.width() - w) // 2, par.M + 64)
        self.raise_()
        self.show()
        self._fade(1.0, 180)
        self._timer.start(ms)

    def _action_rect(self):
        fm = QFontMetrics(make_font(10.5, QFont.Weight.DemiBold))
        aw = fm.horizontalAdvance(self.action or "") + 22
        return QRectF(self.width() - aw - 8, 8, aw, self.height() - 16)

    def mousePressEvent(self, e):
        if self.action and self._action_rect().contains(e.position()):
            cb = self.cb
            self.action = self.cb = None
            self._fade(0.0, 160)
            if cb:
                QTimer.singleShot(0, cb)

    def paintEvent(self, _):
        P = self.pres
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = QRectF(self.rect()).adjusted(1, 1, -1, -1)
        cut = min(14.0, r.height() * 0.34)
        p.setBrush(QColor(4, 8, 14, int(238 * self.a)))
        p.setPen(QPen(rgba(P.c1, 130 * self.a), 1.1))
        p.drawPath(chamfer(r, cut, 0, cut, 0))
        p.setPen(pen_of(P.c1, 230 * self.a, 2.0))
        p.drawLine(QPointF(r.left(), r.top() + cut), QPointF(r.left() + cut, r.top()))
        p.setFont(make_font(10.5))
        p.setPen(rgba((226, 242, 255), 255 * self.a))
        tr = r.adjusted(0, 0, -(self._action_rect().width() + 4) if self.action else 0, 0)
        p.drawText(tr, ALIGN_C, self.text)
        if self.action:
            ar = self._action_rect()
            p.setBrush(rgba(P.c1, 40 * self.a))
            p.setPen(NOPEN)
            p.drawPath(chamfer(ar, 7, 0, 7, 0))
            p.setFont(make_font(10.5, QFont.Weight.DemiBold))
            p.setPen(rgba(lighten(P.c1, 0.5), 255 * self.a))
            p.drawText(ar, ALIGN_C, self.action)


class CommandPalette(QWidget):
    ROW = 44

    def __init__(self, parent, pres):
        super().__init__(parent)
        self.pres, self.items, self.shown, self.sel, self.hover = pres, [], [], 0, -1
        self.entry = QLineEdit(self)
        self.entry.setObjectName("paletteEntry")
        self.entry.setFont(make_font(13.5))
        self.entry.setPlaceholderText("Type a command or a session title")
        self.entry.setFrame(False)
        self.entry.textChanged.connect(self._filter)
        self.entry.installEventFilter(self)
        self.setMouseTracking(True)
        self.hide()

    def open_(self, items):
        self.items = items
        self.setGeometry(self.parentWidget().rect())
        self.entry.clear()
        self._filter("")
        self.raise_()
        self.show()
        self.entry.setFocus()
        sfx("palette")

    def close_(self):
        if self.isVisible():
            self.hide()
            self.parentWidget().dock.entry.setFocus()

    def _filter(self, q):
        words = q.lower().split()
        self.shown = [it for it in self.items if all(w in it[0].lower() for w in words)][:8]
        self.sel, self.hover = 0, -1
        self._layout()
        self.update()

    def _panel(self):
        w = min(620, self.width() - 80)
        rows = max(1, len(self.shown))
        return QRectF((self.width() - w) / 2, max(40, self.height() * 0.16), w, 68 + rows * self.ROW + 10)

    def _layout(self):
        pr = self._panel()
        self.entry.setGeometry(int(pr.x() + 56), int(pr.y() + 16), int(pr.width() - 82), 36)

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._layout()

    def _run(self, i):
        if 0 <= i < len(self.shown):
            fn = self.shown[i][2]
            self.close_()
            QTimer.singleShot(0, fn)

    def eventFilter(self, obj, ev):
        if obj is self.entry and ev.type() == QEvent.Type.KeyPress:
            k, n = ev.key(), len(self.shown)
            if k == Qt.Key.Key_Down and n:
                self.sel = (self.sel + 1) % n
            elif k == Qt.Key.Key_Up and n:
                self.sel = (self.sel - 1) % n
            elif k in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
                self._run(self.sel)
            elif k == Qt.Key.Key_Escape:
                self.close_()
            else:
                return super().eventFilter(obj, ev)
            self.update()
            return True
        return super().eventFilter(obj, ev)

    def _row_at(self, pos):
        pr = self._panel()
        y = pos.y() - (pr.y() + 68)
        if pr.contains(pos) and y >= 0:
            i = int(y // self.ROW)
            return i if i < len(self.shown) else -1
        return -1

    def mouseMoveEvent(self, e):
        i = self._row_at(e.position())
        if i != self.hover:
            self.hover = i
            if i >= 0:
                self.sel = i
            self.update()

    def mousePressEvent(self, e):
        if not self._panel().contains(e.position()):
            self.close_()
        else:
            self._run(self._row_at(e.position()))

    def paintEvent(self, _):
        P = self.pres
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.fillRect(self.rect(), QColor(0, 2, 6, 165))
        pr = self._panel()
        p.setBrush(NOBRUSH)
        for i in range(1, 15):
            p.setPen(QPen(rgba(P.c1, 70 * (1 - i / 15) ** 2), 1.4))
            k = 0.414 * i
            p.drawPath(chamfer(pr.adjusted(-i, -i, i, i), 24 + k, 5 + k, 24 + k, 5 + k))
        p.setBrush(QColor(3, 6, 11, 250))
        p.setPen(QPen(rgba(P.c1, 140), 1.1))
        p.drawPath(chamfer(pr, 24, 5, 24, 5))
        p.setPen(pen_of(P.c1, 230, 2.2))
        p.drawLine(QPointF(pr.left(), pr.top() + 24), QPointF(pr.left() + 24, pr.top()))
        p.drawLine(QPointF(pr.right() - 24, pr.bottom()), QPointF(pr.right(), pr.bottom() - 24))
        draw_sigil(p, P.c1, pr.x() + 36, pr.y() + 34, 8, P.t)
        p.setPen(QPen(rgba(P.c1, 45), 1))
        p.drawLine(QPointF(pr.x() + 16, pr.y() + 62), QPointF(pr.right() - 16, pr.y() + 62))
        if not self.shown:
            p.setFont(make_font(11))
            p.setPen(QColor("#4a6580"))
            p.drawText(QRectF(pr.x(), pr.y() + 68, pr.width(), self.ROW), ALIGN_C, "No match")
            return
        for i, (name, hint, _fn) in enumerate(self.shown):
            r = QRectF(pr.x() + 10, pr.y() + 68 + i * self.ROW, pr.width() - 20, self.ROW)
            if i == self.sel:
                g = QLinearGradient(r.left(), 0, r.right(), 0)
                g.setColorAt(0, rgba(P.c1, 46))
                g.setColorAt(1, rgba(P.c1, 6))
                p.setPen(NOPEN)
                p.setBrush(g)
                p.drawPath(chamfer(r.adjusted(0, 3, 0, -3), 0, 0, 10, 0))
                p.setBrush(rgba(P.c1, 240))
                p.drawRect(QRectF(r.left(), r.top() + 3, 2.5, r.height() - 6))
            p.setFont(make_font(11.5, QFont.Weight.Medium))
            p.setPen(QColor("#ecf6ff") if i == self.sel else QColor("#9db8cd"))
            p.drawText(QRectF(r.left() + 18, r.top(), r.width() * 0.62, r.height()), ALIGN_VL, name)
            p.setFont(make_font(9))
            p.setPen(QColor("#53728a"))
            p.drawText(QRectF(r.left(), r.top(), r.width() - 16, r.height()), ALIGN_VR, hint)


# --------------------------------------------------------------------------------------
# The floating Ai-ru orb: lives in the corner whenever the app is in the background
# --------------------------------------------------------------------------------------
class MiniOrb(QWidget):
    S, CAP = 160, 440
    BTNS = (("ask", math.radians(150)), ("mic", math.radians(100)), ("open", math.radians(50)))

    def __init__(self, pres):
        super().__init__(None, Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint | Qt.WindowType.Tool)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        mac = getattr(Qt.WidgetAttribute, "WA_MacAlwaysShowToolWindow", None)
        if mac is not None:
            self.setAttribute(mac)
        self.setMouseTracking(True)
        self.pres = pres
        self.cloud = DustCloud(650)
        self.hover, self.hv, self.btn_hover, self.mic_on = False, 0.0, -1, False
        noop = lambda *a: None
        self.on_restore = self.on_quit = self.on_mute = self.on_mic = self.on_submit = self.on_mode = noop
        self.saved_right = self.saved_y = None
        self.mode, self._toast = "orb", ""
        self._press, self._press_btn, self._moved, self._placed = None, -1, False, False
        self.setFixedSize(self.S, self.S)
        self.entry = QLineEdit(self)
        self.entry.setObjectName("orbEntry")
        self.entry.setFont(make_font(10.5))
        self.entry.setPlaceholderText("Ask Ai-ru...")
        self.entry.setFrame(False)
        self.entry.hide()
        self.entry.returnPressed.connect(self._submit)
        self.entry.installEventFilter(self)
        self._fade = QPropertyAnimation(self, b"windowOpacity", self)
        self._tt = QTimer(self)
        self._tt.setSingleShot(True)
        self._tt.timeout.connect(self._end_toast)

    # --- lifecycle
    def right_edge(self):
        return self.x() + self.width()

    def appear(self, msg=None):
        if self.isVisible():
            if msg:
                self.show_toast(msg)
            return
        geo = QApplication.primaryScreen().availableGeometry()
        if not self._placed:
            right = self.saved_right if self.saved_right is not None else geo.right() - 22
            y = self.saved_y if self.saved_y is not None else geo.bottom() - self.height() - 22
            right = int(clamp(right, geo.left() + self.width(), geo.right()))
            y = int(clamp(y, geo.top(), geo.bottom() - self.height()))
            self.move(right - self.width(), y)
            self._placed = True
        self.setWindowOpacity(0.0)
        self.show()
        self._fade.stop()
        self._fade.setDuration(260)
        self._fade.setStartValue(0.0)
        self._fade.setEndValue(1.0)
        self._fade.start()
        if msg:
            self.show_toast(msg)

    def vanish(self):
        if not self.isVisible():
            return
        self._tt.stop()
        self.close_ask()
        self._end_toast()
        self.hide()

    def set_mic(self, on):
        self.mic_on = on

    # --- capsule (toast / quick ask) to the left of the orb
    def _set_width(self, w):
        if self.width() == w:
            return
        right = self.right_edge()
        self.setFixedSize(w, self.S)
        self.move(right - w, self.y())

    def _capsule(self):
        cx = self.width() - self.S / 2
        return QRectF(10, self.S / 2 - 27, cx - self.S * 0.36 * 1.3 - 10, 54)

    def show_toast(self, text):
        if not self.isVisible() or self.mode == "ask":
            return
        text = " ".join(text.split())
        self._toast = text[:110] + ("..." if len(text) > 110 else "")
        self._set_width(self.CAP)
        self._tt.start(4800)

    def _end_toast(self):
        self._toast = ""
        if self.mode != "ask":
            self._set_width(self.S)
        self.update()

    def open_ask(self):
        if not self.isVisible():
            return
        self._tt.stop()
        self._toast, self.mode = "", "ask"
        self._set_width(self.CAP)
        r = self._capsule().adjusted(26, 8, -16, -8)
        self.entry.setGeometry(r.toRect())
        self.entry.clear()
        self.entry.show()
        self.activateWindow()
        self.raise_()
        self.entry.setFocus()

    def close_ask(self):
        if self.mode != "ask":
            return
        self.mode = "orb"
        self.entry.hide()
        self.entry.clear()
        if not self._toast:
            self._set_width(self.S)

    def _submit(self):
        text = self.entry.text().strip()
        self.close_ask()
        if text:
            self.on_submit(text)
            self.show_toast("Sent. Ai-ru is on it")

    def eventFilter(self, obj, ev):
        if obj is self.entry and ev.type() == QEvent.Type.FocusOut and self.mode == "ask":
            QTimer.singleShot(150, lambda: self.close_ask() if not self.entry.hasFocus() else None)
        return super().eventFilter(obj, ev)

    def keyPressEvent(self, e):
        if e.key() == Qt.Key.Key_Escape:
            self.close_ask()
        else:
            super().keyPressEvent(e)

    def tick(self, dt):
        self.hv = approach(self.hv, 1.0 if (self.hover or self.mode == "ask") else 0.0, 12, dt)

    # --- geometry helpers
    def _center(self):
        return self.width() - self.S / 2, self.S / 2

    def _btn_pos(self, i):
        cx, cy = self._center()
        rb = self.S * 0.36 * 1.17
        a = self.BTNS[i][1]
        return cx + rb * math.cos(a), cy + rb * math.sin(a)

    def _btn_at(self, pos):
        if self.hv < 0.4:
            return -1
        for i in range(len(self.BTNS)):
            bx, by = self._btn_pos(i)
            if math.hypot(pos.x() - bx, pos.y() - by) < 13:
                return i
        return -1

    def _act(self, name):
        if name == "mic":
            self.on_mic()
        elif name == "ask":
            self.open_ask()
        else:
            self.on_restore()

    # --- input
    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            self._press, self._moved = e.globalPosition(), False
            self._press_btn = self._btn_at(e.position())

    def mouseMoveEvent(self, e):
        cx, cy = self._center()
        self.hover = math.hypot(e.position().x() - cx, e.position().y() - cy) < self.S * 0.36 * 1.17 + 14
        self.btn_hover = self._btn_at(e.position())
        if self._press is not None and self._press_btn < 0 and not self._moved:
            d = e.globalPosition() - self._press
            if abs(d.x()) + abs(d.y()) > 6:
                self._moved = True
                h = self.windowHandle()
                if h:
                    h.startSystemMove()
                self._press = None
        self.update()

    def mouseReleaseEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton and self._press is not None and not self._moved:
            if self._press_btn >= 0:
                if self._btn_at(e.position()) == self._press_btn:
                    self._act(self.BTNS[self._press_btn][0])
            elif self.mode != "ask":
                self.on_restore()
        self._press, self._press_btn = None, -1

    def moveEvent(self, e):
        super().moveEvent(e)
        if self._placed and self.isVisible():
            self.saved_right, self.saved_y = self.right_edge(), self.y()

    def leaveEvent(self, e):
        self.hover, self.btn_hover = False, -1
        self.update()

    def contextMenuEvent(self, e):
        m = QMenu(self)
        add_action(m, "Open Ai-ru", self.on_restore)
        add_action(m, "Quick ask", self.open_ask)
        add_action(m, "Mic off" if self.mic_on else "Mic on", self.on_mic)
        add_action(m, "Toggle sound", self.on_mute)
        add_action(m, "Cycle orb mode", self.on_mode)
        m.addSeparator()
        add_action(m, "Quit", self.on_quit)
        m.exec(e.globalPos())

    # --- painting
    def paintEvent(self, _):
        P = self.pres
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        c1, c2, t, en = P.c1, P.c2, P.t, P.energy
        cx, cy = self._center()
        Ro = self.S * 0.36 * (1.05 if self.hover else 1.0)

        if self.mode == "ask" or self._toast:
            r = self._capsule()
            cut = r.height() * 0.34
            p.setBrush(QColor(4, 8, 14, 236))
            p.setPen(QPen(rgba(c1, 130), 1.1))
            p.drawPath(chamfer(r, cut, 0, cut, 0))
            p.setPen(pen_of(c1, 230, 2.0))
            p.drawLine(QPointF(r.left(), r.top() + cut), QPointF(r.left() + cut, r.top()))
            if self.mode != "ask":
                p.setFont(make_font(9.5))
                p.setPen(QColor(222, 240, 255))
                opt = QTextOption(ALIGN_VL)
                opt.setWrapMode(QTextOption.WrapMode.WordWrap)
                p.drawText(r.adjusted(26, 4, -16, -4), self._toast, opt)

        p.translate(cx, cy + 2.5 * math.sin(t * 1.4))  # gentle float
        p.setCompositionMode(ADD)
        p.setPen(NOPEN)
        g = QRadialGradient(0, 0, Ro * 1.35)
        g.setColorAt(0, rgba(c2, 90 + 90 * en))
        g.setColorAt(0.5, rgba(c2, 28 + 34 * en))
        g.setColorAt(1, rgba(c2, 0))
        p.setBrush(g)
        p.drawEllipse(QPointF(0, 0), Ro * 1.35, Ro * 1.35)
        p.setCompositionMode(SRC)

        p.setBrush(NOBRUSH)
        if P.state != "IDLE":  # hexagonal ripples
            for k in range(2):
                pr = (t * 0.8 + k * 0.5) % 1.0
                p.setPen(QPen(rgba(c1, 170 * (1 - pr) ** 1.6), 1.6))
                p.drawPath(hex_path(0, 0, Ro * (0.7 + 0.5 * pr)))
        rot, R1 = P.rot * 0.15, Ro * 1.12  # hex frame with alternating bright edges
        verts = [QPointF(R1 * math.cos(rot - math.pi / 2 + i * TAU / 6), R1 * math.sin(rot - math.pi / 2 + i * TAU / 6))
                 for i in range(6)]
        for i in range(6):
            bright = i % 2 == 0
            p.setPen(pen_of(c1, 200 if bright else 55, 2.0 if bright else 1.0))
            p.drawLine(verts[i], verts[(i + 1) % 6])

        self.cloud.draw(p, P, Ro * 0.8, size=0.95)

        # hover buttons: ask / mic / open, as small hexagons
        p.resetTransform()
        a = self.hv
        if a > 0.02:
            for i, (name, _ang) in enumerate(self.BTNS):
                bx, by = self._btn_pos(i)
                hov = i == self.btn_hover
                active = name == "mic" and self.mic_on
                p.setBrush(rgba(c1, 150 * a) if active else QColor(5, 9, 15, int(236 * a)))
                p.setPen(QPen(rgba(c1, (130 + 125 * hov) * a), 1.2))
                p.drawPath(hex_path(bx, by, 12.0 if hov else 11.0))
                p.save()
                p.translate(bx, by)
                p.setPen(pen_of(lighten(c1, 0.6) if not active else (5, 5, 8), 255 * a, 1.3))
                p.setBrush(NOBRUSH)
                if name == "mic":
                    p.drawRoundedRect(QRectF(-2, -5, 4, 7), 2, 2)
                    p.drawArc(QRectF(-4, -3, 8, 8), 180 * 16, 180 * 16)
                    p.drawLine(QPointF(0, 5), QPointF(0, 6.5))
                elif name == "ask":
                    p.setBrush(rgba(lighten(c1, 0.6), 255 * a))
                    p.setPen(NOPEN)
                    for dx in (-3.6, 0, 3.6):
                        p.drawEllipse(QPointF(dx, 0), 1.2, 1.2)
                else:
                    p.drawLine(QPointF(-3, 3), QPointF(3, -3))
                    p.drawPolyline(QPolygonF([QPointF(-0.5, -3), QPointF(3, -3), QPointF(3, 0.5)]))
                p.restore()


# --------------------------------------------------------------------------------------
# Background Worker Threads to execute Ollama Brain / Tool Calls & Model Warm-up safely
# --------------------------------------------------------------------------------------
class WarmupWorker(QThread):
    """Silently pre-loads the Ollama 7B model into GPU VRAM in background without blocking the UI."""

    def run(self):
        try:
            import ollama
            import config

            ollama.chat(
                model=config.OLLAMA_MODEL,
                messages=[{"role": "user", "content": "ping"}],
                options={"num_predict": 1, "temperature": 0.0},
            )
        except Exception:
            pass


class BackendWorker(QThread):
    response_ready = pyqtSignal(str)
    thought_ready = pyqtSignal(str)
    status_ready = pyqtSignal(str)

    def __init__(self, user_text: str, chat_history: list[dict] | None = None):
        super().__init__()
        self.user_text = user_text
        self.chat_history = chat_history
        self.brain = Brain()

    def run(self):
        try:
            self.status_ready.emit("THINKING")
            self.thought_ready.emit("Querying local Ollama brain...")
            response = self.brain.process_user_query(self.user_text, chat_history=self.chat_history)
            self.status_ready.emit("SPEAKING")
            self.response_ready.emit(response)
        except Exception as e:
            self.status_ready.emit("IDLE")
            self.response_ready.emit(f"Error processing query: {e}")


# --------------------------------------------------------------------------------------
# The main window
# --------------------------------------------------------------------------------------
class AiruGUI(QWidget):
    _status_sig = pyqtSignal(str)
    _message_sig = pyqtSignal(str, str)
    _thought_sig = pyqtSignal(str)
    _notify_sig = pyqtSignal(str)
    _wake_sig = pyqtSignal()

    def __init__(self):
        super().__init__()
        global SOUND
        QApplication.setQuitOnLastWindowClosed(False)
        if "--background" in sys.argv and sys.argv and sys.argv[0]:
            try:  # login items start in an unrelated folder; relative paths in the backend need the script's own
                os.chdir(os.path.dirname(os.path.abspath(sys.argv[0])))
            except Exception:
                pass
        load_custom_fonts()
        self.M, self.cut = 22, 30
        settings = load_json(SETTINGS_FILE, {})
        self.theme = settings.get("theme", "Neon") if settings.get("theme") in THEMES else "Neon"
        self.orb_mode = settings.get("orb_mode") if settings.get("orb_mode") in ORB_MODES else "background"
        self.sidebar_pref = bool(settings.get("sidebar", True))
        self.autostart = bool(settings.get("autostart", True))
        STATE_DEFS["IDLE"].update(c1=THEMES[self.theme][0], c2=THEMES[self.theme][1])

        self.setWindowFlags(Qt.WindowType.Window | Qt.WindowType.FramelessWindowHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setWindowTitle(APP_NAME)
        self.icon = make_app_icon(THEMES[self.theme][0])
        self.setWindowIcon(self.icon)
        QApplication.instance().setStyleSheet(build_qss(THEMES[self.theme][0]))
        scr = QApplication.primaryScreen().availableGeometry()
        self.setMinimumSize(900, 600)
        self.resize(min(1240, int(scr.width() * 0.92)), min(820, int(scr.height() * 0.92)))

        self.pres = Presence()
        for i in range(3):
            self.pres.c1[i], self.pres.c2[i] = THEMES[self.theme][0][i], THEMES[self.theme][1][i]
        self.sound = SoundEngine()
        self.sound.enabled = bool(settings.get("sound", True))
        SOUND = self.sound
        self._on_submit = self._on_mic = self._on_new = None
        self.pinned, self.presence_mode, self._opacity_i = False, False, 0
        self.mic_on, self.backend_state, self._quitting = False, "IDLE", False
        self.stage, self.stage_target, self._chat_h = 0.0, False, 200
        self._pokes, self._undo, self._party_token, self._bg_hinted = [], None, 0, False
        self._party_t, self._party_i = 0.0, 0
        self._idle_since, self._next_quip, self._frame_n = time.time(), random.uniform(45, 90), 0

        self.sessions = load_sessions()[:200]
        self._sid = max([s["id"] for s in self.sessions], default=0)
        self.current = None

        root = QHBoxLayout(self)
        self.root = root
        root.setContentsMargins(self.M, self.M, self.M, self.M)
        root.setSpacing(0)
        self.sidebar = Sidebar(self.pres)
        root.addWidget(self.sidebar)
        self._sb_w = float(Sidebar.W if self.sidebar_pref else 0)
        self._sb_end = int(self._sb_w)
        if not self.sidebar_pref:
            self.sidebar.setFixedWidth(0)
            self.sidebar.hide()

        right = QVBoxLayout()
        right.setContentsMargins(34, 8, 26, 14)
        right.setSpacing(0)
        root.addLayout(right, 1)

        bar = QHBoxLayout()
        bar.setSpacing(2)
        bar.setContentsMargins(0, 6, 0, 6)
        self.btn_menu = IconButton(self.pres, "menu")
        self.btn_menu.setToolTip("Slide the sidebar (Ctrl+B)")
        self.session_title = QLabel("New session")
        self.session_title.setObjectName("sessionTitle")
        self.session_title.setFont(make_font(11.5, QFont.Weight.Normal, 0.6))
        bar.addWidget(self.btn_menu)
        bar.addSpacing(8)
        bar.addWidget(self.session_title, 1)
        self.btn_search = IconButton(self.pres, "search")
        self.btn_search.setToolTip("Command palette (Ctrl+K)")
        self.snd_btn = IconButton(self.pres, "sound")
        self.snd_btn.setToolTip("Sound feedback (Ctrl+Shift+S)")
        self.btn_full = IconButton(self.pres, "full")
        self.btn_full.setToolTip("Fullscreen (F11)")
        self.min_btn = IconButton(self.pres, "min")
        self.min_btn.setToolTip("Minimize to the Ai-ru orb")
        self.close_btn = IconButton(self.pres, "close")
        self.close_btn.setToolTip("Send to background (Ctrl+Q quits)")
        self.icon_btns = (self.btn_menu, self.btn_search, self.snd_btn, self.btn_full, self.min_btn, self.close_btn)
        for b in self.icon_btns[1:]:
            bar.addWidget(b)
        barw = QWidget()
        barw.setFixedHeight(48)
        barw.setLayout(bar)
        right.addWidget(barw)

        self.orb_view = OrbView(self.pres)
        self.wave = WaveLine(self.pres)
        self.strip = StateStrip(self.pres)
        self.live_caption = QLabel("")
        self.live_caption.setObjectName("liveCaption")
        self.live_caption.setFont(make_font(15, QFont.Weight.Light, 0.3))
        self.live_caption.setWordWrap(True)
        self.live_caption.setAlignment(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop)
        self.live_caption.setContentsMargins(70, 0, 70, 8)
        self.live_caption.setMinimumHeight(64)
        self.live_caption.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)
        self.live_caption.hide()
        self.chat = ChatView(self.pres)
        self.chat.setMinimumHeight(0)
        self.dock = InputDock(self.pres)
        right.addWidget(self.orb_view, 6)
        right.addWidget(self.wave)
        right.addWidget(self.strip)
        right.addWidget(self.live_caption)
        right.addWidget(self.chat, 5)
        right.addSpacing(6)
        right.addWidget(self.dock)

        self.grip = QSizeGrip(self)
        self.grip.setFixedSize(18, 18)
        self.toast = Toast(self, self.pres)
        self.palette = CommandPalette(self, self.pres)

        self.orb = MiniOrb(self.pres)
        self.orb.saved_right, self.orb.saved_y = settings.get("orb_right"), settings.get("orb_y")
        self.orb.on_restore, self.orb.on_quit, self.orb.on_mute = self._restore, self._quit, self._toggle_sound
        self.orb.on_mic, self.orb.on_submit, self.orb.on_mode = self._toggle_mic, self._submit_text, self._cycle_orb_mode
        self._build_tray()

        self._stage_anim = QVariantAnimation(self)
        self._stage_anim.setDuration(560)
        self._stage_anim.setEasingCurve(QEasingCurve.Type.InOutCubic)
        self._stage_anim.valueChanged.connect(self._stage_tick)
        self._sb_anim = QVariantAnimation(self)
        self._sb_anim.setDuration(280)
        self._sb_anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._sb_anim.valueChanged.connect(self._sb_tick)
        self._sb_anim.finished.connect(self._sb_done)
        self._orb_timer = QTimer(self)
        self._orb_timer.setSingleShot(True)
        self._orb_timer.timeout.connect(self._sync_orb)
        QApplication.instance().applicationStateChanged.connect(lambda _s: self._orb_timer.start(250))

        # Default wiring to actual backend handlers
        self.set_callbacks(
            on_submit=self._handle_backend_query,
            on_mic=lambda active: None if active else self._toast("Microphone listening toggled.")
        )

        # wiring
        self.sidebar.new_requested.connect(self._new_session)
        self.sidebar.session_selected.connect(self._switch_session)
        self.sidebar.session_delete.connect(self._delete_session_id)
        self.orb_view.interacted.connect(self._orb_touched)
        self.dock.entry.returnPressed.connect(self._submit)
        self.dock.entry.textEdited.connect(self._typing)
        self.dock.send.clicked.connect(self._submit)
        self.dock.mic.clicked.connect(self._mic_clicked)
        self.btn_menu.clicked.connect(self._toggle_sidebar)
        self.btn_search.clicked.connect(self._open_palette)
        self.snd_btn.clicked.connect(self._toggle_sound)
        self.btn_full.clicked.connect(self._toggle_fullscreen)
        self.min_btn.clicked.connect(self.showMinimized)
        self.close_btn.clicked.connect(self._to_background)
        self._status_sig.connect(self._apply_status, Qt.ConnectionType.QueuedConnection)
        self._message_sig.connect(self._do_add_message, Qt.ConnectionType.QueuedConnection)
        self._thought_sig.connect(self._show_thought, Qt.ConnectionType.QueuedConnection)
        self._notify_sig.connect(self._do_notify, Qt.ConnectionType.QueuedConnection)
        self._wake_sig.connect(self._do_wake, Qt.ConnectionType.QueuedConnection)
        for keys, fn in (("Ctrl+K", self._open_palette), ("F11", self._toggle_fullscreen), ("F2", self._toggle_presence),
                         ("Ctrl+B", self._toggle_sidebar), ("Ctrl+T", self._cycle_theme), ("Ctrl+E", self._export),
                         ("Ctrl+N", self._new_session), ("Ctrl+M", self._toggle_mic), ("Ctrl+Space", self._toggle_mic),
                         ("Ctrl+Shift+S", self._toggle_sound), ("Ctrl+Shift+O", self._cycle_orb_mode),
                         ("Ctrl+Shift+P", self._toggle_party), ("Ctrl+Q", self._quit), ("Esc", self._escape)):
            QShortcut(QKeySequence(keys), self).activated.connect(fn)

        self._create_session()
        self._refresh_sessions()
        self._push_orb_theme()
        self.orb_view.set_state("IDLE")
        if self.orb_view.view is None:
            QTimer.singleShot(1200, lambda: self._toast("Orb: using the built-in cloud. " + self.orb_view.reason, 7000))
        if self.autostart:
            set_autostart(True)

        self._last = time.perf_counter()
        self._fps = 60.0
        self._clock = QTimer(self)
        self._clock.setTimerType(Qt.TimerType.PreciseTimer)
        self._clock.timeout.connect(self._frame)
        self._clock.start(16)
        self.move(scr.center() - self.rect().center())
        self.dock.entry.setFocus()
        self._orb_timer.start(900)

        # Trigger silent background warm-up of Ollama 7B model
        QTimer.singleShot(400, self._trigger_warmup)

    def _trigger_warmup(self):
        self._warmup_worker = WarmupWorker()
        self._warmup_worker.start()

    def _handle_backend_query(self, text):
        self.update_status("THINKING")
        chat_history = []
        if hasattr(self, "current") and "msgs" in self.current:
            past_msgs = self.current["msgs"][:-1] if self.current["msgs"] else []
            for sender, msg in past_msgs:
                role = "user" if is_user(sender) else "assistant"
                chat_history.append({"role": role, "content": msg})

        self.worker = BackendWorker(text, chat_history=chat_history)
        self.worker.status_ready.connect(self.update_status)
        self.worker.thought_ready.connect(self.add_thought)
        self.worker.response_ready.connect(self._on_backend_response)
        self.worker.start()

    def _on_backend_response(self, response_text):
        self.add_message("ai-ru", response_text)
        self.update_status("IDLE")

    # ================= backend contract =================
    def update_status(self, state: str):
        self._status_sig.emit(str(state))

    def add_message(self, sender: str, text: str):
        self._message_sig.emit(str(sender), str(text))

    def set_callbacks(self, on_submit=None, on_mic=None, on_new_session=None):
        self._on_submit, self._on_mic, self._on_new = on_submit, on_mic, on_new_session

    def set_audio_level(self, level: float):
        self.pres.push_level(level)

    def add_thought(self, text: str):
        self._thought_sig.emit(str(text))

    def notify(self, text: str):
        self._notify_sig.emit(str(text))

    def wake(self):
        """Thread-safe. Call when your wake-word detector hears 'Ai-ru': turns the mic on like the button."""
        self._wake_sig.emit()

    def start(self):
        """Show the window, or go straight to the floating orb when launched at login (--background)."""
        if "--background" in sys.argv:
            self.hide()
            self._to_background(quiet=True)
        else:
            self.show()

    def _do_wake(self):
        if self.mic_on:
            return
        self.pres.kick(1.0, shock=True)
        self.dock.mic.setChecked(True)
        self._mic_clicked()

    # ================= tray =================
    def _build_tray(self):
        self.tray = None
        if not QSystemTrayIcon.isSystemTrayAvailable():
            return
        self.tray = QSystemTrayIcon(self.icon, self)
        self.tray.setToolTip(APP_NAME)
        menu = QMenu()
        add_action(menu, "Show Ai-ru", self._restore)
        add_action(menu, "Toggle mic", self._toggle_mic)
        add_action(menu, "Toggle sound", self._toggle_sound)
        add_action(menu, "Cycle orb mode", self._cycle_orb_mode)
        menu.addSeparator()
        add_action(menu, "Quit", self._quit)
        self._tray_menu = menu
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(lambda r: self._restore() if r == QSystemTrayIcon.ActivationReason.Trigger else None)
        self.tray.show()

    # ================= orb visibility =================
    def _want_orb(self):
        hidden = (not self.isVisible()) or self.isMinimized()
        if self.orb_mode == "always":
            return True
        if self.orb_mode == "minimized":
            return hidden
        return hidden or not self.isActiveWindow()

    def _sync_orb(self, msg=None):
        if self._quitting:
            return
        if self._want_orb():
            self.orb.appear(msg)
        else:
            self.orb.vanish()

    def _cycle_orb_mode(self):
        self.orb_mode = ORB_MODES[(ORB_MODES.index(self.orb_mode) + 1) % len(ORB_MODES)]
        sfx("tick")
        self._toast(ORB_MODE_TEXT[self.orb_mode], 3200)
        self._save_settings()
        self._sync_orb()

    def _to_background(self, quiet=False):
        if self.isFullScreen():
            self._set_chrome(False)
            self.showNormal()
        self.hide()
        if not quiet:
            sfx("whoosh")
        self._sync_orb(None if quiet else "Ai-ru is running in the background")
        if self.tray and not self._bg_hinted and not quiet:
            self._bg_hinted = True
            self.tray.showMessage(APP_NAME, "Still here. Click the orb or this icon to bring me back.", self.icon, 3500)

    # ================= orb page =================
    def _push_orb_theme(self):
        c1, c2 = THEMES[self.theme]
        self.orb_view.set_theme(c1, c2)
        d = STATE_DEFS[self.pres.state]
        self.orb_view.set_aurora(d["c1"], d["c2"])

    def _orb_state(self, s):
        d = STATE_DEFS[s]
        self.orb_view.set_aurora(d["c1"], d["c2"])
        if not self.pres.party:
            self.orb_view.set_state(s)

    def _orb_touched(self, tap):
        if tap:
            self._poke()
        if not self.palette.isVisible():
            self.dock.entry.setFocus()

    # ================= state, mic, messages =================
    def _refresh_state(self, silent=False):
        s = "LISTENING" if self.mic_on else self.backend_state
        if s == self.pres.state:
            return
        self.pres.set_state(s)
        self._orb_state(s)
        self._idle_since = time.time()
        if not silent:
            sfx(s.lower())
        if self.orb.isVisible() and s != "IDLE":
            self.orb.show_toast(STATE_DEFS[s]["label"].capitalize() + "...")

    def _apply_status(self, state):
        s = normalize_state(state)
        if s == self.backend_state:
            return
        self.backend_state = s
        if s == "LISTENING" and not self.mic_on:
            self._set_mic_ui(True)
        elif s != "LISTENING" and self.mic_on:
            self._set_mic_ui(False)
        self._refresh_state()

    def _set_mic_ui(self, on):
        self.mic_on = on
        self.dock.mic.setChecked(on)
        self.dock.entry.setPlaceholderText("Listening... speak, or type" if on else "Message Ai-ru")
        self.orb.set_mic(on)
        self._update_stage()
        self._update_sidebar()
        self._update_caption()

    def _toggle_mic(self):
        self.dock.mic.setChecked(not self.dock.mic.isChecked())
        self._mic_clicked()

    def _mic_clicked(self):
        on = self.dock.mic.isChecked()
        sfx("crackle" if on else "mic_off")
        self.pres.kick(0.8, shock=True)
        if not on and self.backend_state == "LISTENING":
            self.backend_state = "IDLE"
        self._set_mic_ui(on)
        self._refresh_state(silent=True)
        safe_call(self._on_mic, on)

    def _show_thought(self, text):
        self.strip.set_thought(text)

    def _do_notify(self, text):
        if self.orb.isVisible():
            self.orb.show_toast(text)
        if self.tray and (not self.isVisible() or self.isMinimized()):
            self.tray.showMessage(APP_NAME, " ".join(text.split())[:200], self.icon, 4000)
        else:
            self._toast(text)

    def _typing(self, _):
        sfx("key")
        self._idle_since = time.time()
        self.pres.kick(0.3)

    def _submit(self):
        text = self.dock.entry.text()
        self.dock.entry.clear()
        self._submit_text(text)

    def _submit_text(self, text):
        text = text.strip()
        if not text:
            return
        sfx("send")
        self.pres.kick(1.0, shock=True)
        self._do_add_message("user", text, quiet=True)
        if self._on_submit:
            safe_call(self._on_submit, text)
        else:
            self._handle_backend_query(text)

    def _do_add_message(self, sender, text, quiet=False):
        s = self.current
        s["msgs"].append((sender, text))
        s["ts"] = time.time()
        if s["title"] == "New session":
            if is_user(sender):
                s["title"] = " ".join(text.split())[:48] or "New session"
            else:
                s["title"], s["auto"] = "Session " + time.strftime("%H:%M"), True
        elif s.get("auto") and is_user(sender):
            s["title"], s["auto"] = " ".join(text.split())[:48] or s["title"], False
        self.chat.add_message(sender, text, animate=True)
        self._idle_since = time.time()
        if not quiet:
            sfx("tick" if is_user(sender) else "recv")
        if not is_user(sender):
            self.pres.kick(0.5, shock=True)
            self._update_caption()
            if self.orb.isVisible():
                self.orb.show_toast(text)
            if self.tray and (not self.isVisible() or self.isMinimized()):
                self.tray.showMessage(APP_NAME, " ".join(text.split())[:200], self.icon, 4500)
        self._refresh_sessions()
        self._save()

    def _update_caption(self):
        if self.mic_on:
            self.live_caption.setText("Listening. Go ahead...")
            return
        last = next((t for s, t in reversed(self.current["msgs"]) if not is_user(s)), "") if self.current else ""
        t = " ".join(last.split())
        self.live_caption.setText(t if len(t) <= 320 else t[:317] + "...")

    # ================= sessions =================
    def _save(self):
        save_json(SESSIONS_FILE, [{"id": s["id"], "title": s["title"], "ts": s["ts"], "msgs": s["msgs"]}
                                  for s in self.sessions if s["msgs"]])

    def _create_session(self):
        if self.current is not None and not self.current["msgs"]:
            return
        self._sid += 1
        self.current = {"id": self._sid, "title": "New session", "ts": time.time(), "msgs": []}
        self.sessions.insert(0, self.current)

    def _refresh_sessions(self):
        self.sidebar.set_sessions(self.sessions, self.current["id"])
        self.session_title.setText(self.current["title"])

    def _load_current(self):
        self.sessions = [x for x in self.sessions if x["msgs"] or x is self.current]
        self.chat.clear()
        for sender, text in self.current["msgs"]:
            self.chat.add_message(sender, text, animate=False)
        self._update_caption()
        self._refresh_sessions()

    def _new_session(self):
        sfx("new")
        if self.current["msgs"]:
            self.current = None
            self._create_session()
            self._load_current()
            safe_call(self._on_new)
        self.dock.entry.setFocus()

    def _switch_session(self, sid):
        target = next((s for s in self.sessions if s["id"] == sid), None)
        if target is None or target is self.current:
            return
        sfx("tick")
        self.current = target
        self._load_current()

    def _delete_session_id(self, sid):
        s = next((x for x in self.sessions if x["id"] == sid), None)
        if s is None:
            return
        self._undo = (s, self.sessions.index(s))
        was_current = s is self.current
        self.sessions.remove(s)
        if was_current:
            self.current = self.sessions[0] if self.sessions else None
            if self.current is None:
                self._create_session()
            self._load_current()
        else:
            self._refresh_sessions()
        self._save()
        sfx("pop")
        self._toast("Session deleted", 6000, action="Undo", cb=self._undo_delete)

    def _delete_session(self):
        self._delete_session_id(self.current["id"])

    def _undo_delete(self):
        if not self._undo:
            self._toast("Nothing to undo")
            return
        s, idx = self._undo
        self._undo = None
        if s not in self.sessions:
            self.sessions.insert(min(idx, len(self.sessions)), s)
        self.current = s
        self._load_current()
        self._save()
        sfx("new")
        self._toast("Session restored")

    def _delete_all(self):
        self.sessions, self.current, self._undo = [], None, None
        self._create_session()
        self._load_current()
        self._save()
        sfx("pop")
        self._toast("All sessions deleted")

    def _clear_session(self):
        self.current["msgs"].clear()
        self.current["title"] = "New session"
        self._load_current()
        self._save()
        self._toast("Session cleared")

    def _export(self):
        if not self.current["msgs"]:
            self._toast("Nothing to export yet")
            return
        default = str(HOME / f"airu-session-{time.strftime('%Y%m%d-%H%M', time.localtime(self.current['ts']))}.md")
        path, _ = QFileDialog.getSaveFileName(self, "Export session", default, "Markdown (*.md)")
        if not path:
            return
        lines = [f"# {self.current['title']}", f"*{fmt_ts(self.current['ts'])}*", ""]
        for sender, text in self.current["msgs"]:
            lines += [f"**{'You' if is_user(sender) else APP_NAME}:** {text}", ""]
        try:
            Path(path).write_text("\n".join(lines), encoding="utf-8")
            self._toast("Exported to " + Path(path).name)
        except Exception as ex:
            self._toast(f"Export failed: {ex}")

    def _copy_last(self):
        last = next((t for s, t in reversed(self.current["msgs"]) if not is_user(s)), "")
        if last:
            QApplication.clipboard().setText(last)
            self._toast("Copied Ai-ru's last reply")
        else:
            self._toast("No reply to copy")

    # ================= playful stuff =================
    def _poke(self):
        now = time.time()
        self._pokes = [t for t in self._pokes if now - t < 4.0] + [now]
        self._idle_since = now
        sfx("zap")
        self.pres.kick(0.9, shock=True)
        if len(self._pokes) >= 7:
            self._pokes = []
            self.strip.set_thought("okay okay, party time")
            self._set_party(True, 14000)
        else:
            self.strip.set_thought(random.choice(POKE_QUIPS))

    def _set_party(self, on, ms=None):
        self._party_token += 1
        tok = self._party_token
        self.pres.party = on
        if on:
            sfx("whoosh")
            self._toast("Party mode. Ctrl+Shift+P to stop", 2600)
            if ms:
                QTimer.singleShot(ms, lambda: self._set_party(False) if tok == self._party_token else None)
        else:
            self.orb_view.set_state(self.pres.state)
            self._toast("Party over")

    def _toggle_party(self):
        self._set_party(not self.pres.party)

    # ================= layout modes =================
    def _toast(self, text, ms=2400, action=None, cb=None):
        self.toast.show_text(text, ms, action, cb)

    def _update_stage(self):
        tgt = self.presence_mode or self.mic_on
        if tgt == self.stage_target:
            return
        self.stage_target = tgt
        if self.stage < 0.01:
            self._chat_h = max(self.chat.height(), 140)
        self._stage_anim.stop()
        self._stage_anim.setStartValue(self.stage)
        self._stage_anim.setEndValue(1.0 if tgt else 0.0)
        self._stage_anim.start()

    def _stage_tick(self, v):
        self.stage = float(v)
        done = self.stage >= 0.999
        self.chat.setVisible(not done)
        if not done:
            self.chat.setMaximumHeight(16777215 if self.stage <= 0.001 else max(1, int(self._chat_h * (1 - self.stage))))
        self.live_caption.setVisible(self.stage > 0.55)

    def _update_sidebar(self, instant=False):
        want = self.sidebar_pref and not self.stage_target
        end = Sidebar.W if want else 0
        if self._sb_end == end and not instant:
            return
        self._sb_end = end
        self._sb_anim.stop()
        if want:
            self.sidebar.show()
        if instant or not self.isVisible():
            self._sb_w = float(end)
            self.sidebar.setFixedWidth(end)
            self.sidebar.setVisible(want)
            return
        self._sb_anim.setStartValue(self._sb_w)
        self._sb_anim.setEndValue(float(end))
        self._sb_anim.start()

    def _sb_tick(self, v):
        self._sb_w = float(v)
        self.sidebar.setFixedWidth(max(0, int(v)))

    def _sb_done(self):
        if self._sb_end == 0:
            self.sidebar.hide()

    def _toggle_sidebar(self):
        self.sidebar_pref = not self.sidebar_pref
        sfx("slide")
        self._update_sidebar()
        self._save_settings()

    def _toggle_presence(self):
        self.presence_mode = not self.presence_mode
        sfx("whoosh")
        self._update_stage()
        self._update_sidebar()
        self._update_caption()
        self._toast("Presence mode on. F2 to return." if self.presence_mode else "Presence mode off")

    # ================= features =================
    def _toggle_sound(self):
        self.sound.enabled = not self.sound.enabled
        if self.sound.enabled:
            sfx("tick")
        self.snd_btn.update()
        self._toast("Sound on" if self.sound.enabled else "Sound off")
        self._save_settings()

    def _toggle_autostart(self):
        self.autostart = not self.autostart
        ok = set_autostart(self.autostart)
        base = "Ai-ru will start when you log in" if self.autostart else "Ai-ru won't start at login"
        self._toast(base if ok else base + " (this system could not be updated)", 3200)
        self._save_settings()

    def _save_settings(self):
        save_json(SETTINGS_FILE, {"theme": self.theme, "sound": self.sound.enabled, "orb_mode": self.orb_mode,
                                  "sidebar": self.sidebar_pref, "orb_right": self.orb.saved_right,
                                  "orb_y": self.orb.saved_y, "autostart": self.autostart})

    def _cycle_theme(self):
        self._apply_theme(THEME_ORDER[(THEME_ORDER.index(self.theme) + 1) % len(THEME_ORDER)])

    def _apply_theme(self, name):
        self.theme = name
        c1, c2 = THEMES[name]
        STATE_DEFS["IDLE"].update(c1=c1, c2=c2)
        QApplication.instance().setStyleSheet(build_qss(c1))
        self.icon = make_app_icon(c1)
        self.setWindowIcon(self.icon)
        if self.tray:
            self.tray.setIcon(self.icon)
        self._push_orb_theme()
        self.pres.kick(0.8, shock=True)
        sfx("whoosh")
        self._toast(f"Theme: {name}")
        self._save_settings()

    def _toggle_pin(self):
        self.pinned = not self.pinned
        self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, self.pinned)
        self.show()
        self._toast("Pinned on top" if self.pinned else "Unpinned")

    def _cycle_opacity(self):
        levels = (1.0, 0.88, 0.75)
        self._opacity_i = (self._opacity_i + 1) % len(levels)
        self.setWindowOpacity(levels[self._opacity_i])
        self._toast(f"Window opacity {int(levels[self._opacity_i] * 100)}%")

    def _set_chrome(self, full):
        self.M, self.cut = (0, 0) if full else (22, 30)
        self.sidebar.cut = self.cut
        self.root.setContentsMargins(self.M, self.M, self.M, self.M)
        self.grip.setVisible(not full)
        self.update()

    def _toggle_fullscreen(self):
        sfx("whoosh")
        if self.isFullScreen():
            self._set_chrome(False)
            self.showNormal()
        else:
            self._set_chrome(True)
            self.showFullScreen()
        self.btn_full.update()

    def _escape(self):
        if self.palette.isVisible():
            self.palette.close_()
        elif self.isFullScreen():
            self._toggle_fullscreen()

    def _help(self):
        self._toast("Ctrl+K palette    F11 fullscreen    F2 presence    Ctrl+B sidebar\n"
                    "Ctrl+Space mic    Ctrl+T theme    Ctrl+N new    Ctrl+E export\n"
                    "Ctrl+Shift+O orb mode    Ctrl+Shift+P party    Ctrl+Q quit\n"
                    "Poke the orb. Seven pokes in a row is a secret.", 8000)

    def _palette_commands(self):
        cmds = [
            ("Toggle mic (listening mode)", "Ctrl+Space", self._toggle_mic),
            ("Wake Ai-ru (test the wake word)", "", self._do_wake),
            ("New session", "Ctrl+N", self._new_session),
            ("Slide sidebar", "Ctrl+B", self._toggle_sidebar),
            ("Toggle fullscreen", "F11", self._toggle_fullscreen),
            ("Presence mode", "F2", self._toggle_presence),
            ("Cycle theme", "Ctrl+T", self._cycle_theme),
            ("Party mode", "Ctrl+Shift+P", self._toggle_party),
            ("Poke Ai-ru", "", self._poke),
            ("Toggle sound", "Ctrl+Shift+S", self._toggle_sound),
            ("Start Ai-ru when I log in (toggle)", "", self._toggle_autostart),
            ("Orb: cycle visibility mode", "Ctrl+Shift+O", self._cycle_orb_mode),
            ("Send to background orb", "", self._to_background),
            ("Minimize to Ai-ru orb", "", self.showMinimized),
            ("Pin on top", "", self._toggle_pin),
            ("Cycle window opacity", "", self._cycle_opacity),
            ("Export session as Markdown", "Ctrl+E", self._export),
            ("Copy last reply", "", self._copy_last),
            ("Clear this session", "", self._clear_session),
            ("Delete this session", "", self._delete_session),
            ("Undo delete session", "", self._undo_delete),
            ("Delete ALL sessions", "", self._delete_all),
            ("Keyboard shortcuts", "", self._help),
            ("Quit Ai-ru", "Ctrl+Q", self._quit),
        ]
        for name in THEME_ORDER:
            cmds.append((f"Theme: {name}", "", lambda n=name: self._apply_theme(n)))
        for s in self.sessions[:40]:
            if s is not self.current and s["msgs"]:
                cmds.append(("Open: " + s["title"], fmt_ts(s["ts"]), lambda sid=s["id"]: self._switch_session(sid)))
        return cmds

    def _open_palette(self):
        self.palette.open_(self._palette_commands())

    def _restore(self):
        self.setWindowState((self.windowState() & ~Qt.WindowState.WindowMinimized) | Qt.WindowState.WindowActive)
        self.show()
        self.raise_()
        self.activateWindow()
        self.dock.entry.setFocus()
        self._orb_timer.start(300)

    def _quit(self):
        self._quitting = True
        self._save()
        self._save_settings()
        self.orb.hide()
        if self.tray:
            self.tray.hide()
        QApplication.instance().quit()

    # ================= clock =================
    def _frame(self):
        now = time.perf_counter()
        dt, self._last = now - self._last, now
        if dt > 0:
            self._fps = approach(self._fps, min(1.0 / dt, 240.0), 3.0, dt)
        P = self.pres
        P.advance(dt)
        main_vis = self.isVisible() and not self.isMinimized()
        want = 16 if main_vis else 33
        if self._clock.interval() != want:
            self._clock.setInterval(want)
        if self.orb.isVisible():
            self.orb.tick(dt)
            self.orb.update()
        if not main_vis:
            P.shock_req = 0
            return
        self._frame_n += 1
        if P.shock_req:  # every state change, send, poke and mic click rings through the orb
            P.shock_req = 0
            self.orb_view.pulse()
        self.orb_view.tick()
        if self._frame_n % 2 == 0:
            self.orb_view.set_voice(P.level if P.state in ("LISTENING", "SPEAKING") else 0.0)
        if P.party and time.time() - self._party_t > 0.8:  # party: the orb cycles through its state colours
            self._party_t, self._party_i = time.time(), (self._party_i + 1) % 4
            self.orb_view.set_state(("LISTENING", "THINKING", "SPEAKING", "IDLE")[self._party_i])
        self.wave.update()
        self.strip.update()
        self.dock.refresh()
        self.sidebar.wordmark.update()
        self.sidebar.new_btn.update()
        if self.sidebar.active_item is not None:
            self.sidebar.active_item.update()
        if self._frame_n % 4 == 0:
            for b in self.icon_btns:
                b.update()
        k = self.M + 58
        self.update(QRegion(self.rect()).subtracted(QRegion(self.rect().adjusted(k, k, -k, -k))))
        if (time.time() - self._idle_since > self._next_quip and P.state == "IDLE" and not self.mic_on):
            self.strip.set_thought(random.choice(IDLE_QUIPS))
            self._idle_since, self._next_quip = time.time(), random.uniform(45, 90)

    # ================= window chrome =================
    def paintEvent(self, _):
        P, M, C = self.pres, self.M, self.cut
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        # clear first: a translucent window never wipes its own backing store
        p.setCompositionMode(QPainter.CompositionMode.CompositionMode_Source)
        p.fillRect(self.rect(), Qt.GlobalColor.transparent)
        p.setCompositionMode(SRC)
        r = QRectF(self.rect()).adjusted(M, M, -M, -M)
        glow = 0.25 + 0.8 * P.energy + 0.4 * P.flash
        S = C * 0.4

        def shape(rect, grow=0.0):
            k = 0.414 * grow
            return chamfer(rect, C + k if C else 0, S + k if C else 0, C + k if C else 0, S + k if C else 0)

        p.setBrush(NOBRUSH)
        for i in range(1, M + 1):  # soft outer halo (windowed mode only)
            p.setPen(QPen(rgba(P.c1, 55 * glow * (1 - i / M) ** 2.4), 1.6))
            p.drawPath(shape(r.adjusted(-i, -i, i, i), i))
        path = shape(r)
        p.setPen(NOPEN)
        p.setBrush(QColor(3, 3, 5, 255))
        p.drawPath(path)
        p.setClipPath(path)
        p.setBrush(NOBRUSH)
        for i in range(0, 22, 2):  # gentle inner spill
            p.setPen(QPen(rgba(P.c1, 46 * glow * (1 - i / 22) ** 2.4), 3))
            p.drawPath(shape(r.adjusted(i, i, -i, -i)))
        p.setClipping(False)
        if M:
            p.setPen(QPen(rgba(P.c1, 60 + 60 * P.energy), 1.0))
            p.drawPath(shape(r.adjusted(0.5, 0.5, -0.5, -0.5)))
            p.setPen(pen_of(P.c1, 190 + 60 * P.energy, 2.4))  # the two big cut edges glow brightest
            p.drawLine(QPointF(r.left(), r.top() + C), QPointF(r.left() + C, r.top()))
            p.drawLine(QPointF(r.right() - C, r.bottom()), QPointF(r.right(), r.bottom() - C))

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self.grip.move(self.width() - self.M - 20, self.height() - self.M - 20)
        self.grip.raise_()
        if self.palette.isVisible():
            self.palette.setGeometry(self.rect())

    def mousePressEvent(self, e):
        if (e.button() == Qt.MouseButton.LeftButton and e.position().y() < self.M + 54
                and not self.isFullScreen()):
            h = self.windowHandle()
            if h:
                h.startSystemMove()
                return
        super().mousePressEvent(e)

    def mouseDoubleClickEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton and e.position().y() < self.M + 54:
            self._toggle_fullscreen()
        else:
            super().mouseDoubleClickEvent(e)

    def changeEvent(self, e):
        if e.type() in (QEvent.Type.WindowStateChange, QEvent.Type.ActivationChange):
            self._orb_timer.start(150)
        super().changeEvent(e)

    def showEvent(self, e):
        super().showEvent(e)
        self._update_sidebar(instant=True)
        self._orb_timer.start(300)

    def hideEvent(self, e):
        super().hideEvent(e)
        self._orb_timer.start(100)

    def closeEvent(self, e):
        if self._quitting:
            self._clock.stop()
            e.accept()
        else:  # Alt+F4 / taskbar close: go to the background, keep the orb
            e.ignore()
            self._to_background()


# backwards-compatible names for existing backends
AuraGUI = AuraWindow = AiruGUI


# --------------------------------------------------------------------------------------
# Demo harness (not used by the backend)
# --------------------------------------------------------------------------------------
def main():
    # 1. Initialize Windows AppUserModelID immediately before anything else
    if sys.platform == "win32":
        try:
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("airu.orb.hud.app")
        except Exception:
            pass

    # 2. Build QApplication and set app icon BEFORE building windows
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app_icon = make_app_icon(THEMES.get("Neon", ((26, 170, 255), (0, 76, 210)))[0])
    app.setWindowIcon(app_icon)

    f = QFont()
    f.setFamilies(FONT_FAMILIES)
    f.setPointSize(10)
    app.setFont(f)

    # 3. Create window (inherits AppUserModelID and app icon)
    win = AiruGUI()
    win.setWindowIcon(app_icon)
    win.start()
    if "--background" not in sys.argv:
        QTimer.singleShot(500, lambda: win.add_message(
            "ai-ru", f"{greeting()}. Poke the orb, press Ctrl+Space to talk, or type commands below to execute OS tools natively."))
    sys.exit(app.exec())


if __name__ == "__main__":
    main()