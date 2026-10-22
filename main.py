"""AI-ru Companion: Ultra-responsive terminal companion loop.

Ties together config, os_tools, and brain with 100% non-blocking optional voice.
- Zero audio/stream initialization at startup: starts instantly.
- Voice output is DISABLED by default (enable anytime with /voice on).
- Speech synthesis and playback run strictly in a detached background queue.
- Main thread is never blocked: input() and Ctrl+C are always responsive.
"""

from __future__ import annotations

import os
import sys
from typing import Any

# Suppress pygame welcome prompt and configure console encoding
os.environ["PYGAME_HIDE_SUPPORT_PROMPT"] = "1"

# Ensure UTF-8 encoding on Windows console to prevent UnicodeEncodeError in PowerShell
if sys.platform == "win32":
    try:
        if hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        if hasattr(sys.stderr, "reconfigure"):
            sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

import config
import os_tools
from brain import Brain


class AIRUCompanion:
    """Ultra-responsive terminal companion tying Ollama, OS tools, and optional voice."""

    def __init__(self, enable_voice: bool = False) -> None:
        self.brain = Brain()
        self.voice_enabled = enable_voice
        self._tts: Any = None
        self._stt: Any = None
        self._chunker: Any = None

    def _get_tts(self) -> Any:
        """Lazily initialize TextToSpeech engine on demand."""
        if self._tts is None:
            try:
                from voice import SentenceChunker, TextToSpeech

                self._tts = TextToSpeech(voice=config.TTS_VOICE)
                self._chunker = SentenceChunker()
            except Exception as exc:
                print(f"\n[⚠️ Voice initialization warning: {exc}]")
                self.voice_enabled = False
        return self._tts

    def _get_stt(self) -> Any:
        """Lazily initialize SpeechToText engine on demand."""
        if self._stt is None:
            print("[🎙️ Initializing Speech-to-Text engine...]")
            from voice import SpeechToText

            self._stt = SpeechToText()
        return self._stt

    def handle_query(self, user_text: str) -> None:
        """Process user query through Ollama brain with real-time streaming output."""
        user_text = user_text.strip()
        if not user_text:
            return

        # Interrupt any background audio if voice is active (barge-in)
        if self._tts is not None:
            self._tts.interrupt()

        print("\nAI-ru > ", end="", flush=True)

        streamed_pieces: list[str] = []
        chunker = None

        if self.voice_enabled:
            tts = self._get_tts()
            if tts:
                from voice import SentenceChunker

                chunker = SentenceChunker()

        def on_token(token: str) -> None:
            streamed_pieces.append(token)
            sys.stdout.write(token)
            sys.stdout.flush()

            # Non-blocking sentence feed into background TTS worker
            if self.voice_enabled and chunker and self._tts:
                for sentence in chunker.feed(token):
                    clean = sentence.strip()
                    if clean:
                        self._tts.speak(clean)

        try:
            reply = self.brain.process_user_query(user_text, on_token=on_token)

            # Flush any remaining sentence in buffer to background voice queue
            if self.voice_enabled and chunker and self._tts:
                for tail in chunker.flush():
                    clean = tail.strip()
                    if clean:
                        self._tts.speak(clean)

            # Fallback if reply was not streamed piece-by-piece
            if not streamed_pieces:
                print(reply, end="", flush=True)
                if self.voice_enabled and self._tts and reply:
                    self._tts.speak(reply)

            print()

        except KeyboardInterrupt:
            if self._tts is not None:
                self._tts.interrupt()
            print("\n[⏹️ Generation interrupted by user]")
        except Exception as exc:
            print(f"\n[❌ Error: {exc}]")

    def run_mic_input(self) -> None:
        """Capture microphone input, transcribe it via faster-whisper, and process it."""
        try:
            stt = self._get_stt()
            print("[🎙️ Listening... Speak into your microphone now]")
            transcribed = stt.listen()
            transcribed = transcribed.strip()
            if transcribed:
                print(f"[You (Voice) > {transcribed}]")
                self.handle_query(transcribed)
            else:
                print("[No speech detected.]")
        except KeyboardInterrupt:
            print("\n[🎙️ Microphone listening cancelled]")
        except Exception as exc:
            print(f"[❌ Microphone STT Error: {exc}]")

    def print_banner(self) -> None:
        voice_status = "ON (background)" if self.voice_enabled else "OFF (silent text mode)"
        print("=" * 66)
        print("       🤖  AI-ru (AURA Next-Gen) Terminal Companion  🤖       ")
        print("=" * 66)
        print(f" • Model       : {config.OLLAMA_MODEL}")
        print(f" • Voice Output: {voice_status}")
        print(" • Tools       : Native OS Tool Execution (Apps, Volume, Stats)")
        print("-" * 66)
        print(" Commands:")
        print("   /voice on   : Enable real-time background neural speech")
        print("   /voice off  : Disable speech output (silent terminal mode)")
        print("   /mic        : Speak query via microphone (faster-whisper STT)")
        print("   /stats      : Print live system hardware telemetry")
        print("   /tools      : List available OS automation tools")
        print("   /clear      : Reset conversation history")
        print("   /help       : Show command help")
        print("   quit / exit : Exit companion")
        print("=" * 66 + "\n")

    def run_loop(self) -> None:
        """Main terminal interaction loop. Never blocks on audio devices."""
        self.print_banner()

        while True:
            try:
                line = input("You > ").strip()
            except KeyboardInterrupt:
                if self._tts is not None and self._tts.is_speaking():
                    self._tts.interrupt()
                    print("\n[🔇 Speech playback interrupted]")
                    continue
                print("\nExiting AI-ru. Goodbye!")
                break
            except EOFError:
                print("\nExiting AI-ru. Goodbye!")
                break

            if not line:
                continue

            lower = line.lower()

            if lower in {"q", "quit", "exit"}:
                print("Exiting AI-ru. Goodbye!")
                if self._tts is not None:
                    self._tts.interrupt()
                break

            if lower in {"/mic", "/listen", "mic"}:
                self.run_mic_input()
                continue

            if lower in {"/voice on", "voice on"}:
                self.voice_enabled = True
                self._get_tts()
                print("[🔊 Background voice output enabled]")
                continue

            if lower in {"/voice off", "voice off"}:
                self.voice_enabled = False
                if self._tts is not None:
                    self._tts.interrupt()
                print("[🔇 Voice output disabled (silent mode)]")
                continue

            if lower in {"/clear", "/reset", "clear", "cls"}:
                self.brain.reset()
                print("[🧹 Conversation memory reset]")
                continue

            if lower in {"/stats", "stats"}:
                print(os_tools.system_control("system_stats"))
                continue

            if lower in {"/tools", "tools"}:
                print("\nRegistered OS Execution Tools:")
                for tool in os_tools.OLLAMA_TOOLS_SCHEMA:
                    fn = tool.get("function", {})
                    name = fn.get("name", "tool")
                    desc = fn.get("description", "").split("\n")[0]
                    print(f" • {name}: {desc}")
                print()
                continue

            if lower in {"/help", "help", "?"}:
                self.print_banner()
                continue

            # Regular query -> Ollama brain + tools
            self.handle_query(line)


def main() -> None:
    # Voice output is disabled by default for maximum terminal responsiveness
    companion = AIRUCompanion(enable_voice=False)
    companion.run_loop()


if __name__ == "__main__":
    main()
