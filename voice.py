"""AI-ru Voice Engine: Real-time, ultra-low-latency voice pipeline.

Implements:
1. Speech-to-Text (STT): faster-whisper (tiny.en/base.en) with int8 quantization
   and sounddevice microphone recording loop (lazy initialized).
2. Text-to-Speech (TTS): completely non-blocking background queue with edge-tts
   (and pyttsx3 fallback), pygame mixer audio playback, and instant barge-in.
3. Sentence Chunking: streaming sentence boundary generator (.!? splitting)
   for zero-perceived latency speech pipelining while LLM streams output.
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

os.environ["PYGAME_HIDE_SUPPORT_PROMPT"] = "1"
from collections.abc import Callable, Iterable, Iterator
from typing import Any

import numpy as np

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
# 2. Text-to-Speech (TTS) Engine - 100% Non-Blocking Asynchronous Queue
# ---------------------------------------------------------------------------


class TextToSpeech:
    """Thread-safe neural TTS engine with dedicated background synthesis & playback.

    Calls to `speak()` never block the calling thread or terminal input loop.
    """

    def __init__(
        self,
        voice: str | None = None,
        use_edge: bool = True,
    ) -> None:
        self.voice = voice or getattr(config, "TTS_VOICE", "en-US-AriaNeural")
        self.use_edge = use_edge

        self._mixer_lock = threading.Lock()
        self._mixer_initialized = False

        # Queue holding text strings to be synthesized and spoken in background
        self._text_queue: queue.Queue[str | None] = queue.Queue()
        self._interrupt_event = threading.Event()
        self._is_speaking = False

        # Optional pyttsx3 fallback engine (lazy initialized)
        self._pyttsx_engine = None

        # Dedicated background worker for synthesis + playback
        self._worker_thread = threading.Thread(target=self._speech_worker, daemon=True)
        self._worker_thread.start()

    def _ensure_mixer(self) -> None:
        """Lazily initialize pygame.mixer on demand in background thread."""
        with self._mixer_lock:
            if not self._mixer_initialized:
                try:
                    import pygame

                    if not pygame.mixer.get_init():
                        pygame.mixer.init(frequency=24000, size=-16, channels=2, buffer=512)
                    self._mixer_initialized = True
                except Exception:
                    try:
                        import pygame

                        pygame.mixer.init()
                        self._mixer_initialized = True
                    except Exception:
                        pass

    def interrupt(self) -> None:
        """Immediately stop ongoing audio playback and discard queued speech (Barge-In)."""
        self._interrupt_event.set()

        # Stop currently playing pygame audio
        with self._mixer_lock:
            try:
                import pygame

                if pygame.mixer.get_init():
                    pygame.mixer.music.stop()
                    pygame.mixer.music.unload()
            except Exception:
                pass

        # Drain the text queue
        while not self._text_queue.empty():
            try:
                self._text_queue.get_nowait()
                self._text_queue.task_done()
            except queue.Empty:
                break

        self._is_speaking = False
        self._interrupt_event.clear()

    def is_speaking(self) -> bool:
        """Check if audio is currently playing or waiting in queue."""
        return self._is_speaking or not self._text_queue.empty()

    def speak(self, text: str, interrupt_current: bool = False) -> None:
        """Queue text for speech synthesis and playback in background (completely non-blocking)."""
        if not text or not text.strip():
            return
        if interrupt_current:
            self.interrupt()
        self._text_queue.put(text.strip())

    def speak_stream(self, token_stream: Iterable[str], interrupt_current: bool = False) -> None:
        """Non-blocking: chunks tokens and feeds completed sentences into background speech queue."""
        if interrupt_current:
            self.interrupt()

        for sentence in iter_sentence_chunks(token_stream):
            if self._interrupt_event.is_set():
                break
            clean_sentence = sentence.strip()
            if clean_sentence:
                self.speak(clean_sentence)

    def synthesize_to_file(self, text: str, output_path: str | None = None) -> str:
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
            except Exception:
                pass

        # Attempt 2: pyttsx3 offline fallback (lazy initialized)
        if _HAS_PYTTSX3:
            try:
                if self._pyttsx_engine is None:
                    self._pyttsx_engine = pyttsx3.init()
                    self._pyttsx_engine.setProperty("rate", 195)
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

    def _speech_worker(self) -> None:
        """Background daemon thread: synthesizes and plays queued sentences sequentially."""
        while True:
            text = self._text_queue.get()
            if text is None:
                break

            if self._interrupt_event.is_set():
                self._text_queue.task_done()
                continue

            self._is_speaking = True
            audio_path = ""
            try:
                # 1. Synthesize audio file
                audio_path = self.synthesize_to_file(text)
                if not audio_path or self._interrupt_event.is_set():
                    if audio_path:
                        self._safe_remove(audio_path)
                    self._is_speaking = False
                    self._text_queue.task_done()
                    continue

                # 2. Play audio via pygame mixer
                import pygame

                self._ensure_mixer()
                with self._mixer_lock:
                    pygame.mixer.music.load(audio_path)
                    pygame.mixer.music.play()

                # 3. Wait for playback while checking for interruption
                while True:
                    if self._interrupt_event.is_set():
                        with self._mixer_lock:
                            try:
                                pygame.mixer.music.stop()
                                pygame.mixer.music.unload()
                            except Exception:
                                pass
                        break
                    with self._mixer_lock:
                        busy = pygame.mixer.music.get_busy()
                    if not busy:
                        break
                    time.sleep(0.02)

            except Exception:
                pass
            finally:
                if audio_path:
                    self._safe_remove(audio_path)
                self._is_speaking = False
                self._text_queue.task_done()

    @staticmethod
    def _safe_remove(path: str) -> None:
        try:
            if os.path.exists(path):
                os.remove(path)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# 3. Speech-to-Text (STT) Engine (Lazy-Initialized)
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

        # Device selection preferences
        self.configured_device = device or getattr(config, "WHISPER_DEVICE", "auto")
        self.configured_compute = compute_type or getattr(config, "WHISPER_COMPUTE_TYPE", "int8")

        self.device, self.compute_type = self._resolve_device(self.configured_device, self.configured_compute)
        self._model: Any = None
        self._init_lock = threading.Lock()

    @property
    def model(self) -> Any:
        """Lazily initialize WhisperModel upon first actual transcription."""
        if self._model is None:
            with self._init_lock:
                if self._model is None:
                    self._model = self._init_model()
        return self._model

    @staticmethod
    def _resolve_device(device_pref: str, compute_pref: str) -> tuple[str, str]:
        """Resolve CUDA vs CPU availability with optimal quantization."""
        if device_pref == "cuda":
            return "cuda", compute_pref
        if device_pref == "cpu":
            return "cpu", "int8"

        try:
            import ctranslate2

            if ctranslate2.get_cuda_device_count() > 0:
                return "cuda", "int8_float16" if compute_pref == "int8" else compute_pref
        except Exception:
            pass
        return "cpu", "int8"

    def _init_model(self) -> Any:
        """Initialize faster-whisper WhisperModel with automatic fallback to CPU."""
        from faster_whisper import WhisperModel

        try:
            return WhisperModel(
                self.model_size,
                device=self.device,
                compute_type=self.compute_type,
            )
        except Exception:
            # Fallback to CPU int8 if CUDA libraries or drivers fail
            self.device = "cpu"
            self.compute_type = "int8"
            return WhisperModel("tiny.en", device="cpu", compute_type="int8")

    def transcribe(self, audio: np.ndarray) -> str:
        """Transcribe a 16kHz float32 audio buffer into text with sub-200ms latency."""
        if audio is None or len(audio) == 0:
            return ""

        if audio.dtype != np.float32:
            audio = audio.astype(np.float32)

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
        """Capture microphone audio buffer until the user pauses or stops speaking."""
        import sounddevice as sd

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
        return self.stt.listen(stop_event=stop_event)

    def speak(self, text: str, interrupt: bool = False) -> None:
        self.tts.speak(text, interrupt_current=interrupt)

    def speak_stream(self, token_stream: Iterable[str], interrupt: bool = False) -> None:
        self.tts.speak_stream(token_stream, interrupt_current=interrupt)

    def interrupt(self) -> None:
        self.tts.interrupt()
