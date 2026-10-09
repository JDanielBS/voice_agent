"""Lógica central del agente (Fase 4).

Integra state, LLM (con tool-calling), resolución de entidades y ejecución.

Un solo salto al LLM por turno (ARQUITECTURA.md §5.1). El modelo puede emitir
varias llamadas a tools en ese único salto (p. ej. "camas, consultorios y
ambulancias por separado"); todas se ejecutan y se enuncian. La redacción es
determinista y autodescriptiva (medida + filtros), para que nunca quede un
número huérfano.
"""
from __future__ import annotations

import json
import os
from openai import AzureOpenAI

from src.agent.state import manager as state_manager
from src.agent.prompt import generate_prompt
from src.query import tools, render, resolve

MAX_TOOL_CALLS = 5  # regla 5: por voz no se pueden escuchar más

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


_PISTAS_DIMENSION = {
    "municipio": ("municipio", "ciudad", "pueblo", "localidad"),
    "departamento": ("departamento", "distrito", "region", "región", "gobernacion",
                     "gobernación", "estado"),
}


def _dims_ambiguas_sin_pista(value: str, user_text: str, vocab: dict) -> list[str] | None:
    """Si `value` existe en varias dimensiones y el usuario no dijo cuál, devuélvelas.

    El LLM elige una dimensión, pero "Cali" o "Nariño" existen como municipio y
    como departamento. Si la frase no contiene una pista ("en el departamento de
    Cali"), un número correcto sobre la entidad equivocada sería peor: se pregunta.
    """
    norm = resolve.normalize(value)
    dims = vocab.get("ambiguous", {}).get(norm)
    if not dims or len(dims) < 2:
        return None
    texto = resolve.normalize(user_text or "")
    # Un filtro heredado (el LLM lo repite desde los slots) ya se decidió antes:
    # no se vuelve a preguntar si el valor no se menciona en este turno.
    if norm not in texto:
        return None
    if any(any(p in texto for p in _PISTAS_DIMENSION.get(d, (d,))) for d in dims):
        return None
    return dims


def _resolve_filters(filters: dict, user_text: str = "") -> tuple[dict, dict | None]:
    """Aplica resolve a los filtros.

    Devuelve (filtros_resueltos, error). `error` es None o un dict con
    {"msg", "kind", "value", "dimension", "candidates"} para pedir aclaración.
    """
    resolved: dict = {}
    vocab = resolve.load_vocab()
    for k, v in filters.items():
        if not isinstance(v, str):
            resolved[k] = v
            continue
        dims = _dims_ambiguas_sin_pista(v, user_text, vocab)
        if dims:
            return resolved, {
                "kind": "ambiguo", "value": v, "dimension": None,
                "candidates": dims,
            }
        res = resolve.resolve(v, dimension=k)
        if res["status"] == "ok":
            resolved[k] = res["canonical"]
            continue
        motivo = res.get("motivo")
        if motivo == "ambiguo":
            return resolved, {
                "kind": "ambiguo", "value": v, "dimension": None,
                "candidates": res.get("dimensiones", []),
            }
        if motivo == "empate":
            return resolved, {
                "kind": "empate", "value": v, "dimension": k,
                "candidates": res.get("candidatos", []),
            }
        return resolved, {
            "kind": "sin_coincidencia", "value": v, "dimension": k,
            "candidates": res.get("candidatos", []),
        }
    return resolved, None


def _error_msg(err: dict) -> str:
    if err["kind"] == "sin_coincidencia":
        return f"No encontré el término '{err['value']}' en mi base de datos."
    return render.render_disambiguation(err["value"], err["candidates"], err["dimension"])


# Argumentos que cada tool acepta. El modelo a veces añade `filters: {}` a
# list_values o `sentimiento` a cualquier tool; se descartan antes de invocar.
_TOOL_KEYS = {
    "aggregate": ("measure", "group_by", "filters", "top_n"),
    "count": ("filters",),
    "lookup": ("text_query", "filters", "limit"),
    "list_values": ("dimension",),
}


def _execute_tool(func_name: str, args: dict, schema: dict) -> dict:
    """Ejecuta la tool y devuelve la "pieza" redactada (determinista).

    Cada pieza trae su texto completo y, cuando la consulta tiene contexto
    compartido (lugar), lo que hace falta para que `render.componer` deduplique
    y conecte las piezas de un mismo turno.
    """
    kwargs = {k: args[k] for k in _TOOL_KEYS.get(func_name, ()) if k in args}
    filters = kwargs.get("filters") or {}
    if func_name == "aggregate":
        res = tools.aggregate(**kwargs)
        return render._agregado_pieza(res, kwargs.get("measure", ""),
                                      kwargs.get("group_by") or [], filters)
    if func_name == "count":
        res = tools.count(**kwargs)
        return render._contar_pieza(res, filters)
    if func_name == "lookup":
        res = tools.lookup(**kwargs)
        texto = render.render_lookup(res, tools.search_columns(schema), filters,
                                     kwargs.get("text_query", ""))
    elif func_name == "list_values":
        res = tools.list_values(**kwargs)
        texto = render.render_list_values(res, kwargs.get("dimension"))
    else:
        texto = "No pude procesar la consulta."
    return render._pieza(texto)


def _apply_state(state, func_name: str, args: dict, resolved_filters: dict):
    measure = args.get("measure")
    group_by = args.get("group_by") or []
    state.update(tool_name=func_name, filters=resolved_filters,
                 measure=measure, active_dimension=group_by[0] if group_by else None)


def _complete_pending(state, user_text: str, schema: dict) -> str | None:
    """Intenta cerrar la desambiguación pendiente con la respuesta del usuario.

    Devuelve la respuesta si logró completar el slot; None si no reconoció la
    respuesta (entonces se descarta y se reinterpreta el turno desde cero).
    """
    pend = state.pending_disambiguation
    if not pend:
        return None
    texto = (user_text or "").lower()
    elegido = None

    if pend["kind"] == "ambiguo":
        sinonimos = {
            "municipio": ("municipio", "ciudad", "pueblo"),
            "departamento": ("departamento", "departamento", "distrito", "region", "región"),
        }
        for cand in pend["candidates"]:
            palabras = sinonimos.get(cand, (cand,))
            if any(p in texto for p in palabras):
                elegido = cand
                break
    else:
        for cand in pend["candidates"]:
            if resolve.normalize(str(cand)) in resolve.normalize(texto):
                elegido = cand
                break

    if elegido is None:
        state.clear_disambiguation()
        return None

    args = dict(pend["args"])
    filt = dict(args.get("filters") or {})
    if pend["kind"] == "ambiguo":
        res = resolve.resolve(pend["value"], dimension=elegido)
        if res["status"] != "ok":
            state.clear_disambiguation()
            return None
        filt.pop(pend.get("base_dimension"), None)
        filt[elegido] = res["canonical"]
    else:
        filt[pend["dimension"]] = elegido

    args["filters"] = filt
    state.clear_disambiguation()
    ans = _execute_tool(pend["tool"], args, schema)["texto"]
    _apply_state(state, pend["tool"], args, filt)
    sentimiento = pend.get("sentimiento", "neutro")
    final = render.aplicar_tono(ans, sentimiento)
    state.add_message("user", user_text)
    state.add_message("assistant", final)
    return final


def process_turn(session_id: str, user_text: str) -> str:
    from dotenv import load_dotenv
    load_dotenv()

    client = _get_client()
    state = state_manager.get(session_id)
    schema = tools.load_schema()

    # Desambiguación pendiente: se intenta cerrar sin reinterpretar la frase.
    if state.pending_disambiguation:
        settled = _complete_pending(state, user_text, schema)
        if settled is not None:
            return settled

    state.tick()

    system_prompt = generate_prompt()

    context_msgs = [{"role": "system", "content": system_prompt}]
    slots_summary = state.resumen_slots()
    if slots_summary != "No hay contexto previo.":
        context_msgs.append({"role": "system",
                             "content": f"Contexto de slots activos: {slots_summary}"})

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
        import sys
        print(f"Azure OpenAI ERROR: {e}", file=sys.stderr)
        return "Hubo un error de conexión con el motor de inteligencia artificial."

    msg = response.choices[0].message
    if not msg.tool_calls:
        # El modelo redactó algo directo (saludo, fuera de dominio, seguimiento).
        final = msg.content or "No sé cómo procesar esa petición."
        state.add_message("user", user_text)
        state.add_message("assistant", final)
        return final

    respuestas: list[str] = []
    sentimiento = "neutro"

    for tc in msg.tool_calls[:MAX_TOOL_CALLS]:
        func_name = tc.function.name
        try:
            args = json.loads(tc.function.arguments)
        except json.JSONDecodeError:
            return "Hubo un problema procesando los argumentos de la consulta."

        raw_filters = args.get("filters", {})
        resolved_filters, err = _resolve_filters(raw_filters, user_text)
        if err:
            err["tool"] = func_name
            err["args"] = args
            err["base_dimension"] = next(iter(raw_filters), None)
            err["sentimiento"] = args.get("sentimiento", "neutro")
            if err["kind"] != "sin_coincidencia":
                state.set_disambiguation(user_text, err["candidates"], err)
            return _error_msg(err)

        args["filters"] = resolved_filters
        sentimiento = args.pop("sentimiento", sentimiento) or sentimiento

        try:
            respuestas.append(_execute_tool(func_name, args, schema))
            _apply_state(state, func_name, args, resolved_filters)
        except Exception as e:
            return f"Hubo un problema ejecutando la consulta: {e}"

    final_ans = render.componer(respuestas)
    final_ans = render.aplicar_tono(final_ans, sentimiento)
    state.add_message("user", user_text)
    state.add_message("assistant", final_ans)
    return final_ans
