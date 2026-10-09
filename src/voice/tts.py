"""Azure Speech TTS — streaming.

Sintetiza texto a audio mulaw 8kHz para Twilio Media Streams.
Timeout de 3s (Regla 12 de CLAUDE.md).
"""
from __future__ import annotations

import os
import time
from typing import Generator

import azure.cognitiveservices.speech as speechsdk

TTS_TIMEOUT_S = 3  # Regla 12


def _speech_config() -> speechsdk.SpeechConfig:
    key = os.environ["AZURE_SPEECH_KEY"]
    region = os.environ["AZURE_SPEECH_REGION"]
    config = speechsdk.SpeechConfig(subscription=key, region=region)
    # Formato nativo para Twilio (mulaw 8kHz)
    config.set_speech_synthesis_output_format(speechsdk.SpeechSynthesisOutputFormat.Raw8Khz8BitMonoMULaw)
    # Voz neuronal en español de Colombia
    config.speech_synthesis_voice_name = "es-CO-SalomeNeural"
    return config


def synthesize_stream(text: str) -> Generator[bytes, None, None]:
    """Sintetiza texto y genera chunks de audio en streaming (mulaw 8kHz)."""
    if not text.strip():
        return

    config = _speech_config()
    pull_stream = speechsdk.audio.PullAudioOutputStream()
    stream_config = speechsdk.audio.AudioOutputConfig(stream=pull_stream)
    
    synthesizer = speechsdk.SpeechSynthesizer(speech_config=config, audio_config=stream_config)

    # Iniciar la síntesis
    # No usamos speak_text_async() con wait simple porque queremos extraer audio
    # a medida que se genera, pero debemos protegernos con el timeout.
    
    result_future = synthesizer.speak_text_async(text)
    
    # Preparar buffer de lectura
    buffer_size = 3200  # 400ms de mulaw a 8kHz
    audio_buffer = bytes(buffer_size)
    
    start_time = time.time()
    first_chunk_received = False
    
    while True:
        # Check timeout para el primer chunk
        if not first_chunk_received and (time.time() - start_time) > TTS_TIMEOUT_S:
            print("[TTS] Timeout esperando el primer chunk de audio.")
            synthesizer.stop_speaking_async()
            return
            
        bytes_read = pull_stream.read(audio_buffer)
        if bytes_read == 0:
            break
            
        first_chunk_received = True
        yield audio_buffer[:bytes_read]
        
    # Verificar si hubo un error al finalizar
    try:
        # Ya terminó, no debería bloquear
        result = result_future.get()
        if result.reason == speechsdk.ResultReason.Canceled:
            print(f"[TTS] Síntesis cancelada: {result.cancellation_details.reason}")
    except Exception as e:
        print(f"[TTS] Error al finalizar la síntesis: {e}")

if __name__ == "__main__":
    from dotenv import load_dotenv
    load_dotenv()
    
    print("Sintetizando de prueba...")
    chunks = list(synthesize_stream("Hola, esto es una prueba de síntesis en tiempo real."))
    print(f"Total chunks: {len(chunks)}, Total bytes: {sum(len(c) for c in chunks)}")
