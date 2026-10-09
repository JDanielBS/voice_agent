"""Azure Speech STT — streaming sobre PushAudioInputStream.

Recibe chunks PCM 16-bit 8kHz del WebSocket de Twilio (ya decodificados de
mulaw), los empuja al SDK y devuelve texto transcrito.

Regla 12: timeout de 2 s de referencia. La voz real tarda ~2-3 s en
endpointizar, así que el resultado se espera en STT_RESULT_WAIT_S; si no llega,
se espera el cierre de sesión (STT_SETTLE_S) para no dejar sesiones de Azure
vivas que tiran las transcripciones siguientes del mismo proceso.
"""
from __future__ import annotations

import logging
import os
import threading

import azure.cognitiveservices.speech as speechsdk

log = logging.getLogger(__name__)

STT_TIMEOUT_S = 2       # Regla 12: ventana esperada para voz real
STT_RESULT_WAIT_S = 4.5  # Azure endpointiza la voz en ~2-3 s
STT_SETTLE_S = 3.5       # sin resultado: esperar el cierre de sesión
STT_MAX_WAIT_S = STT_RESULT_WAIT_S + STT_SETTLE_S


def _speech_config() -> speechsdk.SpeechConfig:
    key = os.environ["AZURE_SPEECH_KEY"]
    region = os.environ["AZURE_SPEECH_REGION"]
    config = speechsdk.SpeechConfig(subscription=key, region=region)
    config.speech_recognition_language = "es-CO"
    # ponytail: se puede agregar phrase list con vocab.json para sesgar
    # el reconocimiento hacia nombres de dimensiones del dataset.
    return config


class StreamingSTT:
    """STT en streaming: se alimenta con chunks PCM y produce texto."""

    def __init__(self):
        self._config = _speech_config()
        self._stream: speechsdk.audio.PushAudioInputStream | None = None
        self._recognizer: speechsdk.SpeechRecognizer | None = None

    def start(self) -> None:
        """Inicia una sesión de reconocimiento."""
        fmt = speechsdk.audio.AudioStreamFormat(
            samples_per_second=8000,
            bits_per_sample=16,
            channels=1,
        )
        self._stream = speechsdk.audio.PushAudioInputStream(stream_format=fmt)
        audio_cfg = speechsdk.audio.AudioConfig(stream=self._stream)
        self._recognizer = speechsdk.SpeechRecognizer(
            speech_config=self._config,
            audio_config=audio_cfg,
        )

    def push_chunk(self, pcm_bytes: bytes) -> None:
        """Empuja audio PCM 16-bit 8kHz al stream del recognizer."""
        if self._stream:
            self._stream.write(pcm_bytes)

    def finish_and_get_text(self) -> str:
        """Cierra el stream de audio y obtiene la transcripción.

        Bloquea (en un hilo de worker) hasta obtener resultado, o hasta que la
        sesión de Azure cierra. Nunca abandona una sesión abierta: dejarla viva
        al acumularse agotaba el límite de sesiones concurrentes y tiraba las
        transcripciones siguientes (síntoma: "STT: timeout" desde el segundo
        turno, incluso tras reconectar). Devuelve "" si no reconoció nada.
        """
        if not self._stream or not self._recognizer:
            return ""

        self._stream.close()

        done = threading.Event()
        result_text = [""]

        def on_recognized(evt):
            if evt.result.reason == speechsdk.ResultReason.RecognizedSpeech:
                result_text[0] = evt.result.text
            done.set()

        def on_stop(evt):
            done.set()

        self._recognizer.recognized.connect(on_recognized)
        self._recognizer.session_stopped.connect(on_stop)
        self._recognizer.canceled.connect(on_stop)
        self._recognizer.recognize_once_async()

        if not done.wait(STT_RESULT_WAIT_S):
            log.warning("STT: sin resultado en %.1fs; esperando cierre de sesión (%.1fs)",
                        STT_RESULT_WAIT_S, STT_SETTLE_S)
            done.wait(STT_SETTLE_S)

        self._cleanup()
        return result_text[0]

    def cancel(self) -> None:
        """Cancela reconocimiento en curso (para barge-in)."""
        self._cleanup()

    def _cleanup(self):
        if self._stream:
            try:
                self._stream.close()
            except Exception:
                pass
        self._stream = None
        self._recognizer = None


def transcribe_pcm(pcm_audio: bytes) -> str:
    """Utilidad de conveniencia: transcribe un bloque completo de audio PCM."""
    stt = StreamingSTT()
    stt.start()
    # Alimentar en chunks de 3200 bytes (200ms a 8kHz 16-bit)
    chunk_size = 3200
    for i in range(0, len(pcm_audio), chunk_size):
        stt.push_chunk(pcm_audio[i:i + chunk_size])
    return stt.finish_and_get_text()

