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
import logging
import os
import time
from openai import AzureOpenAI

log = logging.getLogger("src.agent.agent")

from src.agent.state import manager as state_manager
from src.agent.prompt import generate_prompt
from src.agent import turn
from src.query import tools, render, resolve

MAX_TOOL_CALLS = 5  # regla 5: por voz no se pueden escuchar más

# Selector de desarrollador (QUERY_MODE): "tools" usa las 4 tools tipadas y el
# código construye el SQL (regla 2); "gpt" expone solo consulta_sql para que el
# LLM redacte el SELECT de todo (rompe la regla 2 a propósito, solo para
# comparar respuestas). Default "tools": no cambia el comportamiento.
_TOOLS_TIPADAS = ("aggregate", "count", "lookup", "list_values")


def query_mode() -> str:
    from dotenv import load_dotenv
    load_dotenv()
    return (os.environ.get("QUERY_MODE") or "tools").strip().lower()


def tools_para_modo(mode: str) -> list[dict]:
    if mode == "gpt":
        return [t for t in TOOLS_DEF if t["function"]["name"] == "consulta_sql"]
    return [t for t in TOOLS_DEF if t["function"]["name"] in _TOOLS_TIPADAS]

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
                    "sentimiento": {"type": "string", "enum": ["neutro", "urgente", "frustrado", "positivo"], "description": "Tono de voz / sentimiento del usuario."},
                    "necesita_interpretacion": {"type": "boolean", "description": "true si, además del dato, el usuario pidió su significado o interpretación."}
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
                    "sentimiento": {"type": "string", "enum": ["neutro", "urgente", "frustrado", "positivo"], "description": "Tono de voz / sentimiento del usuario."},
                    "necesita_interpretacion": {"type": "boolean", "description": "true si, además del dato, el usuario pidió su significado o interpretación."}
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
                    "sentimiento": {"type": "string", "enum": ["neutro", "urgente", "frustrado", "positivo"], "description": "Tono de voz / sentimiento del usuario."},
                    "necesita_interpretacion": {"type": "boolean", "description": "true si, además del dato, el usuario pidió su significado o interpretación."}
                },
                "required": ["text_query"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "list_values",
            "description": "Listar los valores distintos de una columna, opcionalmente filtrados. Úsalo para '¿quiénes/qué X hay en Y?' (ej. gerentes, prestadores o sedes en un municipio).",
            "parameters": {
                "type": "object",
                "properties": {
                    "dimension": {"type": "string"},
                    "filters": {"type": "object"},
                    "sentimiento": {"type": "string", "enum": ["neutro", "urgente", "frustrado", "positivo"], "description": "Tono de voz / sentimiento del usuario."},
                    "necesita_interpretacion": {"type": "boolean", "description": "true si, además del dato, el usuario pidió su significado o interpretación."}
                },
                "required": ["dimension"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "consulta_sql",
            "description": "ÚLTIMO RECURSO: solo si NINGUNA de las otras tools puede responder. Genera un único SELECT de lectura sobre la tabla raw_records (columna 'data' jsonb). Accede campos con data->>'columna'; para texto usa ILIKE '%valor%' (los valores se guardan en MAYÚSCULAS). Nunca escribas (INSERT/UPDATE/...).",
            "parameters": {
                "type": "object",
                "properties": {
                    "sql": {"type": "string", "description": "Un SELECT sobre raw_records."},
                    "sentimiento": {"type": "string", "enum": ["neutro", "urgente", "frustrado", "positivo"]}
                },
                "required": ["sql"]
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
        azure_endpoint=os.environ["AZURE_OPENAI_ENDPOINT"],
        # Regla 12: sin esto el SDK espera 600 s y reintenta dos veces; un
        # Azure lento congelaría la llamada entera en vez de dar la frase fija.
        timeout=5.0,
        max_retries=0,
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
    "list_values": ("dimension", "filters"),
    "consulta_sql": ("sql",),
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
    elif func_name == "consulta_sql":
        res = tools.run_sql(kwargs.get("sql", ""))
        texto = render.render_sql(res)
    else:
        texto = "No pude procesar la consulta."
    return render._pieza(texto)


def _apply_state(state, func_name: str, args: dict, resolved_filters: dict):
    measure = args.get("measure")
    group_by = args.get("group_by") or []
    state.update(tool_name=func_name, filters=resolved_filters,
                 measure=measure, active_dimension=group_by[0] if group_by else None)
    state.last_tool_args = dict(args)


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
    state.last_response = final
    state.last_sentimiento = sentimiento
    state.add_message("user", user_text)
    state.add_message("assistant", final)
    return final


def process_turn(session_id: str, user_text: str) -> str:
    from dotenv import load_dotenv
    load_dotenv()

    client = _get_client()
    state = state_manager.get(session_id)
    schema = tools.load_schema()
    mode = query_mode()

    # Fase 6: Interceptar turnos de corrección
    intercept = turn.interceptar(user_text, state)
    if intercept:
        user_text = intercept

    # Desambiguación pendiente: se intenta cerrar sin reinterpretar la frase.
    if state.pending_disambiguation:
        settled = _complete_pending(state, user_text, schema)
        if settled is not None:
            return settled

    state.tick()

    system_prompt = generate_prompt(mode=mode)

    context_msgs = [{"role": "system", "content": system_prompt}]
    slots_summary = state.resumen_slots()
    if slots_summary != "No hay contexto previo.":
        context_msgs.append({"role": "system",
                             "content": f"Contexto de slots activos: {slots_summary}"})

    for msg in state.history:
        context_msgs.append({"role": msg["role"], "content": msg["content"]})

    context_msgs.append({"role": "user", "content": user_text})

    deployment = os.environ["AZURE_OPENAI_DEPLOYMENT"]

    t_llm = time.perf_counter()
    try:
        response = client.chat.completions.create(
            model=deployment,
            messages=context_msgs,
            tools=tools_para_modo(mode),
            tool_choice="auto"
        )
    except Exception as e:
        log.error("Azure OpenAI ERROR: %s", e)
        return "Hubo un error de conexión con el motor de inteligencia artificial."
    log.info("LLM intención+tool-calling: %d ms", round((time.perf_counter() - t_llm) * 1000))

    msg = response.choices[0].message
    if not msg.tool_calls:
        # El modelo redactó algo directo (saludo, fuera de dominio, seguimiento).
        final = msg.content or "No sé cómo procesar esa petición."
        state.last_response = final
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
        args.pop("necesita_interpretacion", None)  # ya no se usa: el redactor corre siempre

        log.info("tool: %s args=%s", func_name,
                 json.dumps(args, ensure_ascii=False)[:300])

        try:
            respuestas.append(_execute_tool(func_name, args, schema))
            _apply_state(state, func_name, args, resolved_filters)
        except Exception as e:
            log.exception("tool: error ejecutando %s", func_name)
            return f"Hubo un problema ejecutando la consulta: {e}"

    datos = render.componer(respuestas)

    # Segunda pasada SIEMPRE: el LLM redacta la respuesta final hablada a
    # partir de los datos deterministas que devolvieron las tools. Las cifras
    # y nombres salen EXCLUSIVAMENTE de `datos` (regla 1), nunca del modelo.
    t_red = time.perf_counter()
    final_ans = _redactar(client, deployment, user_text, datos)
    log.info("LLM redactor: %d ms -> %r",
             round((time.perf_counter() - t_red) * 1000), final_ans[:200])

    final_ans = render.aplicar_tono(final_ans, sentimiento)
    state.last_response = final_ans
    state.last_sentimiento = sentimiento
    state.add_message("user", user_text)
    state.add_message("assistant", final_ans)
    return final_ans


_REDACTOR_SYS = (
    "Eres una asistente de voz formal y empática que responde por teléfono "
    "sobre capacidad instalada de IPS en Colombia. Recibes la PREGUNTA del "
    "usuario y los DATOS que el sistema ya consultó en la base. Redacta UNA "
    "respuesta hablada, natural y clara, que de verdad conteste la pregunta.\n"
    "Reglas estrictas:\n"
    "- Usa EXCLUSIVAMENTE los números, nombres, lugares y campos que aparecen "
    "en DATOS. Nunca inventes ni calcules cifras nuevas.\n"
    "- Si DATOS dice que no hay resultado o es vacío, dilo con naturalidad; no "
    "te inventes un dato.\n"
    "- Enuncia los filtros/lugar a los que corresponde la cifra, para que no "
    "quede un número huérfano (ej. 'en el Chocó', 'camas').\n"
    "- Breve: 1 o 2 frases, apto para escuchar por teléfono. Sin listas largas "
    "ni markdown. Español colombiano neutro.\n"
    "- Responde SIEMPRE algo; nunca devuelvas vacío."
)


def _redactar(client, deployment: str, user_text: str, datos: str) -> str:
    """Segundo salto al LLM: redacta la respuesta final hablada a partir de los
    datos deterministas de las tools. Cifras y nombres solo salen de `datos`
    (regla 1). Si falla, cae al texto determinista (regla 7: nunca silencio)."""
    try:
        resp = client.chat.completions.create(
            model=deployment,
            messages=[
                {"role": "system", "content": _REDACTOR_SYS},
                {"role": "user", "content": f"PREGUNTA: {user_text}\nDATOS: {datos}"},
            ],
        )
        final = (resp.choices[0].message.content or "").strip()
    except Exception:
        return datos
    return final or datos
