"""Servidor WebSocket para el agente de voz por navegador.

Sirve el panel frontend y un WebSocket `/demo` para la llamada: 
recibe PCM 16-bit 8kHz mono, corre el motor de voz (VAD -> STT -> process_turn -> TTS) 
y empuja a la UI la transcripción diarizada, el sentimiento y el audio de la respuesta.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware

load_dotenv()  # Cargar variables de entorno antes de importar modulos que usan os.environ

from src.agent.agent import process_turn
from src.agent.state import manager as state_manager
from src.voice.stt import StreamingSTT
from src.voice.tts import synthesize_stream
from src.voice.vad import VAD

app = FastAPI(title="Voice Agent API")

# Configurar CORS para permitir que cualquier frontend pueda consumir la API
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # En producción estricta, usar los dominios permitidos
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

_PANEL = Path(__file__).parent / "static" / "panel.html"

@app.get("/")
async def panel():
    return FileResponse(_PANEL)

async def _play_tts(ws: WebSocket, text: str) -> None:
    """Sintetiza y envía el audio (mulaw 8kHz) como frames binarios al navegador."""
    gen = synthesize_stream(text)
    while True:
        try:
            chunk = await asyncio.to_thread(next, gen)
        except StopIteration:
            break
        except Exception as e:
            print(f"[TTS] Error: {e}")
            break
        
        if chunk:
            try:
                await ws.send_bytes(chunk)
            except Exception:
                # El socket se cerró mientras enviábamos
                break

@app.websocket("/demo")
async def demo_endpoint(ws: WebSocket):
    await ws.accept()
    session_id = f"demo_{id(ws)}"
    state_manager.get(session_id).clear()

    vad = VAD()
    stt = StreamingSTT()
    stt.start()
    
    print(f"[WS] Nueva conexión iniciada: {session_id}")

    try:
        while True:
            try:
                pcm = await ws.receive_bytes()  # PCM 16-bit 8kHz mono LE del navegador
            except WebSocketDisconnect:
                print(f"[WS] Desconexión (receive): {session_id}")
                break

            stt.push_chunk(pcm)
            if vad.process(pcm) != "speech_end":
                continue

            text = await asyncio.to_thread(stt.finish_and_get_text)
            
            # reiniciar STT/VAD para el próximo turno
            stt = StreamingSTT()
            stt.start()
            vad.reset()

            if not text.strip():
                continue

            print(f"[WS] Usuario ({session_id}): {text}")
            
            try:
                await ws.send_json({"role": "user", "text": text})
            except WebSocketDisconnect:
                break

            reply = await asyncio.to_thread(process_turn, session_id, text)
            sentimiento = state_manager.get(session_id).last_sentimiento
            
            print(f"[WS] Agente ({session_id}): {reply} [{sentimiento}]")
            
            try:
                await ws.send_json({"role": "agent", "text": reply, "sentimiento": sentimiento})
                await _play_tts(ws, reply)
            except WebSocketDisconnect:
                break

    except Exception as e:
        print(f"[WS] Error en {session_id}: {e}")
    finally:
        print(f"[WS] Limpiando sesión {session_id}")
        stt.cancel()
        state_manager.get(session_id).clear()

if __name__ == "__main__":
    import uvicorn
    # Para producción, es mejor usar Gunicorn o Uvicorn con workers desde la línea de comandos
    uvicorn.run("src.voice.server:app", host="0.0.0.0", port=7861, workers=1)
