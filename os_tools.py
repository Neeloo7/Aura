"""Windows 11 OS controller: 3-tier launcher, process closer, media + telemetry."""

from __future__ import annotations

import ctypes
import difflib
import glob
import os
import subprocess
import tempfile
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

# Custom App Aliases: maps common application names to candidate paths, URIs, or commands.
# Environment variables like %LocalAppData%, %ProgramFiles%, %APPDATA% are expanded dynamically.
CUSTOM_APP_ALIASES: dict[str, list[str]] = {
    "obsidian": [
        r"%LocalAppData%\Programs\Obsidian\Obsidian.exe",
        r"%ProgramFiles%\Obsidian\Obsidian.exe",
        r"%ProgramFiles(x86)%\Obsidian\Obsidian.exe",
        "obsidian://",
        "obsidian",
    ],
    "discord": [
        r"%LocalAppData%\Discord\Update.exe --processStart Discord.exe",
        r"%LocalAppData%\Discord\app-*\Discord.exe",
        "discord://",
        "discord",
    ],
    "vscode": [
        r"%LocalAppData%\Programs\Microsoft VS Code\Code.exe",
        r"%ProgramFiles%\Microsoft VS Code\Code.exe",
        r"%ProgramFiles(x86)%\Microsoft VS Code\Code.exe",
        "vscode://",
        "code",
    ],
    "vs code": [
        r"%LocalAppData%\Programs\Microsoft VS Code\Code.exe",
        r"%ProgramFiles%\Microsoft VS Code\Code.exe",
        "vscode://",
        "code",
    ],
    "visual studio code": [
        r"%LocalAppData%\Programs\Microsoft VS Code\Code.exe",
        r"%ProgramFiles%\Microsoft VS Code\Code.exe",
        "vscode://",
        "code",
    ],
    "code": [
        r"%LocalAppData%\Programs\Microsoft VS Code\Code.exe",
        "code",
    ],
    "cursor": [
        r"%LocalAppData%\Programs\cursor\Cursor.exe",
        "cursor://",
        "cursor",
    ],
    "spotify": [
        "spotify:",
        r"%AppData%\Spotify\Spotify.exe",
        r"%ProgramFiles%\Spotify\Spotify.exe",
        "spotify",
    ],
    "chrome": [
        r"%ProgramFiles%\Google\Chrome\Application\chrome.exe",
        r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe",
        r"%LocalAppData%\Google\Chrome\Application\chrome.exe",
        "chrome",
    ],
    "google chrome": [
        r"%ProgramFiles%\Google\Chrome\Application\chrome.exe",
        r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe",
        "chrome",
    ],
    "brave": [
        r"%ProgramFiles%\BraveSoftware\Brave-Browser\Application\brave.exe",
        r"%LocalAppData%\BraveSoftware\Brave-Browser\Application\brave.exe",
        "brave",
    ],
    "edge": [
        "microsoft-edge:",
        r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe",
        r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe",
        "msedge",
    ],
    "microsoft edge": [
        "microsoft-edge:",
        r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe",
        "msedge",
    ],
    "steam": [
        "steam://",
        r"%ProgramFiles(x86)%\Steam\steam.exe",
        r"%ProgramFiles%\Steam\steam.exe",
        "steam",
    ],
    "slack": [
        r"%LocalAppData%\slack\slack.exe",
        "slack://",
        "slack",
    ],
    "notion": [
        r"%LocalAppData%\Programs\Notion\Notion.exe",
        "notion://",
        "notion",
    ],
    "telegram": [
        r"%AppData%\Telegram Desktop\Telegram.exe",
        "tg://",
        "telegram",
    ],
    "whatsapp": [
        r"%LocalAppData%\WhatsApp\WhatsApp.exe",
        "whatsapp:",
        "whatsapp",
    ],
    "terminal": [
        "wt",
        "powershell",
    ],
    "windows terminal": [
        "wt",
    ],
    "wt": [
        "wt",
    ],
    "powershell": [
        "pwsh",
        "powershell",
    ],
    "cmd": [
        "cmd",
    ],
    "command prompt": [
        "cmd",
    ],
    "notepad": [
        "notepad",
    ],
    "calculator": [
        "calc",
    ],
    "calc": [
        "calc",
    ],
    "settings": [
        "ms-settings:",
    ],
    "windows settings": [
        "ms-settings:",
    ],
    "recycle bin": [
        "shell:RecycleBinFolder",
    ],
    "recyclebin": [
        "shell:RecycleBinFolder",
    ],
    "bin": [
        "shell:RecycleBinFolder",
    ],
    "trash": [
        "shell:RecycleBinFolder",
    ],
    "task manager": [
        "taskmgr",
    ],
    "taskmgr": [
        "taskmgr",
    ],
    "file explorer": [
        "explorer",
    ],
    "explorer": [
        "explorer",
    ],
    "files": [
        "explorer",
    ],
    "paint": [
        "mspaint",
    ],
    "snipping tool": [
        "ms-screenclip:",
        "snippingtool",
    ],
    "camera": [
        "microsoft.windows.camera:",
    ],
    "vlc": [
        r"%ProgramFiles%\VideoLAN\VLC\vlc.exe",
        r"%ProgramFiles(x86)%\VideoLAN\VLC\vlc.exe",
        "vlc",
    ],
    "postman": [
        r"%LocalAppData%\Postman\Postman.exe",
        "postman://",
        "postman",
    ],
    "github desktop": [
        r"%LocalAppData%\GitHubDesktop\GitHubDesktop.exe",
        "github-windows://",
    ],
    "docker": [
        r"%ProgramFiles%\Docker\Docker\Docker Desktop.exe",
    ],
    "docker desktop": [
        r"%ProgramFiles%\Docker\Docker\Docker Desktop.exe",
    ],
    "word": [
        "winword",
    ],
    "excel": [
        "excel",
    ],
    "powerpoint": [
        "powerpnt",
    ],
    "ableton": [
        r"%ProgramFiles%\Ableton\*\Program\Ableton Live*.exe",
        r"%ProgramData%\Ableton\*\Program\Ableton Live*.exe",
        "ableton",
    ],
    "valorant": [
        r"%RiotGames%\Riot Client\RiotClientServices.exe",
        "valorant",
    ],
}

# Process name hints for closing applications via psutil
_APP_PROCESS_HINTS: dict[str, list[str]] = {
    "obsidian": ["obsidian.exe"],
    "discord": ["discord.exe"],
    "vscode": ["code.exe"],
    "vs code": ["code.exe"],
    "visual studio code": ["code.exe"],
    "code": ["code.exe"],
    "cursor": ["cursor.exe"],
    "spotify": ["spotify.exe"],
    "chrome": ["chrome.exe"],
    "google chrome": ["chrome.exe"],
    "brave": ["brave.exe"],
    "edge": ["msedge.exe"],
    "microsoft edge": ["msedge.exe"],
    "steam": ["steam.exe", "steamservice.exe"],
    "slack": ["slack.exe"],
    "notion": ["notion.exe"],
    "telegram": ["telegram.exe"],
    "whatsapp": ["whatsapp.exe"],
    "terminal": ["windowsterminal.exe"],
    "notepad": ["notepad.exe"],
    "calculator": ["calculatorapp.exe", "calc.exe"],
    "task manager": ["taskmgr.exe"],
    "vlc": ["vlc.exe"],
    "docker": ["docker desktop.exe"],
}

_URI_ALIASES: dict[str, tuple[str, bool]] = {
    # Legacy target -> (command, use_shell_start)
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


def _launch_app_alias(target: str) -> tuple[str, bool]:
    """Check custom app alias dictionary using exact and fuzzy matching.

    Returns (matched_key, success).
    """
    needle = _normalize(target)

    # 1. Exact match
    matched_key = needle if needle in CUSTOM_APP_ALIASES else None

    # 2. Fuzzy match against alias dictionary
    if not matched_key:
        hits = difflib.get_close_matches(needle, list(CUSTOM_APP_ALIASES.keys()), n=1, cutoff=0.7)
        if hits:
            matched_key = hits[0]

    if not matched_key:
        return "", False

    candidates = CUSTOM_APP_ALIASES[matched_key]
    flags = CREATE_NO_WINDOW if os.name == "nt" else 0

    for cand in candidates:
        expanded = os.path.expandvars(cand)

        # A. Wildcard/glob file path (e.g., app-*\Discord.exe or Ableton Live*.exe)
        if "*" in expanded:
            matches = glob.glob(expanded)
            if matches:
                exe = sorted(matches)[-1]
                try:
                    subprocess.Popen(f'"{exe}"', shell=True, creationflags=flags)
                    return matched_key, True
                except Exception:
                    continue

        # B. Direct executable path on disk
        parts = expanded.split(" --", 1)
        base_path = parts[0].strip(' "')
        if os.path.isfile(base_path) or os.path.exists(base_path):
            try:
                cmd = expanded if "--" in expanded else f'"{expanded}"'
                subprocess.Popen(cmd, shell=True, creationflags=flags)
                return matched_key, True
            except Exception:
                continue

        # C. URI schemes (e.g., obsidian://, discord://, spotify:, ms-settings:, shell:)
        if ":" in cand and not cand.startswith(("\\", "/", "C:", "c:", "D:", "d:")) and not os.path.isabs(expanded):
            try:
                subprocess.Popen(f'start "" {cand}', shell=True, creationflags=flags)
                return matched_key, True
            except Exception:
                continue

        # D. Direct shell command (e.g., notepad, calc, code, wt)
        if " " not in cand and not cand.endswith(".exe"):
            try:
                subprocess.Popen(cand, shell=True, creationflags=flags)
                return matched_key, True
            except Exception:
                continue

    return matched_key, False


def open_app_or_website(target_name: str) -> str:
    """3-tier launcher: Custom App Aliases / URIs -> AppOpener fuzzy match -> web."""
    raw = (target_name or "").strip()
    if not raw:
        return "Need an app or site name to open."

    key = _normalize(raw)

    # Tier 1 — Custom App Aliases with fuzzy matching (Obsidian, Discord, VS Code, Chrome...)
    matched_alias, success = _launch_app_alias(key)
    if success:
        return f"Opened {matched_alias.title()}."

    # Legacy direct Windows URI / shell commands
    if key in _URI_ALIASES:
        command, use_start = _URI_ALIASES[key]
        try:
            _shell_launch(command, use_start)
            return f"Opened {raw}."
        except Exception as exc:
            return f"Failed to open {raw}: {exc}"

    # Tier 2 — AppOpener fuzzy match against all installed Windows applications
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
    """Close via custom process hints, AppOpener fuzzy match, then psutil fallback."""
    raw = (app_name or "").strip()
    if not raw:
        return "Need an application name to close."

    key = _normalize(raw)
    closed_via_opener = False

    # Check custom alias hints for exact process names
    process_targets = []
    if key in _APP_PROCESS_HINTS:
        process_targets.extend(_APP_PROCESS_HINTS[key])
    else:
        # Fuzzy match key against hints
        hits = difflib.get_close_matches(key, list(_APP_PROCESS_HINTS.keys()), n=1, cutoff=0.7)
        if hits:
            process_targets.extend(_APP_PROCESS_HINTS[hits[0]])

    # Terminate matching target processes directly if found
    killed = []
    if process_targets:
        targets_lower = {p.lower() for p in process_targets}
        for proc in psutil.process_iter(["pid", "name"]):
            try:
                pname = (proc.info["name"] or "").lower()
                if pname in targets_lower:
                    proc.terminate()
                    killed.append(pname)
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        if killed:
            return f"Closed {', '.join(sorted(set(killed)))}."

    # Try AppOpener
    try:
        app_close(raw, match_closest=True, output=False)
        closed_via_opener = True
    except Exception:
        closed_via_opener = False

    # psutil fallback
    needle = key.replace(" ", "")
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


def capture_screen_context(save_path: str | None = None) -> str:
    """Capture the current active screen state into a temporary 'screen_context.png'.

    Registered tool for Ollama screen vision and desktop context analysis.
    Briefly hides any active AiruGUI window so it captures the actual desktop/Spotify
    instead of the app's own UI window.
    """
    import time
    target_path = Path(save_path) if save_path else Path(tempfile.gettempdir()) / "screen_context.png"
    target_path.parent.mkdir(parents=True, exist_ok=True)
    captured = False
    hidden_windows: list[Any] = []
    app = None

    # Hide any active AiruGUI windows briefly so we capture the true desktop/Spotify
    try:
        from PyQt6.QtWidgets import QApplication
        app = QApplication.instance()
        if app:
            for w in app.topLevelWidgets():
                if w.isVisible():
                    hidden_windows.append(w)
                    w.hide()
            app.processEvents()
            time.sleep(0.08)  # Let Windows DWM compositor paint underlying desktop windows
    except Exception:
        pass

    try:
        # Tier 1: Try PyQt6 grabWindow (guaranteed to work in active desktop GUI session)
        if app:
            try:
                from PyQt6.QtGui import QGuiApplication
                screen = QGuiApplication.primaryScreen()
                if screen:
                    pix = screen.grabWindow(0)
                    if not pix.isNull():
                        pix.save(str(target_path))
                        captured = True
            except Exception:
                pass

        # Tier 2: Try pyautogui
        if not captured:
            try:
                import pyautogui
                img = pyautogui.screenshot()
                img.save(str(target_path))
                captured = True
            except Exception:
                pass

        # Tier 3: Try PIL ImageGrab
        if not captured:
            try:
                from PIL import ImageGrab
                img = ImageGrab.grab(all_screens=True)
                img.save(str(target_path))
                captured = True
            except Exception as exc:
                return f"Failed to capture screen context: {exc}"
    finally:
        # Restore all previously visible windows immediately
        if app and hidden_windows:
            for w in hidden_windows:
                try:
                    w.show()
                except Exception:
                    pass
            app.processEvents()

    if captured:
        try:
            import shutil
            shutil.copy2(str(target_path), "screen_context.png")
        except Exception:
            pass
        return f"Active screen context captured and saved to '{target_path.resolve()}'."

    return "Failed to capture screen context."


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
        "capture_screen_context": capture_screen_context,
        "screen_context": capture_screen_context,
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
                            "capture_screen_context",
                            "system_stats",
                        ],
                        "description": "Which OS action to run.",
                    }
                },
                "required": ["action"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "capture_screen_context",
            "description": (
                "Capture the current desktop screen into a temporary 'screen_context.png' for visual context "
                "and UI analysis. Call this immediately whenever the user asks what is playing on Spotify or media, "
                "what window or app is open, what is on their screen, or asks for visual inspection. "
                "Never ask for permission or confirm first; invoke this tool immediately."
            ),
            "parameters": {
                "type": "object",
                "properties": {},
            },
        },
    },
]

_TOOL_MAP = {
    "open_app_or_website": open_app_or_website,
    "close_application": close_application,
    "system_control": system_control,
    "capture_screen_context": capture_screen_context,
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
