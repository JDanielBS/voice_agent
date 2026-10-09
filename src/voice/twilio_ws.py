"""Servidor WebSocket para Twilio Media Streams.

Gestiona:
1. Endpoint /twiml que devuelve <Stream>.
2. Endpoint /ws que recibe/envía audio mulaw.
3. Decodificación mulaw -> pcm para VAD y STT.
4. Conecta VAD -> STT -> process_turn -> TTS -> Twilio.
"""
from __future__ import annotations

import asyncio
import base64
import json

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import Response

from src.agent.agent import process_turn
from src.voice.mulaw import mulaw_to_pcm
from src.voice.stt import StreamingSTT
from src.voice.tts import synthesize_stream
from src.voice.vad import VAD

app = FastAPI()


@app.post("/twiml")
async def twiml_endpoint(request: Request):
    """Genera el TwiML para conectar la llamada al WebSocket."""
    host = request.headers.get("host", "localhost")
    scheme = "wss" if "ngrok" in host or request.url.scheme == "https" else "ws"
    ws_url = f"{scheme}://{host}/ws"
    
    # Twilio se conecta a nosotros
    response = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Say language="es-CO">Conectando al asistente de capacidad instalada.</Say>
    <Connect>
        <Stream url="{ws_url}" />
    </Connect>
</Response>"""
    return Response(content=response, media_type="application/xml")


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    
    stream_sid = None
    call_sid = "default_session"
    
    vad = VAD()
    stt = StreamingSTT()
    stt.start()
    
    tts_task = None
    
    async def play_tts(text: str):
        try:
            generator = synthesize_stream(text)
            
            while True:
                # pull_stream.read() es bloqueante, lo mandamos a un thread
                try:
                    chunk = await asyncio.to_thread(next, generator)
                except StopIteration:
                    break
                    
                if chunk:
                    encoded = base64.b64encode(chunk).decode("ascii")
                    msg = {
                        "event": "media",
                        "streamSid": stream_sid,
                        "media": {"payload": encoded}
                    }
                    await websocket.send_json(msg)
                    
            # Enviar mark para indicar fin de habla
            await websocket.send_json({
                "event": "mark",
                "streamSid": stream_sid,
                "mark": {"name": "tts_finished"}
            })
                    
        except asyncio.CancelledError:
            print("[WS] TTS interrumpido por barge-in")
        except Exception as e:
            print(f"[WS] Error en TTS: {e}")

    try:
        while True:
            message = await websocket.receive_text()
            data = json.loads(message)
            
            event = data.get("event")
            if event == "start":
                stream_sid = data["start"]["streamSid"]
                call_sid = data["start"]["callSid"]
                print(f"[WS] Conectado Stream: {stream_sid}, Call: {call_sid}")
                
            elif event == "media":
                payload = data["media"]["payload"]
                mulaw_bytes = base64.b64decode(payload)
                pcm_bytes = mulaw_to_pcm(mulaw_bytes)
                
                vad_event = vad.process(pcm_bytes)
                
                if vad_event == "speech_start":
                    if tts_task and not tts_task.done():
                        tts_task.cancel()
                        await websocket.send_json({"event": "clear", "streamSid": stream_sid})
                        print("[WS] Barge-in detectado. Vaciando audio saliente.")
                        
                # Siempre alimentar al STT
                stt.push_chunk(pcm_bytes)
                
                if vad_event == "speech_end":
                    print("[WS] Fin de habla detectado. Resolviendo STT...")
                    # finish_and_get_text es bloqueante (espera respuesta de Azure)
                    text = await asyncio.to_thread(stt.finish_and_get_text)
                    print(f"[WS] Usuario dijo: '{text}'")
                    
                    if text.strip():
                        # process_turn es bloqueante (espera OpenAI)
                        reply = await asyncio.to_thread(process_turn, call_sid, text)
                        print(f"[WS] Agente responde: {reply}")
                        
                        tts_task = asyncio.create_task(play_tts(reply))
                    
                    # Preparar nuevo STT para lo que sigue
                    stt = StreamingSTT()
                    stt.start()
                    vad.reset()
                    
            elif event == "stop":
                print(f"[WS] Llamada {call_sid} finalizada.")
                break
                
    except WebSocketDisconnect:
        print("[WS] WebSocket desconectado")
    finally:
        if tts_task and not tts_task.done():
            tts_task.cancel()
        stt.cancel()

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("src.voice.twilio_ws:app", host="0.0.0.0", port=7860, reload=True)

