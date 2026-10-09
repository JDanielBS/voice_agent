"""Post-proceso para oído (render).

Transforma los resultados crudos de las consultas en un texto fluido que el
STT puede leer, o que sirve como base para que el LLM lo redacte en casos atípicos.
Respuestas de una o dos frases. El dato primero, el contexto después.
Nunca listas largas; máximo 3 elementos enunciados.

Regla de conversación (ARQUITECTURA.md §5.4 y §10.6): toda respuesta enuncia
**qué** se midió y **con qué filtros**, para que un seguimiento del usuario
("¿y en Chocó?", "¿de qué?") tenga un antecedente claro y no un número huérfano.

Redacción humana sin depender del LLM (regla 3: un solo salto): las frases se
construyen con vocabulario de presentación (nunca nombres crudos de columna),
conectores naturales, concordancia de género del número con el sustantivo y
algo de variedad determinista. Todo lo de este módulo es presentación: no toca
SQL ni el acceso a datos, y funciona con cualquier dataset.
"""
from __future__ import annotations

import difflib
import re
import unicodedata

# ── Números a palabras (con concordancia de género) ─────────────────────────

UNIDADES = ["", "un", "dos", "tres", "cuatro", "cinco", "seis", "siete", "ocho", "nueve"]
UNIDADES_F = ["", "una", "dos", "tres", "cuatro", "cinco", "seis", "siete", "ocho", "nueve"]
DECENAS = ["", "diez", "veinte", "treinta", "cuarenta", "cincuenta", "sesenta", "setenta", "ochenta", "noventa"]
ESPECIALES = {11: "once", 12: "doce", 13: "trece", 14: "catorce", 15: "quince",
              16: "dieciséis", 17: "diecisiete", 18: "dieciocho", 19: "diecinueve",
              21: "veintiún", 22: "veintidós", 23: "veintitrés", 24: "veinticuatro",
              25: "veinticinco", 26: "veintiséis", 27: "veintisiete", 28: "veintiocho", 29: "veintinueve"}
ESPECIALES_F = {**ESPECIALES, 21: "veintiuna"}
CENTENAS = ["", "ciento", "doscientos", "trescientos", "cuatrocientos", "quinientos",
            "seiscientos", "setecientos", "ochocientos", "novecientos"]
CENTENAS_F = ["", "ciento", "doscientas", "trescientas", "cuatrocientas", "quinientas",
              "seiscientas", "setecientas", "ochocientas", "novecientas"]

# Prefijos que Socrata deja al mutilar nombres con tilde (c_digo, n_mero).
_MANGLE_PREFIXES = ("num_", "nom_", "nombre_", "n_mero_", "c_digo_", "codigo_")

# Vocabulario de presentación: cómo suena cada token del nombre de columna.
_TILDES = {
    "atencion": "atención", "descripcion": "descripción", "direccion": "dirección",
    "region": "región", "juridica": "jurídica", "publico": "público",
    "publica": "pública", "codigo": "código", "telefono": "teléfono",
    "numero": "número", "digito": "dígito",
}

# Frases naturales para etiquetas conocidas (clave: nombre ya unido, sin tildes).
_FRASES = {
    "cantidad capacidad instalada": "capacidad instalada",
    "grupo capacidad": "tipo de capacidad",
    "tipo capacidad": "tipo de capacidad",
    "descripcion capacidad": "descripción de capacidad",
    "nivel atencion": "nivel de atención",
    "digito verificion": "dígito de verificación",
    "digito verificacion": "dígito de verificación",
}

_FEMENINO_EXC = {"mano", "foto", "moto", "radio"}
_MASCULINO_EXC = {"dia", "mapa", "planeta", "problema", "tema", "sistema"}


def _verbalizar_menor_1000(n: int, genero: str = "m") -> str:
    fem = genero == "f"
    if n == 100:
        return "cien"
    if n == 0:
        return ""
    c, r = n // 100, n % 100
    res = (CENTENAS_F if fem else CENTENAS)[c]
    if r > 0:
        if r in ESPECIALES:
            res += " " + (ESPECIALES_F if fem else ESPECIALES)[r]
        elif r < 10:
            res += " " + (UNIDADES_F if fem else UNIDADES)[r]
        else:
            d, u = r // 10, r % 10
            if u == 0:
                res += " " + DECENAS[d]
            else:
                res += " " + DECENAS[d] + " y " + (UNIDADES_F if fem else UNIDADES)[u]
    return res.strip()


def verbalizar(n: int | float, genero: str = "m", aprox: bool = True) -> str:
    """Verbaliza un número redondeando a la centena/decena si es grande.

    `genero` ("m"/"f") hace concordar el número con el sustantivo que le sigue:
    "unas seiscientas camas" vs "unos seiscientos consultorios".
    `aprox=False` suprime el "unos/unas" (para frases tipo "alrededor de X").
    """
    n = int(n)
    if n == 0:
        return "cero"

    prefijo = ""
    if n > 1000:
        r = round(n, -2)
        if r != n and aprox:
            prefijo = "unas " if genero == "f" else "unos "
        n = r
    elif n > 100:
        r = round(n, -1)
        if r != n and aprox:
            prefijo = "unas " if genero == "f" else "unos "
        n = r

    if n < 1000:
        return f"{prefijo}{_verbalizar_menor_1000(n, genero)}".strip()

    miles, resto = n // 1000, n % 1000
    res = "mil" if miles == 1 else _verbalizar_menor_1000(miles, genero) + " mil"
    if resto > 0:
        res += " " + _verbalizar_menor_1000(resto, genero)
    return f"{prefijo}{res}".strip()


# ── Presentación de columnas y valores ──────────────────────────────────────

def _norm(s) -> str:
    """Minúsculas sin tildes ni puntuación, para comparar texto libre."""
    s = unicodedata.normalize("NFKD", str(s or "")).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9 ]", " ", s.lower()).strip()


def _capitalizar(s: str) -> str:
    return s[:1].upper() + s[1:] if s else s


def _capitalizar_lugar(s: str) -> str:
    """'leticia'/'LETICIA' -> 'Leticia'; respeta inicialismos con mayúsculas ('D.C')."""
    out = []
    for w in str(s).split():
        if w.isupper() and len(w) > 1:
            # "QUIBDÓ" -> "Quibdó"; "D.C" se conserva por su punto.
            out.append(w if any(not ch.isalpha() for ch in w) else w.capitalize())
        else:
            out.append(_capitalizar(w))
    return " ".join(out)


def etiqueta(col: str) -> str:
    """Nombre natural de una columna, listo para el oído.

    Heurística de presentación, no de acceso a datos: parte del string del
    schema y aplica el vocabulario de arriba. Si la columna es desconocida,
    cae al nombre derivado tal cual, así cambiar de dataset no rompe nada.
    """
    name = col or ""
    for p in _MANGLE_PREFIXES:
        if name.startswith(p):
            name = name[len(p):]
            break
    base = name.replace("_", " ").strip().lower()
    if base in _FRASES:
        return _FRASES[base]
    return " ".join(_TILDES.get(w, w) for w in base.split())


# Alias de compatibilidad con el nombre anterior.
humanizar_columna = etiqueta


def _genero(noun: str) -> str:
    """Género gramatical del sustantivo (para concordar el número)."""
    w = _norm(noun).split()[0] if noun else ""
    if w in _FEMENINO_EXC:
        return "f"
    if w in _MASCULINO_EXC:
        return "m"
    # Se prueba el singular: "camas"->"cama", "consultorios"->"consultorio".
    if w.endswith("es") and len(w) > 3:
        w = w[:-2]
    elif w.endswith("s") and len(w) > 2:
        w = w[:-1]
    if w.endswith(("a", "cion", "sion", "dad", "tad", "tud", "umbre")):
        return "f"
    return "m"


# Semántica de presentación por tokens: qué conector y qué tratamiento recibe.
_TOK_LUGAR = ("municipio", "departamento", "ciudad", "localidad", "region",
              "vereda", "corregimiento", "distrito", "pais")
_TOK_CATEGORIA = ("naturaleza", "grupo", "tipo", "clase", "categoria",
                  "descripcion", "nivel")
_TOK_SUSTANTIVO = ("grupo", "tipo", "clase", "categoria")


def _tipo_columna(col: str) -> str | None:
    tokens = set(_norm(col).split())
    if tokens & set(_TOK_LUGAR):
        return "lugar"
    if tokens & set(_TOK_CATEGORIA):
        return "categoria"
    return None


def _es_sustantivo_col(col: str) -> bool:
    """¿El valor de esta columna es un sustantivo contable (camas, sillas)?"""
    return bool(set(_norm(col).split()) & set(_TOK_SUSTANTIVO))


def _valor_hablado(v, col: str | None = None) -> str:
    s = str(v)
    t = _tipo_columna(col or "")
    if t == "lugar":
        return _capitalizar_lugar(s)
    # Valores de categoría (CAMAS, Pública, Privada): en voz, minúscula.
    if t == "categoria":
        return s.lower()
    if s.isupper() and len(s) > 1:
        return s.lower()
    return s


# ── Frases de filtros ───────────────────────────────────────────────────────

def _clasificar(filters: dict | None):
    lugares, categorias, otros = [], [], []
    for k, v in (filters or {}).items():
        val = _valor_hablado(v, k)
        t = _tipo_columna(k)
        if t == "lugar":
            lugares.append(val)
        elif t == "categoria":
            categorias.append((etiqueta(k), val, k))
        else:
            otros.append((etiqueta(k), val, k))
    return lugares, categorias, otros


def _unir(items: list[str]) -> str:
    if not items:
        return ""
    if len(items) == 1:
        return items[0]
    return ", ".join(items[:-1]) + " y " + items[-1]


def _frase_lugar(lugares: list[str], capitalizar: bool = False) -> str:
    if not lugares:
        return ""
    txt = _unir(lugares)
    return f"En {txt}" if capitalizar else f"en {txt}"


def _frase_categorias(categorias: list) -> str:
    partes = []
    for et, val, _k in categorias:
        if et.startswith("tipo de"):
            partes.append(f"de tipo {val}")
        else:
            partes.append(f"de {et} {val}")
    return (" " + _unir(partes)) if partes else ""


def _frase_otros(otros: list) -> str:
    partes = [f"con {et} {val}" for et, val, _k in otros]
    return (" " + _unir(partes)) if partes else ""


def _sustantivo(categorias: list, measure: str) -> str:
    """El sustantivo que acompaña la cifra: el valor de la categoría si existe."""
    for _et, val, k in categorias:
        if _es_sustantivo_col(k):
            return val.lower()
    return etiqueta(measure) or "capacidad"


def _singular(noun: str) -> str:
    return noun[:-1] if noun.endswith("s") and len(noun) > 3 else noun


def _hay_countable(total: int, noun: str) -> str:
    if int(total) == 1:
        sg = _singular(noun)
        articulo = "una" if _genero(sg) == "f" else "un"
        return f"{articulo} {sg}"
    return f"{verbalizar(total, _genero(noun))} {noun}"


def aplicar_tono(texto: str, sentimiento: str) -> str:
    """Adapta el tono de la respuesta según el sentimiento detectado."""
    s = (sentimiento or "neutro").lower()
    if s == "urgente":
        return "Claro, te lo digo rápido: " + texto
    if s == "frustrado":
        return "Entiendo, disculpa. " + texto
    if s == "positivo":
        return "¡Con gusto! " + texto
    return texto


def _vacio(measure: str, filters: dict | None) -> str:
    lugares, categorias, otros = _clasificar(filters)
    noun = _sustantivo(categorias, measure)
    # Si la categoría aportó el sustantivo (camas), no se repite en el detalle.
    cat = [c for c in categorias if not _es_sustantivo_col(c[2])]
    loc = _frase_lugar(lugares)
    detalle = (f" {loc}" if loc else "") + _frase_categorias(cat) + _frase_otros(otros)
    return f"No encontré datos de {noun}{detalle}."


# ── Plantillas de respuesta ─────────────────────────────────────────────────

# Cada tool devuelve una "pieza" con su texto completo y, cuando es posible,
# la información para deduplicar el contexto entre piezas del mismo turno
# (varias llamadas del LLM, p. ej. "camas, consultorios y ambulancias por
# separado"). `componer()` usa ese contexto para no repetir el lugar.
_CONECTORES = ("Además, ", "También ", "Por otra parte, ")


def _pieza(texto: str, breve: str = "", lugar: str = "", loc_es: str = "",
           hay: str | None = None) -> dict:
    return {
        "texto": texto,
        "texto_breve": breve or texto,
        "lugar": lugar,
        "lugar_es": loc_es,
        "hay": hay,
    }


def _agregado_pieza(result: dict, measure: str, group_by: list[str],
                    filters: dict | None = None) -> dict:
    total_grupos = result.get("total_grupos", 0)
    filas = result.get("filas", [])
    lugares, categorias, otros = _clasificar(filters)

    if total_grupos == 0 or not filas:
        return _pieza(_vacio(measure, filters))

    noun = _sustantivo(categorias, measure)
    lugar = _frase_lugar(lugares, capitalizar=True)
    loc_es = _frase_lugar(lugares)
    countable = any(_es_sustantivo_col(k) for _et, _v, k in categorias)

    if not group_by:
        total = filas[0].get("total")
        if total is None:
            return _pieza(_vacio(measure, filters))
        total = int(total)
        if countable:
            hay = _hay_countable(total, noun)
            cifra = verbalizar(total, _genero(noun))
            variantes = [
                f"{lugar + ' ' if lugar else ''}hay {hay}.",
                f"La cifra de {noun}{' ' + loc_es if loc_es else ''} es de {cifra}.",
                f"{lugar + ' ' if lugar else ''}el total de {noun} es de {cifra}.",
            ]
            # Sustantivo sin plural en plural (p. ej. "unidad movil" con 48):
            # se evita "hay 48 unidad movil", que suena mal en voz.
            if total != 1 and not noun.rstrip().endswith("s"):
                variantes = variantes[1:]
            return _pieza(_capitalizar(variantes[total % 3]),
                          breve=f"hay {hay}.", lugar=lugar, loc_es=loc_es, hay=hay)
        cifra = f"alrededor de {verbalizar(total, aprox=False)}"
        extra = f" {loc_es}" if loc_es else " total"
        variantes = [
            f"La {noun}{extra} es de {cifra}.",
            f"{lugar + ', ' if lugar else ''}la {noun} llega a {cifra}.",
            f"El total de {noun}{' ' + loc_es if loc_es else ''} es de {cifra}.",
        ]
        breves = [
            f"La {noun} es de {cifra}.",
            f"la {noun} llega a {cifra}.",
            f"El total de {noun} es de {cifra}.",
        ]
        return _pieza(_capitalizar(variantes[total % 3]),
                      breve=breves[total % 3], lugar=lugar, loc_es=loc_es)

    et = etiqueta(group_by[0])
    grupo_sustantivo = _es_sustantivo_col(group_by[0])
    gen_item = _genero(noun) if (countable and not grupo_sustantivo) else "m"
    items = []
    for f in filas[:3]:
        nombres = ["sin dato" if f[g] is None else _valor_hablado(f[g], g) for g in group_by]
        val = f.get("total")
        if val is None:
            continue
        gen = _genero(nombres[0]) if grupo_sustantivo else gen_item
        items.append(f"{' - '.join(nombres)} con {verbalizar(int(val), gen)}")
    if not items:
        return _vacio(measure, filters)

    if grupo_sustantivo or (category_only(group_by)):
        cabeza = f"Por {et}{' ' + loc_es if loc_es else ''}, los principales son: "
    elif " " in et:
        cabeza = f"Por {et}{' ' + loc_es if loc_es else ''}, los de mayor {noun} son: "
    else:
        cabeza = f"Los {et}s con más {noun}{' ' + loc_es if loc_es else ''}: "

    texto = cabeza + _unir(items) + "."
    if total_grupos > 3:
        texto += f" En total son {verbalizar(total_grupos)}. ¿Quieres que te diga el resto?"
    texto = _capitalizar(texto)
    return _pieza(texto, breve=texto, lugar=lugar, loc_es=loc_es)


def render_aggregate(result: dict, measure: str, group_by: list[str],
                     filters: dict | None = None) -> str:
    """Texto hablado para un resultado agregado (compatibilidad con tests)."""
    return _agregado_pieza(result, measure, group_by, filters)["texto"]


def category_only(group_by: list[str]) -> bool:
    return _tipo_columna(group_by[0]) == "categoria" if group_by else False


def _contar_pieza(result: dict, filters: dict | None = None) -> dict:
    total = int(result.get("total", 0) or 0)
    lugares, categorias, otros = _clasificar(filters)
    detalle = _frase_categorias(categorias) + _frase_otros(otros)
    loc_es = _frase_lugar(lugares)
    lugar = _frase_lugar(lugares, capitalizar=True)

    if total == 0:
        return _pieza(f"No encontré registros{(' ' + loc_es) if loc_es else ''}{detalle}.")

    n = verbalizar(total)
    variantes = [
        f"{lugar + ' ' if lugar else ''}hay {n} registros{detalle}.",
        f"{lugar + ' ' if lugar else ''}el conteo es de {n} registros{detalle}.",
        f"Conté {n} registros{detalle}{(' ' + loc_es) if loc_es else ''}.",
    ]
    return _pieza(_capitalizar(variantes[total % 3]),
                  breve=f"hay {n} registros{detalle}.", lugar=lugar, loc_es=loc_es)


def render_count(result: dict, filters: dict | None = None) -> str:
    """Texto hablado para un conteo (compatibilidad con tests)."""
    return _contar_pieza(result, filters)["texto"]


def _elegir_nombre(fila: dict, cols: list[str], query: str = "") -> tuple[str | None, str]:
    """Elige la columna que encabeza la respuesta y su valor: (columna, valor).

    Dos reglas sobre la similitud simple:
    1. Si un campo es ECO literal de lo que el usuario dijo (buscó por
       código "9140500019" y ese mismo valor vive en codigo_sede), se
       descarta como "nombre" — encontrar lo que se buscó no es informativo.
    2. Entre el resto, se prefiere texto sobre números puros (un código no
       es un buen encabezado aunque coincida), y luego la mayor similitud.
    """
    q = _norm(query)
    candidatos = []
    for c in cols:
        v = fila.get(c)
        if not v:
            continue
        v = str(v)
        es_numerico = v.replace(" ", "").isdigit()
        es_email = "@" in v
        # Eco solo importa para un código: que el usuario haya dicho ese
        # número no lo hace "el nombre". Un nombre que coincide exacto SÍ
        # es la mejor respuesta posible (es justo lo que se buscaba).
        if q and es_numerico and _norm(v) == q:
            continue
        # Prioridad como encabezado: texto normal > email > número puro.
        # Un email es dato válido (de detalle), pero empezar la frase
        # hablada leyéndolo letra por letra es mala experiencia.
        prioridad = 0 if es_numerico else (1 if es_email else 2)
        score = difflib.SequenceMatcher(None, q, _norm(v)).ratio() if q else 1.0
        candidatos.append((prioridad, score, c, v))
    if not candidatos:
        return None, "Una entidad"
    candidatos.sort(key=lambda t: (t[0], t[1]), reverse=True)
    _, _, col, val = candidatos[0]
    return col, val


def render_lookup(result: dict, name_cols: list[str] | None = None,
                  filters: dict | None = None, text_query: str = "") -> str:
    total = int(result.get("total", 0) or 0)
    filas = result.get("filas", [])
    lugares, categorias, otros = _clasificar(filters)
    loc_es = _frase_lugar(lugares)
    detalle_filtro = ((" " + loc_es) if loc_es else "") + _frase_categorias(categorias)

    if total == 0 or not filas:
        return f"No encontré esa entidad{detalle_filtro}."

    cols = name_cols or []
    resueltos = [_elegir_nombre(f, cols, text_query) for f in filas[:5]]
    nombres_unicos = []
    for _, nombre in resueltos:
        if nombre not in nombres_unicos:
            nombres_unicos.append(nombre)

    # La búsqueda ya converge en una sola entidad real: el usuario necesita
    # el detalle (dirección, gerente, teléfono...), no que se le repita
    # "encontré N coincidencias" sin decir nada nuevo.
    if total <= 2 or len(nombres_unicos) == 1:
        col_nombre, nombre = resueltos[0]
        fila = filas[0]
        excluir = {col_nombre} | set((filters or {}).keys())
        detalles = []
        for c in cols:
            if c in excluir:
                continue
            v = fila.get(c)
            if not v or not isinstance(v, str):
                continue
            detalles.append(f"{etiqueta(c)} {v}")
            if len(detalles) == 3:
                break
        if detalles:
            return f"{nombre}: " + ", ".join(detalles) + "."
        return f"Encontré {nombre}{detalle_filtro}, pero no tengo más detalle que mostrar."

    nombres = nombres_unicos[:3]
    texto = f"Encontré {total} coincidencias{detalle_filtro}. Las principales son: " + ", ".join(nombres)
    if total > 3:
        texto += " ¿Quieres que te mencione otras?"
    return texto


def render_list_values(result: dict, dimension: str | None = None) -> str:
    total = int(result.get("total", 0) or 0)
    valores = [_valor_hablado(v, dimension) for v in result.get("values", [])]

    if total == 0 or not valores:
        return "No encontré valores disponibles."

    et = etiqueta(dimension) if dimension else "esa categoría"
    n = min(3, len(valores))
    algunos = valores[:n]
    variantes = [
        f"Puedes consultar por {et}: {_unir(algunos)}.",
        f"Hay {verbalizar(total)} opciones de {et}. Por ejemplo: {_unir(algunos)}.",
    ]
    texto = variantes[total % 2]
    if total > n:
        texto += " ¿Quieres que te diga el resto?"
    return texto


def render_sql(result: dict) -> str:
    """Vuelca filas crudas de consulta_sql a texto plano. Crudo a propósito:
    el redactor (segundo salto) lo convierte en respuesta hablada."""
    filas = result.get("filas") or []
    if not filas:
        return "La consulta no devolvió resultados."
    partes = []
    for fila in filas[:5]:
        partes.append("; ".join(f"{k}: {v}" for k, v in fila.items() if v not in (None, "")))
    cola = f" (y {len(filas) - 5} más)" if len(filas) > 5 else ""
    return f"{len(filas)} resultado(s). " + " | ".join(partes) + cola


def componer(piezas: list[dict]) -> str:
    """Une las piezas de un mismo turno sin repetir el contexto compartido.

    Varias llamadas a tools del LLM (p. ej. "camas, consultorios y ambulancias
    por separado") producen una pieza cada una. Si todas son contables y con el
    mismo lugar, se fusionan en una sola enumeración: "En Pereira hay X camas,
    Y consultorios y Z ambulancias". Si el lugar se repite pero algo no es
    fusionable, el resto va con conector ("Además", "También") y sin repetir el
    lugar. Con lugares distintos, cada pieza conserva su frase completa.
    """
    if len(piezas) <= 1:
        return piezas[0]["texto"] if piezas else ""

    grupos: list[list[dict]] = []
    indice: dict[str, int] = {}
    for p in piezas:
        clave = p.get("lugar_es") or ""
        if clave not in indice:
            indice[clave] = len(grupos)
            grupos.append([])
        grupos[indice[clave]].append(p)

    salidas: list[str] = []
    ci = 0
    for grupo in grupos:
        clave = grupo[0].get("lugar_es") or ""
        # Fusión: mismo lugar y todo contable -> una sola enumeración.
        if clave and all(p.get("hay") for p in grupo):
            cabeza = f"{grupo[0]['lugar']} hay "
            salidas.append(_capitalizar(cabeza + _unir([p["hay"] for p in grupo]) + "."))
            continue
        for i, p in enumerate(grupo):
            if clave and i > 0:
                conector = _CONECTORES[ci % len(_CONECTORES)]
                ci += 1
                salidas.append(_capitalizar(conector + p["texto_breve"]))
            else:
                salidas.append(p["texto"])
    return " ".join(salidas)


def render_disambiguation(original: str, candidates: list[str],
                          dimension: str | None = None) -> str:
    """Pregunta de aclaración.

    `candidates` son dimensiones ('municipio', 'departamento') para el caso
    ambiguo, o valores canónicos para el caso de empate difuso.
    """
    if len(candidates) >= 2:
        if dimension is None and all(c in ("municipio", "departamento") for c in candidates[:2]):
            return (f"¿{original} el {_valor_hablado(candidates[0])}, "
                    f"o el {_valor_hablado(candidates[1])}?")
        return f"Tengo dos opciones para '{original}': ¿{candidates[0]} o {candidates[1]}?"
    return f"No estoy seguro de '{original}'. ¿Puedes decírmelo de otra forma?"


# ── Verificación (sin DB) ───────────────────────────────────────────────────

def demo():
    # Números con concordancia
    assert verbalizar(6430) == "unos seis mil cuatrocientos", verbalizar(6430)
    assert verbalizar(2000) == "dos mil", verbalizar(2000)
    assert verbalizar(6430, "f") == "unas seis mil cuatrocientas", verbalizar(6430, "f")
    assert verbalizar(0) == "cero"
    print("ok: verbalización con redondeo y concordancia de género")

    # Etiquetas naturales
    assert etiqueta("num_cantidad_capacidad_instalada") == "capacidad instalada", \
        etiqueta("num_cantidad_capacidad_instalada")
    assert etiqueta("nom_grupo_capacidad") == "tipo de capacidad", etiqueta("nom_grupo_capacidad")
    assert etiqueta("num_nivel_atencion") == "nivel de atención", etiqueta("num_nivel_atencion")
    assert etiqueta("departamento") == "departamento"
    print("ok: etiquetas naturales derivadas del nombre de columna")

    # Agregado con categoría femenina: concordancia
    r = {"total_grupos": 1, "filas": [{"total": 2632}]}
    txt = render_aggregate(r, "num_cantidad_capacidad_instalada", [],
                           {"departamento": "Nariño", "nom_grupo_capacidad": "CAMAS"})
    assert "camas" in txt and "Nariño" in txt and "dos mil seiscientas" in txt, txt
    print("ok: agregado femenino ->", txt)

    # Agregado sin categoría: usa la medida y evita concordancia
    txt = render_aggregate({"total_grupos": 1, "filas": [{"total": 216613}]},
                           "num_cantidad_capacidad_instalada", [], {})
    assert "capacidad instalada" in txt and "alrededor de" in txt, txt
    print("ok: agregado con medida ->", txt)

    # Agrupado por dimensión sustantiva
    r = {"total_grupos": 2, "filas": [
        {"nom_grupo_capacidad": "CAMAS", "total": 2632},
        {"nom_grupo_capacidad": "CONSULTORIOS", "total": 2000}]}
    txt = render_aggregate(r, "num_cantidad_capacidad_instalada", ["nom_grupo_capacidad"],
                           {"departamento": "Nariño"})
    assert "camas" in txt and "consultorios" in txt and "Nariño" in txt, txt
    print("ok: agrupado por categoría ->", txt)

    # Contexto de conectores: varias categorías contables con el mismo lugar
    # se fusionan en una sola enumeración (Pereira no se repite).
    m = "num_cantidad_capacidad_instalada"
    piezas = [
        _agregado_pieza({"total_grupos": 1, "filas": [{"total": 1600}]}, m, [],
                        {"municipio": "Pereira", "nom_grupo_capacidad": "CAMAS"}),
        _agregado_pieza({"total_grupos": 1, "filas": [{"total": 1100}]}, m, [],
                        {"municipio": "Pereira", "nom_grupo_capacidad": "CONSULTORIOS"}),
        _agregado_pieza({"total_grupos": 1, "filas": [{"total": 27}]}, m, [],
                        {"municipio": "Pereira", "nom_grupo_capacidad": "AMBULANCIAS"}),
    ]
    txt = componer(piezas)
    assert txt.count("Pereira") == 1, txt
    assert "camas" in txt and "consultorios" in txt and "ambulancias" in txt, txt
    print("ok: fusión del mismo lugar ->", txt)

    # Caso mixto (contable + medida): conector y sin repetir el lugar.
    txt = componer([
        _agregado_pieza({"total_grupos": 1, "filas": [{"total": 1600}]}, m, [],
                        {"municipio": "Pereira", "nom_grupo_capacidad": "CAMAS"}),
        _agregado_pieza({"total_grupos": 1, "filas": [{"total": 216613}]}, m, [],
                        {"municipio": "Pereira"}),
    ])
    assert txt.count("Pereira") == 1, txt
    assert any(c.strip() in txt for c in _CONECTORES), txt
    print("ok: caso mixto sin repetir lugar ->", txt)

    # Conteo con filtros naturales
    txt = render_count({"total": 12}, {"naturaleza": "Pública", "departamento": "Amazonas"})
    assert "Amazonas" in txt and "pública" in txt, txt
    print("ok: conteo natural ->", txt)

    # Búsqueda: nombre correcto y lugar capitalizado
    txt = render_lookup({"total": 1, "filas": [{"nombre_prestador": "E.S.E. HOSPITAL SAN RAFAEL"}]},
                        ["nombre_prestador"], {"municipio": "leticia"}, "hospital san rafael")
    assert "E.S.E. HOSPITAL SAN RAFAEL" in txt and "Leticia" in txt, txt
    print("ok: búsqueda natural ->", txt)

    # Lista de valores
    txt = render_list_values({"total": 7, "values": ["AMBULANCIAS", "CAMAS", "CAMILLAS", "SILLAS"]},
                             "nom_grupo_capacidad")
    assert "tipo de capacidad" in txt and "ambulancias" in txt and "resto" in txt, txt
    print("ok: lista natural ->", txt)

    # Desambiguación estilo arquitectura
    assert render_disambiguation("Cali", ["municipio", "departamento"]) == \
        "¿Cali el municipio, o el departamento?", render_disambiguation("Cali", ["municipio", "departamento"])
    print("ok: desambiguación '¿Cali el municipio, o el departamento?'")

    # Caso real 1: "información general de uramédicos" -> 2 filas, misma
    # entidad (nombre repetido) -> debe dar DETALLE, no solo repetir el nombre.
    cols = ["nombre_prestador", "nom_sede_ips", "gerente", "direccion", "email"]
    filas_urame = [
        {"nombre_prestador": "URAMEDICOS", "nom_sede_ips": "URAMEDICOS",
         "gerente": "CLAUDIA CECILIA TRUJILLO GOMEZ", "direccion": "KR 98 # 103-29/37",
         "email": "uramedicos@gmail.com"},
        {"nombre_prestador": "URAMEDICOS", "nom_sede_ips": "URAMEDICOS",
         "gerente": "CLAUDIA CECILIA TRUJILLO GOMEZ", "direccion": "KR 98 # 103-29/37",
         "email": "uramedicos@gmail.com"},
    ]
    txt = render_lookup({"total": 2, "filas": filas_urame}, cols, {}, "uramedicos")
    assert "URAMEDICOS" in txt and ("KR 98" in txt or "TRUJILLO" in txt or "gmail" in txt), txt
    assert "Encontré 2 coincidencias" not in txt, txt
    print("ok: lookup de entidad única da detalle, no solo el nombre ->", txt)

    # Caso real 2: buscar por código exacto -> el código NO debe aparecer
    # como "nombre" (antes salía "Las principales son: 9140500019").
    fila_cod = {"nombre_prestador": "HOSPITAL SAN RAFAEL", "codigo_sede": "9140500019",
                "direccion": "CALLE FALSA 123"}
    col, nombre = _elegir_nombre(fila_cod, ["nombre_prestador", "codigo_sede"], "9140500019")
    assert nombre == "HOSPITAL SAN RAFAEL", nombre
    print("ok: código buscado no se confunde con el nombre ->", nombre)

    # Caso 3: varias entidades distintas de verdad -> sigue listando nombres.
    filas_varias = [{"nombre_prestador": "HOSPITAL A"}, {"nombre_prestador": "HOSPITAL B"},
                     {"nombre_prestador": "HOSPITAL C"}, {"nombre_prestador": "HOSPITAL D"}]
    txt = render_lookup({"total": 4, "filas": filas_varias}, ["nombre_prestador"], {}, "hospital")
    assert "Encontré 4 coincidencias" in txt and "HOSPITAL A" in txt, txt
    print("ok: varias entidades distintas siguen listándose ->", txt)


if __name__ == "__main__":
    demo()
