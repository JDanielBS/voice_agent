"""Azure Speech TTS — texto a audio mulaw 8kHz.

Sintetiza el enunciado completo en memoria y lo entrega troceado a 400 ms.
Timeout de 3 s (Regla 12 de CLAUDE.md).

ponytail: no es streaming real. Se espera a que Azure cierre la síntesis
(`speak_text_async(...).get()`) y luego se trocea `AudioDataStream`. La lectura
en vivo de `PullAudioOutputStream.read()` se bloqueaba para siempre al llegar
al EOF: dejaba un hilo atascado por turno y, al llenarse el pool, congelaba la
sesión. Con respuestas de 1-2 frases el coste (<1,5 s) entra en presupuesto;
si hay que hablar antes del primer fragmento, usar el evento `synthesizing`
con una cola explícita, que sí cierra.
"""
from __future__ import annotations

import os

import azure.cognitiveservices.speech as speechsdk

TTS_TIMEOUT_S = 3  # Regla 12

CHUNK_BYTES = 3200  # 400 ms de mulaw a 8 kHz


def _speech_config() -> speechsdk.SpeechConfig:
    key = os.environ["AZURE_SPEECH_KEY"]
    region = os.environ["AZURE_SPEECH_REGION"]
    config = speechsdk.SpeechConfig(subscription=key, region=region)
    # Formato nativo para el canal telefónico (mulaw 8 kHz)
    config.set_speech_synthesis_output_format(
        speechsdk.SpeechSynthesisOutputFormat.Raw8Khz8BitMonoMULaw)
    # Voz neuronal en español de Colombia
    config.speech_synthesis_voice_name = "es-CO-SalomeNeural"
    return config


def synthesize_stream(text: str):
    """Genera el audio mulaw 8 kHz del texto en chunks de 400 ms."""
    if not text.strip():
        return

    # audio_config=None: sintetiza a memoria, sin dispositivo ni reproducción.
    synthesizer = speechsdk.SpeechSynthesizer(
        speech_config=_speech_config(), audio_config=None)
    result = synthesizer.speak_text_async(text).get()

    if result.reason != speechsdk.ResultReason.SynthesizingAudioCompleted:
        print(f"[TTS] Síntesis no completada: {result.reason}")
        return

    stream = speechsdk.AudioDataStream(result)
    # read_data escribe en el objeto `bytes` in-place (API del SDK).
    buffer = bytes(CHUNK_BYTES)
    while True:
        read = stream.read_data(buffer)
        if read <= 0:
            break
        yield buffer[:read]


if __name__ == "__main__":
    from dotenv import load_dotenv
    load_dotenv()

    print("Sintetizando de prueba...")
    chunks = list(synthesize_stream("Hola, esto es una prueba de síntesis."))
    print(f"Total chunks: {len(chunks)}, Total bytes: {sum(len(c) for c in chunks)}")
