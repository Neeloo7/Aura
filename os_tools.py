"""Windows 11 OS controller: 3-tier launcher, process closer, media + telemetry."""

from __future__ import annotations

import ctypes
import difflib
import os
import subprocess
import webbrowser
from datetime import datetime
from pathlib import Path

import psutil
from AppOpener import close as app_close
from AppOpener import give_appnames
from AppOpener import open as app_open

CREATE_NO_WINDOW = 0x08000000

# Virtual-key codes for media / volume (more reliable than pyautogui on Win11).
_VK_VOLUME_MUTE = 0xAD
_VK_VOLUME_DOWN = 0xAE
_VK_VOLUME_UP = 0xAF
_VK_MEDIA_NEXT_TRACK = 0xB0
_VK_MEDIA_PLAY_PAUSE = 0xB3
_KEYEVENTF_EXTENDEDKEY = 0x0001
_KEYEVENTF_KEYUP = 0x0002

_URI_ALIASES: dict[str, tuple[str, bool]] = {
    # target -> (command, use_shell_start)
    "settings": ("ms-settings:", True),
    "windows settings": ("ms-settings:", True),
    "bin": ("shell:RecycleBinFolder", True),
    "recycle bin": ("shell:RecycleBinFolder", True),
    "recyclebin": ("shell:RecycleBinFolder", True),
    "trash": ("shell:RecycleBinFolder", True),
    "files": ("explorer", False),
    "file explorer": ("explorer", False),
    "explorer": ("explorer", False),
    "notepad": ("notepad", False),
    "calculator": ("calc", False),
    "calc": ("calc", False),
    "task manager": ("taskmgr", False),
    "taskmgr": ("taskmgr", False),
    "spotify": ("spotify:", True),
    "discord": ("discord:", True),
}

_WEB_SHORTCUTS: dict[str, str] = {
    "youtube": "https://www.youtube.com",
    "github": "https://github.com",
    "google": "https://www.google.com",
    "gmail": "https://mail.google.com",
    "twitter": "https://x.com",
    "x": "https://x.com",
    "reddit": "https://www.reddit.com",
    "chatgpt": "https://chatgpt.com",
    "wikipedia": "https://www.wikipedia.org",
    "netflix": "https://www.netflix.com",
    "linkedin": "https://www.linkedin.com",
    "stackoverflow": "https://stackoverflow.com",
}


def _normalize(name: str) -> str:
    return " ".join(name.lower().replace("_", " ").replace("-", " ").split())


def _shell_launch(command: str, use_start: bool) -> None:
    flags = CREATE_NO_WINDOW if os.name == "nt" else 0
    if use_start:
        subprocess.Popen(f'start "" {command}', shell=True, creationflags=flags)
    else:
        subprocess.Popen(command, shell=True, creationflags=flags)


def _press_vk(vk: int, times: int = 1) -> None:
    user32 = ctypes.windll.user32
    for _ in range(times):
        user32.keybd_event(vk, 0, _KEYEVENTF_EXTENDEDKEY, 0)
        user32.keybd_event(vk, 0, _KEYEVENTF_EXTENDEDKEY | _KEYEVENTF_KEYUP, 0)


def _looks_like_url(target: str) -> bool:
    t = target.strip().lower()
    if t.startswith(("http://", "https://", "www.")):
        return True
    if " " in t:
        return False
    return "." in t and not t.endswith(".exe")


def _installed_app_match(target: str) -> str | None:
    try:
        names = list(give_appnames(upper=False).keys())
    except Exception:
        return None
    needle = _normalize(target)
    if needle in names:
        return needle
    hits = difflib.get_close_matches(needle, names, n=1, cutoff=0.55)
    return hits[0] if hits else None


def open_app_or_website(target_name: str) -> str:
    """3-tier launcher: Windows URI/shell -> AppOpener fuzzy match -> web."""
    raw = (target_name or "").strip()
    if not raw:
        return "Need an app or site name to open."

    key = _normalize(raw)

    # Tier 1 — direct Windows URI / shell commands.
    if key in _URI_ALIASES:
        command, use_start = _URI_ALIASES[key]
        try:
            _shell_launch(command, use_start)
            return f"Opened {raw}."
        except Exception as exc:
            return f"Failed to open {raw}: {exc}"

    # Tier 2 — any installed app (Ableton, Valorant, Steam, Chrome, VS Code…).
    matched = _installed_app_match(key)
    if matched:
        try:
            app_open(matched, match_closest=True, output=False)
            return f"Opened {matched}."
        except Exception as exc:
            return f"Failed to open {matched}: {exc}"

    # Tier 3 — websites.
    url = _WEB_SHORTCUTS.get(key)
    if not url and _looks_like_url(raw):
        url = raw if raw.lower().startswith(("http://", "https://")) else f"https://{raw}"
    if url:
        webbrowser.open(url)
        return f"Opened {url}."

    return f"Couldn't find an app or site named '{raw}'."


def close_application(app_name: str) -> str:
    """Close via AppOpener fuzzy match, then psutil process-name fallback."""
    raw = (app_name or "").strip()
    if not raw:
        return "Need an application name to close."

    closed_via_opener = False
    try:
        app_close(raw, match_closest=True, output=False)
        closed_via_opener = True
    except Exception:
        closed_via_opener = False

    needle = _normalize(raw).replace(" ", "")
    killed = []
    for proc in psutil.process_iter(["pid", "name"]):
        try:
            pname = (proc.info["name"] or "").lower()
            compact = pname.replace(" ", "").removesuffix(".exe")
            if needle and (needle in compact or compact in needle):
                proc.terminate()
                killed.append(pname)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue

    if killed:
        return f"Closed {', '.join(sorted(set(killed)))}."
    if closed_via_opener:
        return f"Sent close signal to {raw}."
    return f"Couldn't find a running process for '{raw}'."


def _system_stats() -> str:
    cpu = psutil.cpu_percent(interval=0.4)
    ram = psutil.virtual_memory().percent
    batt = psutil.sensors_battery()
    if batt is None:
        battery = "Battery: n/a (desktop / no battery sensor)"
    else:
        plug = "plugged in" if batt.power_plugged else "on battery"
        battery = f"Battery: {batt.percent:.0f}% ({plug})"
    return f"CPU {cpu:.0f}%, RAM {ram:.0f}%. {battery}."


def _screenshot() -> str:
    try:
        import pyautogui
    except ImportError:
        return "Screenshot needs pyautogui installed."

    pictures = Path.home() / "Pictures"
    pictures.mkdir(parents=True, exist_ok=True)
    path = pictures / f"AI-ru_{datetime.now().strftime('%Y%m%d_%H%M%S')}.png"
    pyautogui.screenshot(str(path))
    return f"Screenshot saved to {path}."


def system_control(action: str) -> str:
    """Volume, media keys, screenshot, and live CPU/RAM/battery stats."""
    act = _normalize(action).replace(" ", "_")
    handlers = {
        "volume_up": lambda: (_press_vk(_VK_VOLUME_UP, 4), "Volume up."),
        "volume_down": lambda: (_press_vk(_VK_VOLUME_DOWN, 4), "Volume down."),
        "mute": lambda: (_press_vk(_VK_VOLUME_MUTE), "Mute toggled."),
        "play_pause": lambda: (_press_vk(_VK_MEDIA_PLAY_PAUSE), "Play/pause toggled."),
        "next_track": lambda: (_press_vk(_VK_MEDIA_NEXT_TRACK), "Skipped to next track."),
        "screenshot": _screenshot,
        "system_stats": _system_stats,
        "stats": _system_stats,
    }
    fn = handlers.get(act)
    if fn is None:
        return (
            f"Unknown action '{action}'. "
            "Use volume_up, volume_down, mute, play_pause, next_track, screenshot, or system_stats."
        )
    result = fn()
    if isinstance(result, tuple):
        return result[1]
    return result


OLLAMA_TOOLS_SCHEMA: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "open_app_or_website",
            "description": (
                "Open any Windows app, system pane, or website. "
                "Use for Settings, Recycle Bin, Explorer, Notepad, Calculator, "
                "Task Manager, Spotify, Discord, installed apps (Ableton, Steam, "
                "Chrome, VS Code, Valorant…), and sites like YouTube or GitHub."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "target_name": {
                        "type": "string",
                        "description": "App, URI alias, or website name (e.g. notepad, recycle bin, ableton, youtube).",
                    }
                },
                "required": ["target_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "close_application",
            "description": "Close a running desktop application by name.",
            "parameters": {
                "type": "object",
                "properties": {
                    "app_name": {
                        "type": "string",
                        "description": "Application name to close (e.g. notepad, chrome, discord).",
                    }
                },
                "required": ["app_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "system_control",
            "description": (
                "System actions: volume_up, volume_down, mute, play_pause, "
                "next_track, screenshot, or system_stats (CPU, RAM, battery)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": [
                            "volume_up",
                            "volume_down",
                            "mute",
                            "play_pause",
                            "next_track",
                            "screenshot",
                            "system_stats",
                        ],
                        "description": "Which OS action to run.",
                    }
                },
                "required": ["action"],
            },
        },
    },
]

_TOOL_MAP = {
    "open_app_or_website": open_app_or_website,
    "close_application": close_application,
    "system_control": system_control,
}


def execute_tool(tool_name: str, args: dict | None = None) -> str:
    """Dispatch an Ollama tool call to the matching OS function."""
    fn = _TOOL_MAP.get(tool_name)
    if fn is None:
        return f"Unknown tool '{tool_name}'."
    try:
        return fn(**(args or {}))
    except TypeError as exc:
        return f"Bad arguments for {tool_name}: {exc}"
    except Exception as exc:
        return f"{tool_name} failed: {exc}"


if __name__ == "__main__":
    print("AI-ru OS tools. Try: open notepad | open recycle bin | open ableton | stats | close notepad | quit")
    while True:
        try:
            line = input("> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not line:
            continue
        lower = line.lower()
        if lower in {"q", "quit", "exit"}:
            break
        if lower in {"stats", "system_stats"}:
            print(system_control("system_stats"))
            continue
        if lower.startswith("open "):
            print(open_app_or_website(line[5:].strip()))
            continue
        if lower.startswith("close "):
            print(close_application(line[6:].strip()))
            continue
        if lower.replace(" ", "_") in {
            "volume_up",
            "volume_down",
            "mute",
            "play_pause",
            "next_track",
            "screenshot",
        }:
            print(system_control(lower))
            continue
        print("Commands: open <name> | close <name> | stats | volume_up | mute | screenshot | quit")
