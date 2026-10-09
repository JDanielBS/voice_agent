"""VAD por energía: detecta fin de turno del usuario.

Umbral de silencio ~700 ms como punto de partida (ARQUITECTURA.md §10.2).
Trabaja sobre frames PCM 16-bit signed, 8 kHz mono.
"""
from __future__ import annotations

import struct

# Calibración: ajustar con llamadas reales (depende de acento, línea, ruido)
SILENCE_THRESHOLD = 500       # amplitud RMS bajo la cual se considera silencio
SILENCE_DURATION_MS = 700     # ms de silencio para marcar fin de turno
SAMPLE_RATE = 8000
BYTES_PER_SAMPLE = 2          # PCM 16-bit


def rms(pcm_bytes: bytes) -> float:
    """RMS de un frame PCM 16-bit signed little-endian."""
    if len(pcm_bytes) < BYTES_PER_SAMPLE:
        return 0.0
    n_samples = len(pcm_bytes) // BYTES_PER_SAMPLE
    samples = struct.unpack(f"<{n_samples}h", pcm_bytes[:n_samples * BYTES_PER_SAMPLE])
    if not samples:
        return 0.0
    return (sum(s * s for s in samples) / n_samples) ** 0.5


class VAD:
    """Detector de actividad de voz basado en energía.

    Uso:
        vad = VAD()
        for chunk in audio_chunks:
            event = vad.process(chunk)
            if event == "speech_end":
                # el usuario terminó de hablar
            elif event == "speech_start":
                # el usuario empezó a hablar (para barge-in)
    """

    def __init__(self, threshold: int = SILENCE_THRESHOLD,
                 silence_ms: int = SILENCE_DURATION_MS):
        self.threshold = threshold
        self.silence_ms = silence_ms
        self._silent_bytes = 0
        self._speaking = False
        # Bytes de silencio que equivalen al umbral de duración
        self._silence_limit = int(SAMPLE_RATE * BYTES_PER_SAMPLE * silence_ms / 1000)

    def process(self, pcm_chunk: bytes) -> str | None:
        """Procesa un chunk PCM y devuelve evento o None.

        Eventos:
          "speech_start" — el usuario empezó a hablar
          "speech_end"   — silencio sostenido >= silence_ms
        """
        energy = rms(pcm_chunk)

        if energy < self.threshold:
            self._silent_bytes += len(pcm_chunk)
            if self._speaking and self._silent_bytes >= self._silence_limit:
                self._speaking = False
                self._silent_bytes = 0
                return "speech_end"
        else:
            was_silent = not self._speaking
            self._speaking = True
            self._silent_bytes = 0
            if was_silent:
                return "speech_start"
        return None

    def reset(self):
        self._silent_bytes = 0
        self._speaking = False


# ── Verificación ─────────────────────────────────────────────────────────────

def demo():
    """Prueba rápida con audio sintético."""
    import math

    # Generar 1s de tono + 1s de silencio
    tone = b""
    for i in range(SAMPLE_RATE):
        val = int(10000 * math.sin(2 * math.pi * 440 * i / SAMPLE_RATE))
        tone += struct.pack("<h", val)
    silence = b"\x00\x00" * SAMPLE_RATE

    vad = VAD()
    chunk_size = 640  # 40ms a 8kHz 16-bit

    events = []
    audio = tone + silence
    for offset in range(0, len(audio), chunk_size):
        ev = vad.process(audio[offset:offset + chunk_size])
        if ev:
            events.append(ev)

    assert "speech_start" in events, f"no detectó inicio de habla: {events}"
    assert "speech_end" in events, f"no detectó fin de habla: {events}"
    print(f"ok: VAD detectó {events}")


if __name__ == "__main__":
    demo()

