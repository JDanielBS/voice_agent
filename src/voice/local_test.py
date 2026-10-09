"""Prueba interactiva local de voz (Micrófono -> STT -> Agente -> TTS -> Parlantes).

Permite probar toda la lógica y la voz de Azure directamente en tu máquina
sin necesidad de Twilio ni ngrok.

Si el SDK de Azure Speech no está instalado, cae a modo texto: se puede probar
toda la lógica del agente escribiendo por teclado.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from dotenv import load_dotenv

# Permitir ejecutar el script directo (python src/voice/local_test.py).
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

# Cargar variables de entorno
load_dotenv()

from src.agent.agent import process_turn

try:
    import azure.cognitiveservices.speech as speechsdk
except ImportError:  # sin SDK: modo texto
    speechsdk = None


def _create_speech_configs():
    key = os.environ.get("AZURE_SPEECH_KEY")
    region = os.environ.get("AZURE_SPEECH_REGION")

    if not key or not region:
        print("ERROR: Faltan AZURE_SPEECH_KEY o AZURE_SPEECH_REGION en tu archivo .env", file=sys.stderr)
        sys.exit(1)

    speech_config = speechsdk.SpeechConfig(subscription=key, region=region)
    speech_config.speech_recognition_language = "es-CO"
    speech_config.speech_synthesis_voice_name = "es-CO-SalomeNeural"
    return speech_config


def run_local_session():
    print("=" * 60)
    print("🎙️ PRUEBA DE CONVERSACIÓN LOCAL (Agente REPS)")
    print("=" * 60)

    recognizer = None
    synthesizer = None

    if speechsdk is None:
        print("⚠️ Azure Speech SDK no está instalado: modo texto (solo teclado).")
        print("   Instálalo con: pip install azure-cognitiveservices-speech\n")
    else:
        print("Inicializando Azure Speech con micrófono y parlantes por defecto...\n")
        speech_config = _create_speech_configs()

        # Entrada (micrófono)
        try:
            audio_in = speechsdk.audio.AudioConfig(use_default_microphone=True)
            recognizer = speechsdk.SpeechRecognizer(speech_config=speech_config, audio_config=audio_in)
        except Exception as e:
            print(f"Advertencia con micrófono: {e}. Podrás escribir por teclado si lo prefieres.")
            recognizer = None

        # Salida (parlantes): AudioOutputConfig, no AudioConfig.
        try:
            audio_out = speechsdk.audio.AudioOutputConfig(use_default_speaker=True)
            synthesizer = speechsdk.SpeechSynthesizer(speech_config=speech_config, audio_config=audio_out)
        except Exception as e:
            print(f"Advertencia con parlantes: {e}. Las respuestas se mostrarán solo en texto.")
            synthesizer = None

    session_id = "test_local_user"

    print("Instrucciones:")
    print(" - Presiona [ENTER] para hablar por tu micrófono (si está disponible).")
    print(" - O escribe tu pregunta directamente en texto si no deseas hablar.")
    print(" - Escribe 'salir' para terminar.\n")

    while True:
        try:
            user_input = input("\n[Presiona ENTER para hablar o escribe tu texto]: ").strip()

            if user_input.lower() in ("salir", "exit", "quit"):
                print("Finalizando prueba local...")
                break

            text = ""
            if not user_input:
                if recognizer is None:
                    print("Micrófono no disponible, escribe la pregunta directamente.")
                    continue
                print("🔴 Escuchando... habla ahora...")
                res = recognizer.recognize_once_async().get()
                if res.reason == speechsdk.ResultReason.RecognizedSpeech:
                    text = res.text
                    print(f"🗣️ Transcrito: '{text}'")
                elif res.reason == speechsdk.ResultReason.NoMatch:
                    print("⚠️ No se detectó ninguna palabra. Intenta de nuevo.")
                    continue
                elif res.reason == speechsdk.ResultReason.Canceled:
                    cancellation = res.cancellation_details
                    print(f"❌ Reconocimiento cancelado/error: {cancellation.reason} - {cancellation.error_details}")
                    continue
            else:
                text = user_input

            print("\n🤖 Consultando a la base de datos...")
            reply = process_turn(session_id, text)
            print(f"🤖 Agente dice:\n{reply}\n")

            if synthesizer:
                print("🔊 Reproduciendo respuesta en los parlantes...")
                synth_res = synthesizer.speak_text_async(reply).get()
                if synth_res.reason != speechsdk.ResultReason.SynthesizingAudioCompleted:
                    print(f"⚠️ Error en TTS: {synth_res.reason}")

        except KeyboardInterrupt:
            print("\nSesión interrumpida.")
            break
        except Exception as e:
            print(f"❌ Error inesperado: {e}")


if __name__ == "__main__":
    run_local_session()
