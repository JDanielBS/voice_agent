"""Servidor WebSocket para el agente de voz por navegador.

Sirve el panel frontend y un WebSocket `/demo` para la llamada: 
recibe PCM 16-bit 8kHz mono, corre el motor de voz (VAD -> STT -> process_turn -> TTS) 
y empuja a la UI la transcripción diarizada, el sentimiento y el audio de la respuesta.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware

load_dotenv()  # Cargar variables de entorno antes de importar modulos que usan os.environ

from src.logging_setup import setup_logging
from src.agent.agent import process_turn
from src.agent.state import manager as state_manager
from src.voice.stt import STT_MAX_WAIT_S, StreamingSTT
from src.voice.tts import TTS_TIMEOUT_S, synthesize_stream
from src.voice.vad import VAD

setup_logging()
log = logging.getLogger("src.voice.server")

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
_SCHEMA = Path(__file__).resolve().parents[2] / "data" / "schema.json"

# Sin estas el turno revienta en el primer `process_turn` y la llamada se queda
# muda. Se comprueban al arrancar para que el fallo salga en el log del deploy
# y no a mitad de una llamada.
_REQUIRED_ENV = (
    "AZURE_SPEECH_KEY", "AZURE_SPEECH_REGION",
    "AZURE_OPENAI_API_KEY", "AZURE_OPENAI_ENDPOINT",
    "AZURE_OPENAI_DEPLOYMENT", "AZURE_OPENAI_API_VERSION",
    "DATABASE_URL",
)

def _readiness() -> dict:
    faltan = [k for k in _REQUIRED_ENV if not os.environ.get(k)]
    if not _SCHEMA.exists():
        faltan.append("data/schema.json (regenerar con: python -m src.schema.infer)")
    return {"ok": not faltan, "faltan": faltan}

_ready = _readiness()
if not _ready["ok"]:
    log.error("FALTA CONFIGURACIÓN — el agente no podrá responder: %s",
              "; ".join(_ready["faltan"]))
else:
    log.info("Configuración completa; %d variables requeridas presentes.",
             len(_REQUIRED_ENV))

@app.get("/")
async def panel():
    return FileResponse(_PANEL)

@app.get("/health")
async def health():
    """Diagnóstico del deploy sin tener que hacer una llamada."""
    return _readiness()

# ponytail: regla 9 pide WAV pregrabado; aquí va por TTS porque data/audio/ no
# se genera todavía. Si cae el TTS, al menos el texto llega a la transcripción.
_FALLBACK = ("Disculpa, tuve un problema técnico al consultar la base de datos. "
             "¿Puedes repetir tu pregunta?")

_TTS_DONE = object()

def _next_chunk(gen):
    """Avanza el generador sin dejar escapar StopIteration.

    `asyncio.to_thread(next, gen)` cuelga la sesión: run_in_executor no puede
    poner StopIteration en un Future (Python lo prohíbe con "StopIteration
    interacts badly with generators") y el `await` nunca se reanuda. Resultado:
    tras la primera respuesta el WebSocket dejaba de leer audio para siempre.
    """
    try:
        return next(gen)
    except StopIteration:
        return _TTS_DONE

async def _play_tts(ws: WebSocket, text: str) -> None:
    """Sintetiza y envía el audio (mulaw 8kHz) como frames binarios al navegador."""
    started = time.perf_counter()
    first_at: float | None = None
    frames = 0
    sent = 0
    gen = synthesize_stream(text)
    try:
        while True:
            try:
                chunk = await asyncio.wait_for(
                    asyncio.to_thread(_next_chunk, gen), timeout=TTS_TIMEOUT_S
                )
            except asyncio.TimeoutError:
                log.warning("TTS: timeout esperando audio; se corta la respuesta.")
                break
            except Exception as e:
                log.warning("TTS: error generando audio: %s", e)
                break

            if chunk is _TTS_DONE:
                break
            if chunk:
                if first_at is None:
                    first_at = time.perf_counter()
                try:
                    await ws.send_bytes(chunk)
                    frames += 1
                    sent += len(chunk)
                except Exception:
                    # El socket se cerró mientras enviábamos
                    log.info("TTS: socket cerrado durante el envío (frames=%d)", frames)
                    break
    finally:
        try:
            gen.close()
        except Exception:
            pass

    elapsed_ms = (time.perf_counter() - started) * 1000
    first_ms = (first_at - started) * 1000 if first_at else None
    log.info(
        "TTS: %d frames, %d bytes, primer audio en %s ms, total %d ms",
        frames, sent,
        f"{first_ms:.0f}" if first_ms is not None else "-",
        round(elapsed_ms),
    )

@app.websocket("/demo")
async def demo_endpoint(ws: WebSocket):
    await ws.accept()
    session_id = f"demo_{id(ws)}"
    state_manager.get(session_id).clear()

    vad = VAD()
    stt = StreamingSTT()
    stt.start()

    log.info("WS: conexión iniciada session=%s", session_id)

    try:
        while True:
            try:
                pcm = await ws.receive_bytes()  # PCM 16-bit 8kHz mono LE del navegador
            except WebSocketDisconnect:
                log.info("WS: desconexión (receive) session=%s", session_id)
                break

            stt.push_chunk(pcm)
            event = vad.process(pcm)
            if event == "speech_start":
                log.debug("VAD: inicio de habla session=%s nivel=%.0f",
                          session_id, vad.level)
            if event != "speech_end":
                continue

            log.info(
                "Turno: fin de habla session=%s nivel=%.0f ruido=%.0f umbral_inicio=%.0f",
                session_id, vad.level, vad.noise, vad.start_threshold,
            )

            # Regla 12: el turno no puede colgarse. finish_and_get_text ya espera
            # con cota interna (resultado + cierre de sesión); este wait_for solo
            # es red de seguridad, arriba de la cota para no cancelar la sesión.
            t_stt = time.perf_counter()
            try:
                text = await asyncio.wait_for(
                    asyncio.to_thread(stt.finish_and_get_text),
                    timeout=STT_MAX_WAIT_S + 1,
                )
            except asyncio.TimeoutError:
                log.warning("STT: timeout esperando la transcripción session=%s",
                            session_id)
                text = ""
            log.info("STT: %d ms session=%s texto=%r",
                     round((time.perf_counter() - t_stt) * 1000), session_id,
                     text[:200])
            # reiniciar STT/VAD para el próximo turno
            stt = StreamingSTT()
            stt.start()
            vad.reset()

            if not text.strip():
                continue

            try:
                await ws.send_json({"role": "user", "text": text})
            except WebSocketDisconnect:
                break

            # Regla 7: ningún fallo del turno puede terminar en silencio ni
            # tumbar la llamada. Se responde con la frase fija y se sigue.
            t_agent = time.perf_counter()
            try:
                reply = await asyncio.to_thread(process_turn, session_id, text)
                sentimiento = state_manager.get(session_id).last_sentimiento
            except Exception:
                log.exception("Agente: error procesando el turno session=%s", session_id)
                reply, sentimiento = _FALLBACK, "neutro"
            log.info("Agente: %d ms session=%s sentimiento=%s respuesta=%r",
                     round((time.perf_counter() - t_agent) * 1000),
                     session_id, sentimiento, reply[:300])

            try:
                await ws.send_json({"role": "agent", "text": reply, "sentimiento": sentimiento})
                await _play_tts(ws, reply)
            except WebSocketDisconnect:
                break

    except Exception:
        log.exception("WS: error fatal session=%s", session_id)
    finally:
        log.info("WS: cerrando sesión session=%s", session_id)
        stt.cancel()
        state_manager.get(session_id).clear()

if __name__ == "__main__":
    import uvicorn
    # Para producción, es mejor usar Gunicorn o Uvicorn con workers desde la línea de comandos
    uvicorn.run("src.voice.server:app", host="0.0.0.0", port=7861, workers=1)
