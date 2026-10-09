"""Azure Speech STT — streaming sobre PushAudioInputStream.

Recibe chunks PCM 16-bit 8kHz del WebSocket de Twilio (ya decodificados de
mulaw), los empuja al SDK y devuelve texto transcrito.

Timeout de 2 s (Regla 12 de CLAUDE.md).
"""
from __future__ import annotations

import os
import threading
from typing import Callable

import azure.cognitiveservices.speech as speechsdk

STT_TIMEOUT_S = 2  # Regla 12


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

        Bloquea hasta obtener resultado o hasta STT_TIMEOUT_S.
        Devuelve cadena vacía si no reconoció nada.
        """
        if not self._stream or not self._recognizer:
            return ""

        self._stream.close()

        result_text = ""
        done = threading.Event()

        def on_recognized(evt):
            nonlocal result_text
            if evt.result.reason == speechsdk.ResultReason.RecognizedSpeech:
                result_text = evt.result.text
            done.set()

        def on_canceled(evt):
            done.set()

        self._recognizer.recognized.connect(on_recognized)
        self._recognizer.canceled.connect(on_canceled)

        # recognize_once_async es el más simple: un solo enunciado
        future = self._recognizer.recognize_once_async()
        try:
            result = future.get()  # bloquea
            if result.reason == speechsdk.ResultReason.RecognizedSpeech:
                result_text = result.text
        except Exception:
            pass

        self._cleanup()
        return result_text

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

