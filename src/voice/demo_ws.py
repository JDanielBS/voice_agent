"""Servidor de demo por navegador (NO es Twilio).

Sirve el panel y un WebSocket `/demo` que usa el micrófono del navegador como
si fuera la llamada: recibe PCM 16-bit 8kHz mono, corre el mismo motor de voz
(VAD -> STT -> process_turn -> TTS) y empuja a la UI la transcripción diarizada,
el sentimiento y el audio de la respuesta.

El camino de Twilio (`twilio_ws.py`, puerto 7860) no se toca. Este corre en 7861.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse

load_dotenv()  # stt.py/tts.py leen os.environ directo; cargar antes de instanciarlos

from src.agent.agent import process_turn
from src.agent.state import manager as state_manager
from src.voice.stt import StreamingSTT
from src.voice.tts import synthesize_stream
from src.voice.vad import VAD

app = FastAPI()

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
        if chunk:
            await ws.send_bytes(chunk)


@app.websocket("/demo")
async def demo_endpoint(ws: WebSocket):
    await ws.accept()
    session_id = f"demo_{id(ws)}"
    state_manager.get(session_id).clear()

    vad = VAD()
    stt = StreamingSTT()
    stt.start()

    try:
        while True:
            pcm = await ws.receive_bytes()  # PCM 16-bit 8kHz mono LE del navegador

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

            await ws.send_json({"role": "user", "text": text})

            reply = await asyncio.to_thread(process_turn, session_id, text)
            sentimiento = state_manager.get(session_id).last_sentimiento
            await ws.send_json({"role": "agent", "text": reply,
                                "sentimiento": sentimiento})
            await _play_tts(ws, reply)

    except WebSocketDisconnect:
        pass
    finally:
        stt.cancel()


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("src.voice.demo_ws:app", host="0.0.0.0", port=7861, reload=True)
