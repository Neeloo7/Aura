"""AI-ru Voice Engine: Real-time, ultra-low-latency voice pipeline.

Implements:
1. Speech-to-Text (STT): faster-whisper (tiny.en/base.en) with int8 quantization
   and sounddevice microphone recording loop.
2. Text-to-Speech (TTS): edge-tts async neural synthesis (with pyttsx3 fallback)
   and non-blocking asynchronous pygame playback with immediate barge-in interruption.
3. Sentence Chunking: streaming sentence boundary generator (.!? splitting)
   for zero-perceived latency speech synthesis while LLM streams output.
"""

from __future__ import annotations

import asyncio
import os
import queue
import re
import sys
import tempfile
import threading
import time
from collections.abc import Callable, Iterable, Iterator
from typing import Any

import numpy as np
import pygame
import sounddevice as sd
from faster_whisper import WhisperModel

import config

# Fallback TTS support (offline SAPI5 / Windows native TTS)
try:
    import pyttsx3

    _HAS_PYTTSX3 = True
except ImportError:
    _HAS_PYTTSX3 = False


# ---------------------------------------------------------------------------
# 1. Sentence Chunker for Zero Latency Streaming
# ---------------------------------------------------------------------------

_ABBREVIATIONS = {
    "mr.",
    "mrs.",
    "ms.",
    "dr.",
    "prof.",
    "sr.",
    "jr.",
    "vs.",
    "etc.",
    "e.g.",
    "i.e.",
    "u.s.",
    "u.k.",
    "a.m.",
    "p.m.",
}


class SentenceChunker:
    """Buffers streaming token stream and yields complete sentences on punctuation (.!?).

    Designed to pipeline streaming LLM output directly into speech synthesis so the
    first sentence begins speaking before subsequent sentences finish generating.
    """

    def __init__(self) -> None:
        self._buffer: list[str] = []

    def feed(self, text: str) -> list[str]:
        """Feed a token or chunk of text and return any newly completed sentences."""
        if not text:
            return []

        self._buffer.append(text)
        current = "".join(self._buffer)
        sentences, remaining = self._split_sentences(current)

        if sentences:
            self._buffer = [remaining] if remaining else []
            return sentences
        return []

    def flush(self) -> list[str]:
        """Flush any remaining text in the buffer."""
        tail = "".join(self._buffer).strip()
        self._buffer = []
        if tail:
            # Strip remaining trailing punctuation or clean up
            return [tail]
        return []

    @classmethod
    def _split_sentences(cls, text: str) -> tuple[list[str], str]:
        """Split text into complete sentences and incomplete trailing text."""
        sentences: list[str] = []
        pattern = r"([.!?]+(?:\s+|\n+|$))"
        parts = re.split(pattern, text)

        if len(parts) <= 1:
            return [], text

        i = 0
        current_chunk = ""
        while i < len(parts) - 1:
            segment = parts[i]
            punct = parts[i + 1] if i + 1 < len(parts) else ""
            candidate = (current_chunk + segment + punct).strip()

            # Guard against common abbreviation splits (e.g. "e.g.", "Dr.")
            last_word = segment.strip().split()[-1].lower() + punct.strip().lower() if segment.strip() else ""
            if last_word in _ABBREVIATIONS or re.search(r"\d+\.\d+$", segment + punct):
                current_chunk += segment + punct
                i += 2
                continue

            if candidate:
                sentences.append(candidate)
                current_chunk = ""
            i += 2

        # The trailing remaining part (after last matched punctuation)
        remaining = current_chunk + (parts[-1] if len(parts) % 2 == 1 else "")
        return sentences, remaining


def iter_sentence_chunks(stream: Iterable[str]) -> Iterator[str]:
    """Generator that consumes a streaming text/token iterable and yields complete sentences."""
    chunker = SentenceChunker()
    for token in stream:
        for sentence in chunker.feed(token):
            yield sentence
    for sentence in chunker.flush():
        yield sentence


# ---------------------------------------------------------------------------
# 2. Text-to-Speech (TTS) Engine with Asynchronous Playback & Barge-In
# ---------------------------------------------------------------------------


class TextToSpeech:
    """Thread-safe neural TTS engine with pygame audio queue and barge-in interruption."""

    def __init__(
        self,
        voice: str | None = None,
        use_edge: bool = True,
    ) -> None:
        self.voice = voice or getattr(config, "TTS_VOICE", "en-US-AriaNeural")
        self.use_edge = use_edge

        # Pygame mixer initialization for low-latency playback
        self._mixer_lock = threading.Lock()
        self._ensure_mixer()

        # Playback queue and worker thread
        self._play_queue: queue.Queue[str | None] = queue.Queue()
        self._interrupt_event = threading.Event()
        self._is_speaking = False
        self._worker_thread = threading.Thread(target=self._playback_worker, daemon=True)
        self._worker_thread.start()

        # Optional pyttsx3 fallback engine
        self._pyttsx_engine = None
        if _HAS_PYTTSX3:
            try:
                self._pyttsx_engine = pyttsx3.init()
                self._pyttsx_engine.setProperty("rate", 195)
            except Exception:
                self._pyttsx_engine = None

    def _ensure_mixer(self) -> None:
        with self._mixer_lock:
            if not pygame.mixer.get_init():
                try:
                    # Low latency buffer: 512 samples
                    pygame.mixer.init(frequency=24000, size=-16, channels=2, buffer=512)
                except Exception:
                    pygame.mixer.init()

    def interrupt(self) -> None:
        """Immediately stop ongoing audio playback and clear queued speech (Barge-In)."""
        self._interrupt_event.set()
        with self._mixer_lock:
            try:
                if pygame.mixer.get_init():
                    pygame.mixer.music.stop()
                    pygame.mixer.music.unload()
            except Exception:
                pass

        # Drain the playback queue
        while not self._play_queue.empty():
            try:
                item = self._play_queue.get_nowait()
                if item and os.path.exists(item):
                    self._safe_remove(item)
                self._play_queue.task_done()
            except queue.Empty:
                break

        self._is_speaking = False
        self._interrupt_event.clear()

    def is_speaking(self) -> bool:
        """Check if audio is currently playing or queued."""
        return self._is_speaking or not self._play_queue.empty()

    def synthesize(self, text: str, output_path: str | None = None) -> str:
        """Synthesize text to an audio file (using edge-tts with pyttsx3 fallback)."""
        text = text.strip()
        if not text:
            return ""

        if output_path is None:
            fd, output_path = tempfile.mkstemp(suffix=".mp3")
            os.close(fd)

        # Attempt 1: edge-tts (Natural neural cloud voice)
        if self.use_edge:
            try:
                loop = asyncio.new_event_loop()
                try:
                    import edge_tts

                    communicate = edge_tts.Communicate(text, self.voice)
                    loop.run_until_complete(communicate.save(output_path))
                    if os.path.exists(output_path) and os.path.getsize(output_path) > 0:
                        return output_path
                finally:
                    loop.close()
            except Exception as exc:
                # Fallback to local offline TTS
                pass

        # Attempt 2: pyttsx3 offline fallback
        if self._pyttsx_engine is not None:
            try:
                wav_path = output_path.replace(".mp3", ".wav")
                self._pyttsx_engine.save_to_file(text, wav_path)
                self._pyttsx_engine.runAndWait()
                if os.path.exists(wav_path) and os.path.getsize(wav_path) > 0:
                    if os.path.exists(output_path):
                        self._safe_remove(output_path)
                    return wav_path
            except Exception:
                pass

        return ""

    def speak(self, text: str, interrupt_current: bool = False) -> None:
        """Synthesize and queue a single sentence/statement for immediate playback."""
        if interrupt_current:
            self.interrupt()

        audio_file = self.synthesize(text)
        if audio_file:
            self._play_queue.put(audio_file)

    def speak_stream(self, token_stream: Iterable[str], interrupt_current: bool = False) -> None:
        """Direct pipeline: consume streaming LLM tokens, chunk on punctuation,

        and immediately trigger speech synthesis for each clause while generation continues.
        """
        if interrupt_current:
            self.interrupt()

        for sentence in iter_sentence_chunks(token_stream):
            if self._interrupt_event.is_set():
                break
            clean_sentence = sentence.strip()
            if clean_sentence:
                audio_file = self.synthesize(clean_sentence)
                if audio_file and not self._interrupt_event.is_set():
                    self._play_queue.put(audio_file)

    def _playback_worker(self) -> None:
        """Background daemon thread playing audio files sequentially."""
        while True:
            audio_path = self._play_queue.get()
            if audio_path is None:
                break

            if self._interrupt_event.is_set() or not os.path.exists(audio_path):
                self._safe_remove(audio_path)
                self._play_queue.task_done()
                continue

            self._is_speaking = True
            try:
                with self._mixer_lock:
                    self._ensure_mixer()
                    pygame.mixer.music.load(audio_path)
                    pygame.mixer.music.play()

                # Wait for audio playback to finish or interruption to occur
                while True:
                    if self._interrupt_event.is_set():
                        with self._mixer_lock:
                            pygame.mixer.music.stop()
                            pygame.mixer.music.unload()
                        break
                    with self._mixer_lock:
                        busy = pygame.mixer.music.get_busy()
                    if not busy:
                        break
                    time.sleep(0.02)

            except Exception:
                pass
            finally:
                with self._mixer_lock:
                    try:
                        pygame.mixer.music.unload()
                    except Exception:
                        pass
                self._safe_remove(audio_path)
                self._is_speaking = False
                self._play_queue.task_done()

    @staticmethod
    def _safe_remove(path: str) -> None:
        try:
            if os.path.exists(path):
                os.remove(path)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# 3. Speech-to-Text (STT) Engine with faster-whisper and sounddevice
# ---------------------------------------------------------------------------


class SpeechToText:
    """Thread-safe STT engine using faster-whisper with sounddevice audio capture."""

    def __init__(
        self,
        model_size: str | None = None,
        device: str | None = None,
        compute_type: str | None = None,
        sample_rate: int | None = None,
    ) -> None:
        self.model_size = model_size or getattr(config, "WHISPER_MODEL", "tiny.en")
        self.sample_rate = sample_rate or getattr(config, "AUDIO_SAMPLE_RATE", 16000)

        # Device selection: prefer CUDA if available, fallback to CPU
        configured_device = device or getattr(config, "WHISPER_DEVICE", "auto")
        configured_compute = compute_type or getattr(config, "WHISPER_COMPUTE_TYPE", "int8")

        self.device, self.compute_type = self._resolve_device(configured_device, configured_compute)
        self.model = self._init_model()

    @staticmethod
    def _resolve_device(device_pref: str, compute_pref: str) -> tuple[str, str]:
        """Resolve CUDA vs CPU availability with optimal quantization."""
        if device_pref == "cuda":
            return "cuda", compute_pref
        if device_pref == "cpu":
            return "cpu", "int8"

        # Auto detection: check ctranslate2 CUDA device count
        try:
            import ctranslate2

            if ctranslate2.get_cuda_device_count() > 0:
                return "cuda", "int8_float16" if compute_pref == "int8" else compute_pref
        except Exception:
            pass
        return "cpu", "int8"

    def _init_model(self) -> WhisperModel:
        """Initialize faster-whisper WhisperModel with automatic fallback to CPU."""
        try:
            return WhisperModel(
                self.model_size,
                device=self.device,
                compute_type=self.compute_type,
            )
        except Exception as exc:
            # Fallback to CPU int8 if CUDA libraries or drivers fail
            if self.device != "cpu":
                self.device = "cpu"
                self.compute_type = "int8"
                return WhisperModel("tiny.en", device="cpu", compute_type="int8")
            raise exc

    def transcribe(self, audio: np.ndarray) -> str:
        """Transcribe a 16kHz float32 audio buffer into text with sub-200ms latency."""
        if audio is None or len(audio) == 0:
            return ""

        # Normalize to float32 mono [-1.0, 1.0]
        if audio.dtype != np.float32:
            audio = audio.astype(np.float32)

        # Faster-whisper transcription with beam_size=1 and vad_filter for maximum speed
        segments, _ = self.model.transcribe(
            audio,
            language="en",
            beam_size=1,
            vad_filter=True,
            vad_parameters=dict(min_silence_duration_ms=400),
        )
        texts = [segment.text.strip() for segment in segments if segment.text.strip()]
        return " ".join(texts).strip()

    def record_phrase(
        self,
        energy_threshold: float | None = None,
        silence_duration: float | None = None,
        max_duration: float = 12.0,
        stop_event: threading.Event | None = None,
    ) -> np.ndarray:
        """Capture microphone audio buffer until the user pauses or stops speaking.

        Uses RMS energy threshold to detect speech start and silence timeout to detect speech end.
        """
        threshold = energy_threshold or getattr(config, "AUDIO_ENERGY_THRESHOLD", 0.015)
        silence_limit = silence_duration or getattr(config, "SILENCE_DURATION", 0.8)

        block_size = int(self.sample_rate * 0.05)  # 50ms blocks
        audio_chunks: list[np.ndarray] = []
        speech_started = False
        silence_start_time: float | None = None
        start_time = time.time()

        def audio_callback(indata, frames, time_info, status):
            nonlocal speech_started, silence_start_time
            if stop_event and stop_event.is_set():
                raise sd.CallbackStop()

            chunk = indata[:, 0].copy()
            audio_chunks.append(chunk)

            # Compute RMS energy of this audio chunk
            rms = np.sqrt(np.mean(chunk**2))

            if rms >= threshold:
                speech_started = True
                silence_start_time = None
            elif speech_started:
                if silence_start_time is None:
                    silence_start_time = time.time()

        with sd.InputStream(
            samplerate=self.sample_rate,
            channels=1,
            dtype="float32",
            blocksize=block_size,
            callback=audio_callback,
        ):
            while True:
                if stop_event and stop_event.is_set():
                    break
                if speech_started and silence_start_time:
                    if time.time() - silence_start_time >= silence_limit:
                        break
                if time.time() - start_time >= max_duration:
                    break
                time.sleep(0.02)

        if not audio_chunks:
            return np.array([], dtype=np.float32)

        return np.concatenate(audio_chunks, axis=0)

    def listen(
        self,
        energy_threshold: float | None = None,
        silence_duration: float | None = None,
        stop_event: threading.Event | None = None,
    ) -> str:
        """Listen from the microphone, record a phrase, and return the transcribed text."""
        audio = self.record_phrase(
            energy_threshold=energy_threshold,
            silence_duration=silence_duration,
            stop_event=stop_event,
        )
        return self.transcribe(audio)


# ---------------------------------------------------------------------------
# 4. High-Level VoicePipeline Convenience Wrapper
# ---------------------------------------------------------------------------


class VoicePipeline:
    """Unified voice pipeline combining STT, TTS, sentence chunking, and barge-in."""

    def __init__(self) -> None:
        self.stt = SpeechToText()
        self.tts = TextToSpeech()

    def listen(self, stop_event: threading.Event | None = None) -> str:
        """Listen to the microphone and transcribe the user's spoken input."""
        return self.stt.listen(stop_event=stop_event)

    def speak(self, text: str, interrupt: bool = False) -> None:
        """Speak a text string using neural TTS."""
        self.tts.speak(text, interrupt_current=interrupt)

    def speak_stream(self, token_stream: Iterable[str], interrupt: bool = False) -> None:
        """Stream speech with sentence chunking for zero latency."""
        self.tts.speak_stream(token_stream, interrupt_current=interrupt)

    def interrupt(self) -> None:
        """Barge-in: immediately cancel playback."""
        self.tts.interrupt()


# ---------------------------------------------------------------------------
# CLI Self-Test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    print("=" * 60)
    print("AI-ru Voice Engine Test CLI")
    print("Commands:")
    print("  speak <text>  : Test TTS synthesis & playback")
    print("  stream <text> : Test sentence chunking streaming pipeline")
    print("  listen        : Test microphone STT (Speak into your mic)")
    print("  stop          : Test barge-in interruption")
    print("  quit          : Exit")
    print("=" * 60)

    pipeline = VoicePipeline()

    while True:
        try:
            line = input("voice> ").strip()
        except (KeyboardInterrupt, EOFError):
            print("\nExiting.")
            break

        if not line:
            continue
        if line.lower() in {"q", "quit", "exit"}:
            break
        if line.lower().startswith("speak "):
            to_say = line[6:].strip()
            print(f"Synthesizing: '{to_say}'")
            pipeline.speak(to_say)
            continue
        if line.lower().startswith("stream "):
            text = line[7:].strip()

            # Simulate streaming tokens from an LLM word by word
            def fake_llm_stream(full_text: str):
                for word in full_text.split(" "):
                    yield word + " "
                    time.sleep(0.04)

            print("Streaming tokens to voice pipeline...")
            pipeline.speak_stream(fake_llm_stream(text))
            continue
        if line.lower() in {"listen", "rec", "record"}:
            print("Listening... (Speak into microphone now)")
            result = pipeline.listen()
            print(f"Recognized: '{result}'")
            continue
        if line.lower() in {"stop", "interrupt"}:
            pipeline.interrupt()
            print("Playback interrupted.")
            continue
