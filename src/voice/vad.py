"""VAD adaptativo por energía: detecta fin de turno del usuario.

Un umbral fijo (el anterior: amplitud 500) falla en los dos extremos de una
llamada real de 8 kHz:

- Voz débil o micrófono con poca ganancia: nunca supera 500, nunca se marca
  `speech_start`, y como `speech_end` exige haber entrado en habla, el turno
  jamás cierra. El usuario termina de hablar y el agente se queda esperando.
- Ruido de línea constante: se queda por encima de 500 y el silencio nunca se
  acumula, así que tampoco cierra.

Aquí el umbral se estima en vivo: se sigue el piso de ruido con una media
exponencial y se exige que la voz lo supere por un factor. El descenso del piso
es rápido (para engancharse al silencio al inicio) y el ascenso lento (para que
un grito no lo suba de golpe). Con histéresis para no cortar palabras.

Trabaja sobre frames PCM 16-bit signed, 8 kHz mono.

ponytail: sigue siendo VAD de energía, no espectral. Una voz y un ventilador
constante con la misma energía son indistinguibles; el techo se levanta
cambiando a un VAD espectral (p. ej. WebRTC VAD) si la demo lo exige.
"""
from __future__ import annotations

import struct

SAMPLE_RATE = 8000
BYTES_PER_SAMPLE = 2  # PCM 16-bit

# Piso de ruido (RMS). Arranca alto y baja rápido para no dispararse con el
# hum de la línea; se topa para que un ruido fuerte no vuelva sordo al VAD.
NOISE_START = 300.0
NOISE_MIN = 25.0
NOISE_MAX = 1500.0
NOISE_DOWN = 0.25   # ataque: bajar el piso hacia el silencio
NOISE_UP = 0.02     # release: subir el piso solo si el ruido persiste

# Umbrales relativos al piso de ruido (con histéresis).
START_RATIO = 2.5   # para declarar que empieza el habla
END_RATIO = 1.6     # para declarar que termina (más bajo: no cortar palabras)
MIN_START = 150.0   # piso absoluto: por debajo de esto es ruido de fondo
MIN_END = 110.0

# Temporización.
SILENCE_DURATION_MS = 700   # silencio sostenido para cerrar el turno
MIN_SPEECH_MS = 150         # ignorar chasquidos / golpes de < 150 ms
WARMUP_MS = 300             # primeros ms: solo calibrar, no emitir eventos


def rms(pcm_bytes: bytes) -> float:
    """RMS de un frame PCM 16-bit signed little-endian."""
    if len(pcm_bytes) < BYTES_PER_SAMPLE:
        return 0.0
    n_samples = len(pcm_bytes) // BYTES_PER_SAMPLE
    samples = struct.unpack(f"<{n_samples}h", pcm_bytes[:n_samples * BYTES_PER_SAMPLE])
    if not samples:
        return 0.0
    return (sum(s * s for s in samples) / n_samples) ** 0.5


def _ms_to_bytes(ms: int) -> int:
    return int(SAMPLE_RATE * BYTES_PER_SAMPLE * ms / 1000)


class VAD:
    """Detector de actividad de voz por energía adaptativa.

    Uso:
        vad = VAD()
        for chunk in audio_chunks:
            event = vad.process(chunk)
            if event == "speech_end":
                # el usuario terminó de hablar
            elif event == "speech_start":
                # el usuario empezó a hablar (para barge-in)
    """

    def __init__(self, noise_start: float = NOISE_START,
                 silence_ms: int = SILENCE_DURATION_MS,
                 min_speech_ms: int = MIN_SPEECH_MS,
                 warmup_ms: int = WARMUP_MS):
        self.silence_ms = silence_ms
        self.min_speech_ms = min_speech_ms
        self.warmup_ms = warmup_ms
        self.noise = float(noise_start)
        self.level = 0.0            # último RMS leído, para depurar/UI
        self._total_bytes = 0
        self._speaking = False
        self._silent_bytes = 0
        self._speech_bytes = 0
        self._silence_limit = _ms_to_bytes(silence_ms)
        self._min_speech_bytes = _ms_to_bytes(min_speech_ms)
        self._warmup_bytes = _ms_to_bytes(warmup_ms)

    @property
    def is_speaking(self) -> bool:
        return self._speaking

    @property
    def start_threshold(self) -> float:
        return max(self.noise * START_RATIO, MIN_START)

    @property
    def end_threshold(self) -> float:
        return max(self.noise * END_RATIO, MIN_END)

    def process(self, pcm_chunk: bytes) -> str | None:
        """Procesa un chunk PCM y devuelve evento o None.

        Eventos:
          "speech_start" — el usuario empezó a hablar
          "speech_end"   — silencio sostenido >= silence_ms
        """
        energy = rms(pcm_chunk)
        self.level = energy
        self._total_bytes += len(pcm_chunk)
        warmed = self._total_bytes >= self._warmup_bytes
        start_th = self.start_threshold

        if not self._speaking:
            if warmed and energy > start_th:
                self._speech_bytes += len(pcm_chunk)
                if self._speech_bytes >= self._min_speech_bytes:
                    self._speaking = True
                    self._speech_bytes = 0
                    self._silent_bytes = 0
                    return "speech_start"
            else:
                self._speech_bytes = 0
                if energy <= start_th:
                    # Solo el audio claramente no-hablado calibra el piso.
                    alpha = NOISE_DOWN if energy < self.noise else NOISE_UP
                    self.noise += alpha * (energy - self.noise)
                    self.noise = min(max(self.noise, NOISE_MIN), NOISE_MAX)
            return None

        if energy < self.end_threshold:
            self._silent_bytes += len(pcm_chunk)
        else:
            self._silent_bytes = 0

        if self._silent_bytes >= self._silence_limit:
            self._speaking = False
            self._silent_bytes = 0
            self._speech_bytes = 0
            return "speech_end"
        return None

    def reset(self):
        self._speaking = False
        self._silent_bytes = 0
        self._speech_bytes = 0


# ── Verificación ─────────────────────────────────────────────────────────────

def _tone(ms: int, amplitude: int) -> bytes:
    """Tono 440 Hz de `ms` milisegundos con la amplitud dada (RMS ≈ amp/√2)."""
    import math
    n = int(SAMPLE_RATE * ms / 1000)
    return b"".join(
        struct.pack("<h", int(amplitude * math.sin(2 * math.pi * 440 * i / SAMPLE_RATE)))
        for i in range(n)
    )


def _noise(ms: int, amplitude: int) -> bytes:
    """Ruido de banda ancha determinista con la amplitud de pico dada."""
    n = int(SAMPLE_RATE * ms / 1000)
    return b"".join(
        struct.pack("<h", ((i * 2654435761) % (2 * amplitude + 1)) - amplitude)
        for i in range(n)
    )


def _run(vad: VAD, audio: bytes, chunk_size: int = 640) -> list[str]:
    events = []
    for offset in range(0, len(audio), chunk_size):
        ev = vad.process(audio[offset:offset + chunk_size])
        if ev:
            events.append(ev)
    return events


def demo():
    """Prueba con audio sintético: tono fuerte, voz débil y ruido constante."""
    # 1. Silencio + voz clara + silencio → debe abrir y cerrar.
    audio = _noise(400, 20) + _tone(900, 10000) + _noise(1200, 20)
    events = _run(VAD(), audio)
    assert "speech_start" in events, f"no detectó inicio de habla: {events}"
    assert "speech_end" in events, f"no detectó fin de habla: {events}"

    # 2. Voz débil (RMS ≈ 280) sobre ruido de fondo suave (RMS ≈ 40). Con el
    #    umbral fijo de 500 esto NUNCA cerraba el turno.
    audio = _noise(600, 55) + _tone(900, 400) + _noise(1200, 55)
    events = _run(VAD(), audio)
    assert "speech_start" in events, f"no detectó voz débil: {events}"
    assert "speech_end" in events, f"no cerró la voz débil: {events}"

    # 3. Ruido de línea constante, sin voz: no debe inventar un turno.
    events = _run(VAD(), _noise(3000, 400))
    assert "speech_start" not in events, f"disparó con ruido constante: {events}"
    assert "speech_end" not in events, f"cerró con ruido constante: {events}"

    # 4. Chasquido corto no debe abrir turno.
    events = _run(VAD(), _noise(1000, 10) + _tone(60, 12000) + _noise(1000, 10))
    assert "speech_end" not in events, f"cerró con un chasquido: {events}"

    print(f"ok: VAD adaptativo — voz clara, voz débil y ruido constante manejados")


if __name__ == "__main__":
    demo()
