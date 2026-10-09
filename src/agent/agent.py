"""Lógica central del agente (Fase 4).

Integra state, LLM (con tool-calling), resolución de entidades y ejecución.
"""
from __future__ import annotations

import json
import os
from openai import AzureOpenAI

from src.agent.state import manager as state_manager
from src.agent.prompt import generate_prompt
from src.query import tools, render, resolve

TOOLS_DEF = [
    {
        "type": "function",
        "function": {
            "name": "aggregate",
            "description": "Obtener sumas agrupadas por dimensiones o con filtros.",
            "parameters": {
                "type": "object",
                "properties": {
                    "measure": {"type": "string"},
                    "group_by": {"type": "array", "items": {"type": "string"}},
                    "filters": {"type": "object"},
                    "top_n": {"type": "integer"},
                    "sentimiento": {"type": "string", "enum": ["neutro", "urgente", "frustrado", "positivo"], "description": "Tono de voz / sentimiento del usuario."}
                },
                "required": ["measure"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "count",
            "description": "Contar cantidad de registros/sedes.",
            "parameters": {
                "type": "object",
                "properties": {
                    "filters": {"type": "object"},
                    "sentimiento": {"type": "string", "enum": ["neutro", "urgente", "frustrado", "positivo"], "description": "Tono de voz / sentimiento del usuario."}
                }
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "lookup",
            "description": "Buscar entidades específicas por nombre libre.",
            "parameters": {
                "type": "object",
                "properties": {
                    "text_query": {"type": "string"},
                    "filters": {"type": "object"},
                    "limit": {"type": "integer"},
                    "sentimiento": {"type": "string", "enum": ["neutro", "urgente", "frustrado", "positivo"], "description": "Tono de voz / sentimiento del usuario."}
                },
                "required": ["text_query"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "list_values",
            "description": "Listar los valores disponibles para una dimensión.",
            "parameters": {
                "type": "object",
                "properties": {
                    "dimension": {"type": "string"},
                    "sentimiento": {"type": "string", "enum": ["neutro", "urgente", "frustrado", "positivo"], "description": "Tono de voz / sentimiento del usuario."}
                },
                "required": ["dimension"]
            }
        }
    }
]


def _get_client():
    from dotenv import load_dotenv
    load_dotenv()
    return AzureOpenAI(
        api_key=os.environ["AZURE_OPENAI_API_KEY"],
        api_version=os.environ["AZURE_OPENAI_API_VERSION"],
        azure_endpoint=os.environ["AZURE_OPENAI_ENDPOINT"]
    )


def _resolve_filters(filters: dict) -> tuple[dict, str | None]:
    """Aplica resolve a los filtros y retorna (filtros_resueltos, msg_error)."""
    resolved = {}
    for k, v in filters.items():
        if isinstance(v, str):
            res = resolve.resolve(v, dimension=k)
            if res["status"] == "ok":
                resolved[k] = res["canonical"]
            else:
                motivo = res.get("motivo")
                if motivo == "ambiguo":
                    dims = res.get("dimensiones", [])
                    return {}, f"La palabra '{v}' es ambigua. ¿Te refieres a {dims[0]} o {dims[1]}?"
                elif motivo == "empate":
                    cands = res.get("candidatos", [])
                    return {}, f"Tengo un conflicto con '{v}'. ¿Te refieres a {cands[0]} o {cands[1]}?"
                else:
                    return {}, f"No encontré el término '{v}' en mi base de datos."
        else:
            resolved[k] = v
    return resolved, None


def process_turn(session_id: str, user_text: str) -> str:
    from dotenv import load_dotenv
    load_dotenv()
    
    client = _get_client()
    state = state_manager.get(session_id)
    
    # Manejo de desambiguación pendiente
    if state.pending_disambiguation:
        cand = state.pending_disambiguation.get("candidates", [])
        for c in cand:
            if c.lower() in user_text.lower():
                state.clear_disambiguation()
                return f"Entendido, usaremos {c}. ¿Me repites tu consulta completa?"
        
        # Si no resolvió, limpiamos y procedemos normal asumiendo que cambió de tema
        state.clear_disambiguation()
        
    state.tick()

    system_prompt = generate_prompt()
    
    context_msgs = [{"role": "system", "content": system_prompt}]
    # Inyectar resumen de slots activos
    slots_summary = state.resumen_slots()
    if slots_summary != "No hay contexto previo.":
        context_msgs.append({"role": "system", "content": f"Contexto de slots activos: {slots_summary}"})
        
    # Inyectar ventana deslizante
    for msg in state.history:
        context_msgs.append({"role": msg["role"], "content": msg["content"]})
        
    context_msgs.append({"role": "user", "content": user_text})
    
    deployment = os.environ["AZURE_OPENAI_DEPLOYMENT"]
    
    try:
        response = client.chat.completions.create(
            model=deployment,
            messages=context_msgs,
            tools=TOOLS_DEF,
            tool_choice="auto"
        )
    except Exception as e:
        return "Hubo un error de conexión con el motor de inteligencia artificial."
        
    msg = response.choices[0].message
    if not msg.tool_calls:
        # El modelo redactó algo directo
        return msg.content or "No sé cómo procesar esa petición."
        
    tc = msg.tool_calls[0]
    func_name = tc.function.name
    try:
        args = json.loads(tc.function.arguments)
    except json.JSONDecodeError:
        return "Hubo un problema procesando los argumentos de la consulta."
        
    # Resolver filtros
    raw_filters = args.get("filters", {})
    resolved_filters, err_msg = _resolve_filters(raw_filters)
    if err_msg:
        # Guardamos estado de desambiguación en caso de que sea empate (simplificado)
        if "conflicto" in err_msg or "ambigua" in err_msg:
            # Fake candidates para el ejemplo
            state.set_disambiguation(user_text, ["opción 1", "opción 2"]) 
        return err_msg
        
    # Guardamos en estado
    state.update(tool_name=func_name, filters=resolved_filters)
    args["filters"] = resolved_filters
    
    # Extraer sentimiento si el LLM lo mandó
    sentimiento = args.pop("sentimiento", "neutro")
    
    try:
        if func_name == "aggregate":
            res = tools.aggregate(**args)
            ans = render.render_aggregate(res, args.get("measure", ""), args.get("group_by", []))
        elif func_name == "count":
            res = tools.count(**args)
            ans = render.render_count(res)
        elif func_name == "lookup":
            res = tools.lookup(**args)
            ans = render.render_lookup(res)
        elif func_name == "list_values":
            res = tools.list_values(**args)
            ans = render.render_list_values(res)
        else:
            ans = "No pude procesar la consulta."
            
        final_ans = render.aplicar_tono(ans, sentimiento)
        state.add_message("user", user_text)
        state.add_message("assistant", final_ans)
        return final_ans
        
    except Exception as e:
        return f"Hubo un problema ejecutando la consulta: {e}"
