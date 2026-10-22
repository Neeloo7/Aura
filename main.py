"""AI-ru Companion: Terminal integration tying together config, os_tools, brain, and voice.

Supports:
- Interactive text input & command interface
- Native Ollama tool-calling for Windows OS operations (apps, volume, telemetry)
- Real-time streaming response with clause-by-clause low-latency neural voice output
- Microphone speech input toggle (/listen or /mic)
- Immediate speech barge-in interruption (Ctrl+C during speech)
"""

from __future__ import annotations

import os
import sys
import time
from typing import List

import config
import os_tools
from brain import Brain
from voice import SentenceChunker, SpeechToText, TextToSpeech


class AIRUCompanion:
    """Ties together Ollama brain, OS execution tools, and real-time voice engine."""

    def __init__(self, enable_voice: bool = True) -> None:
        self.brain = Brain()
        self.tts = TextToSpeech(voice=config.TTS_VOICE)
        self.stt: SpeechToText | None = None  # Lazy-initialized on first /mic call
        self.voice_enabled = enable_voice

    def _get_stt(self) -> SpeechToText:
        if self.stt is None:
            print("[🎙️ Initializing Speech-to-Text engine...]")
            self.stt = SpeechToText()
        return self.stt

    def handle_query(self, user_text: str) -> None:
        """Process user query through Ollama brain with streaming text and sentence-chunked voice."""
        user_text = user_text.strip()
        if not user_text:
            return

        # Stop any ongoing audio playback before answering new query (barge-in)
        self.tts.interrupt()

        print("\nAI-ru > ", end="", flush=True)

        chunker = SentenceChunker()
        streamed_pieces: list[str] = []

        def on_token(token: str) -> None:
            streamed_pieces.append(token)
            sys.stdout.write(token)
            sys.stdout.flush()

            if self.voice_enabled:
                for sentence in chunker.feed(token):
                    clean_sentence = sentence.strip()
                    if clean_sentence:
                        self.tts.speak(clean_sentence)

        try:
            reply = self.brain.process_user_query(user_text, on_token=on_token)

            # Flush any remaining sentence clause in the chunker buffer
            if self.voice_enabled:
                for remaining_sentence in chunker.flush():
                    clean_sentence = remaining_sentence.strip()
                    if clean_sentence:
                        self.tts.speak(clean_sentence)

            # If the response was not streamed piece-by-piece, display and speak reply directly
            if not streamed_pieces:
                print(reply, end="", flush=True)
                if self.voice_enabled and reply:
                    self.tts.speak(reply)

            print()

        except KeyboardInterrupt:
            self.tts.interrupt()
            print("\n[⏹️ Generation / speech interrupted]")
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
        except Exception as exc:
            print(f"[❌ Microphone STT Error: {exc}]")

    def print_banner(self) -> None:
        print("=" * 66)
        print("       🤖  AI-ru (AURA Next-Gen) Terminal Companion  🤖       ")
        print("=" * 66)
        print(f" • Model       : {config.OLLAMA_MODEL}")
        print(f" • Voice       : {config.TTS_VOICE} (Voice output: {'ON' if self.voice_enabled else 'OFF'})")
        print(" • Tools       : Native OS Tool Execution (Apps, Volume, Stats)")
        print("-" * 66)
        print(" Commands:")
        print("   /mic        : Speak via microphone (faster-whisper STT)")
        print("   /voice on   : Enable real-time neural speech output")
        print("   /voice off  : Disable speech output (silent terminal mode)")
        print("   /stats      : Print live system hardware telemetry")
        print("   /tools      : List available OS automation tools")
        print("   /clear      : Reset rolling conversation history")
        print("   /help       : Show command help")
        print("   quit / exit : Exit companion")
        print("   Ctrl + C    : Interrupt ongoing speech (Barge-In)")
        print("=" * 66 + "\n")

    def run_loop(self) -> None:
        """Main terminal interaction loop."""
        self.print_banner()

        while True:
            try:
                line = input("You > ").strip()
            except KeyboardInterrupt:
                # If audio is speaking, interrupt it; otherwise confirm quit
                if self.tts.is_speaking():
                    self.tts.interrupt()
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

            # Command dispatch
            if lower in {"q", "quit", "exit"}:
                print("Exiting AI-ru. Goodbye!")
                self.tts.interrupt()
                break

            if lower in {"/mic", "/listen", "mic"}:
                self.run_mic_input()
                continue

            if lower in {"/voice on", "voice on"}:
                self.voice_enabled = True
                print("[🔊 Voice output enabled]")
                continue

            if lower in {"/voice off", "voice off"}:
                self.voice_enabled = False
                self.tts.interrupt()
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

            # Process regular text query through Ollama + tools + voice
            self.handle_query(line)


def main() -> None:
    companion = AIRUCompanion(enable_voice=True)
    companion.run_loop()


if __name__ == "__main__":
    main()
