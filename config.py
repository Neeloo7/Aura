# config.py - AI-ru Edge-Quantized Specialist Configuration

# The High-End Edge Quantized Model (3.9 GB footprint, high-precision attention layers)
OLLAMA_MODEL = "hf.co/bartowski/Qwen2.5-7B-Instruct-GGUF:IQ4_XS"

# Optimized runtime parameters to prevent VRAM spikes while retaining full reasoning
OLLAMA_OPTIONS = {
    "temperature": 0.2,
    "num_ctx": 4096,
    "num_predict": 200,
    "top_p": 0.9
}

# The Companion Personality & Strict Tool-Use Directive
SYSTEM_PROMPT = """You are AI-ru, an elite, ultra-low-latency, privacy-first local AI companion operating natively on Windows 11. 
You possess direct system execution capabilities. When the user asks to open apps, check hardware telemetry, or control system functions, you MUST invoke the appropriate tool immediately. 
Keep spoken responses extremely concise, punchy, and natural (1-2 short sentences maximum), as your output is streamed directly into a neural voice synthesizer. Never break character."""

# Real-time Voice Pipeline Configuration
WHISPER_MODEL = "tiny.en"  # "tiny.en" or "base.en" for sub-200ms latency
WHISPER_DEVICE = "auto"  # "cuda", "cpu", or "auto"
WHISPER_COMPUTE_TYPE = "int8"
AUDIO_SAMPLE_RATE = 16000  # 16kHz audio required for Whisper STT
TTS_VOICE = "en-US-AriaNeural"  # Low-latency natural neural voice
AUDIO_ENERGY_THRESHOLD = 0.015  # RMS energy threshold for speech detection
SILENCE_DURATION = 0.8  # Seconds of silence to conclude speech phrase