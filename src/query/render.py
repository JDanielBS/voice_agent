"""Post-proceso para oído (render).

Transforma los resultados crudos de las consultas en un texto fluido que el
STT puede leer, o que sirve como base para que el LLM lo redacte en casos atípicos.
Respuestas de una o dos frases. El dato primero, el contexto después.
Nunca listas largas; máximo 3 elementos enunciados.

Regla de conversación (ARQUITECTURA.md §5.4 y §10.6): toda respuesta enuncia
**qué** se midió y **con qué filtros**, para que un seguimiento del usuario
("¿y en Chocó?", "¿de qué?") tenga un antecedente claro y no un número huérfano.
Los nombres de columna se derivan del string del schema (nunca se codifican a
mano), de modo que cambiar de dataset no rompe la redacción.
"""
from __future__ import annotations

import difflib
import re
import unicodedata

UNIDADES = ["", "un", "dos", "tres", "cuatro", "cinco", "seis", "siete", "ocho", "nueve"]
DECENAS = ["", "diez", "veinte", "treinta", "cuarenta", "cincuenta", "sesenta", "setenta", "ochenta", "noventa"]
ESPECIALES = {11: "once", 12: "doce", 13: "trece", 14: "catorce", 15: "quince",
              16: "dieciséis", 17: "diecisiete", 18: "dieciocho", 19: "diecinueve",
              21: "veintiún", 22: "veintidós", 23: "veintitrés", 24: "veinticuatro",
              25: "veinticinco", 26: "veintiséis", 27: "veintisiete", 28: "veintiocho", 29: "veintinueve"}
CENTENAS = ["", "ciento", "doscientos", "trescientos", "cuatrocientos", "quinientos", "seiscientos", "setecientos", "ochocientos", "novecientos"]

# Prefijos que Socrata deja al mutilar nombres con tilde (c_digo, n_mero).
_MANGLE_PREFIXES = ("num_", "nom_", "nombre_", "n_mero_", "c_digo_", "codigo_")


def _verbalizar_menor_1000(n: int) -> str:
    if n == 100: return "cien"
    if n == 0: return ""
    c = n // 100
    r = n % 100
    res = CENTENAS[c]
    if r > 0:
        if r in ESPECIALES:
            res += " " + ESPECIALES[r]
        elif r < 10:
            res += " " + UNIDADES[r]
        else:
            d = r // 10
            u = r % 10
            if u == 0:
                res += " " + DECENAS[d]
            else:
                res += " " + DECENAS[d] + " y " + UNIDADES[u]
    return res.strip()


def verbalizar(n: int | float) -> str:
    """Verbaliza un número redondeando a la centena/decena si es grande."""
    n = int(n)
    if n == 0: return "cero"

    # Redondeo para oído
    prefijo = ""
    if n > 1000:
        r = round(n, -2)
        if r != n: prefijo = "unos "
        n = r
    elif n > 100:
        r = round(n, -1)
        if r != n: prefijo = "unos "
        n = r

    if n < 1000:
        v = _verbalizar_menor_1000(n)
        return f"{prefijo}{v}".strip()

    miles = n // 1000
    resto = n % 1000
    res = "mil" if miles == 1 else _verbalizar_menor_1000(miles) + " mil"
    if resto > 0:
        res += " " + _verbalizar_menor_1000(resto)
    return f"{prefijo}{res}".strip()


def humanizar_columna(col: str) -> str:
    """`num_cantidad_capacidad_instalada` -> 'cantidad capacidad instalada'.

    Heurística de presentación, no de acceso a datos: usa el string del schema
    como venga, sin depender de ningún nombre concreto del dataset.
    """
    name = col or ""
    for p in _MANGLE_PREFIXES:
        if name.startswith(p):
            name = name[len(p):]
            break
    return name.replace("_", " ").strip()


def _norm(s) -> str:
    """Minúsculas sin tildes ni puntuación, para comparar texto libre."""
    s = unicodedata.normalize("NFKD", str(s or "")).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9 ]", " ", s.lower()).strip()


def _valor_hablado(v) -> str:
    s = str(v)
    # Los categóricos vienen en mayúsculas (CAMAS): en voz suenan mejor en minúscula.
    if s.isupper() and len(s) > 1:
        return s.lower()
    return s


def describir_filtros(filters: dict | None) -> str:
    """'departamento Nariño y grupo capacidad camas' ('' si no hay filtros)."""
    if not filters:
        return ""
    partes = [f"{humanizar_columna(k)} {_valor_hablado(v)}" for k, v in filters.items()]
    if len(partes) == 1:
        return partes[0]
    return " y ".join([", ".join(partes[:-1]), partes[-1]])


def describir_dimensiones(group_by: list[str] | None) -> str:
    if not group_by:
        return ""
    return ", ".join(humanizar_columna(g) for g in group_by)


def aplicar_tono(texto: str, sentimiento: str) -> str:
    """Adapta el tono de la respuesta según el sentimiento detectado."""
    s = (sentimiento or "neutro").lower()
    if s == "urgente":
        return "Claro, aquí tienes la información rápida: " + texto
    elif s == "frustrado":
        return "Entiendo la molestia. " + texto
    elif s == "positivo":
        return "¡Con gusto! " + texto
    return texto


def render_aggregate(result: dict, measure: str, group_by: list[str],
                     filters: dict | None = None) -> str:
    total_grupos = result.get("total_grupos", 0)
    filas = result.get("filas", [])

    medida = humanizar_columna(measure) or "la capacidad"
    filtros = _en_filtros(filters)

    if total_grupos == 0 or not filas:
        return f"No encontré datos de {medida}{filtros}."

    if not group_by:
        total = filas[0].get("total")
        if total is None:
            return f"No encontré datos de {medida}{filtros}."
        return f"El total de {medida}{filtros} es de {verbalizar(total)}."

    dims = describir_dimensiones(group_by)
    partes = []
    for f in filas[:3]:
        nombres = ["sin dato" if f[g] is None else str(f[g]) for g in group_by]
        nombre_grupo = " - ".join(nombres)
        val = f.get("total")
        if val is None:
            continue
        partes.append(f"{nombre_grupo} con {verbalizar(val)}")
    if not partes:
        return f"No encontré datos de {medida}{filtros}."

    texto = f"El total de {medida}{filtros}, por {dims}: los principales son " + ", ".join(partes)
    if total_grupos > 3:
        texto += f", y {total_grupos - 3} más."
    else:
        texto += "."
    return texto


def render_count(result: dict, filters: dict | None = None) -> str:
    total = result.get("total", 0)
    filtros = _en_filtros(filters)
    if total == 0:
        return f"No encontré registros{filtros}."
    return f"Encontré {verbalizar(total)} registros{filtros}."


def _buscar_nombre(fila: dict, cols: list[str], query: str = "") -> str | None:
    """Elige, entre las columnas buscables, la que mejor coincide con la consulta.

    Varias columnas (nombre del prestador, de la sede, del gerente) contienen
    texto libre. Sin este criterio se devolvía el primer campo no vacío, que
    podía ser el gerente en vez del hospital. La decisión es por similitud con
    lo que el usuario pidió, no por nombre de columna.
    """
    q = _norm(query)
    mejor, score_mejor = None, -1.0
    for c in cols:
        v = fila.get(c)
        if not v:
            continue
        score = difflib.SequenceMatcher(None, q, _norm(v)).ratio() if q else 1.0
        if score > score_mejor:
            mejor, score_mejor = str(v), score
    return mejor


def render_lookup(result: dict, name_cols: list[str] | None = None,
                  filters: dict | None = None, text_query: str = "") -> str:
    total = result.get("total", 0)
    filas = result.get("filas", [])
    filtros = _en_filtros(filters)

    if total == 0 or not filas:
        return f"No encontré ninguna entidad{filtros}."

    cols = name_cols or []
    nombres = []
    for f in filas[:5]:
        nombre = _buscar_nombre(f, cols, text_query)
        if nombre is None:
            # Fallback genérico: primer campo de texto largo que no sea un filtro.
            for k, v in f.items():
                if k not in (filters or {}) and isinstance(v, str) and len(v) > 8:
                    nombre = v
                    break
        nombre = nombre or "Una entidad"
        if nombre not in nombres:
            nombres.append(nombre)
        if len(nombres) == 3:
            break
    nombres = nombres or ["Una entidad"]

    texto = f"Encontré {total} coincidencias{filtros}. Las principales son: " + ", ".join(nombres)
    if total > 3:
        texto += f", y {total - 3} más."
    else:
        texto += "."
    return texto


def render_list_values(result: dict, dimension: str | None = None) -> str:
    total = result.get("total", 0)
    valores = result.get("values", [])

    if total == 0 or not valores:
        return "No encontré valores disponibles."

    label = humanizar_columna(dimension) if dimension else "esa dimensión"
    partes = [str(v) for v in valores[:3]]
    texto = f"Hay {verbalizar(total)} valores para {label}. Algunos son: " + ", ".join(partes)
    if total > 3:
        texto += f", y {total - 3} más."
    else:
        texto += "."
    return texto


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


def _en_filtros(filters: dict | None) -> str:
    """' en departamento Nariño y grupo capacidad camas' ('' si no hay)."""
    txt = describir_filtros(filters)
    return f" en {txt}" if txt else ""


# ── Verificación (sin DB) ───────────────────────────────────────────────────

def demo():
    # Números
    assert verbalizar(6430) == "unos seis mil cuatrocientos", verbalizar(6430)
    assert verbalizar(2000) == "dos mil", verbalizar(2000)
    assert verbalizar(0) == "cero"
    print("ok: verbalización con redondeo a centena")

    # Nombre de columna -> etiqueta hablada
    assert humanizar_columna("num_cantidad_capacidad_instalada") == "cantidad capacidad instalada"
    assert humanizar_columna("nom_grupo_capacidad") == "grupo capacidad"
    assert humanizar_columna("departamento") == "departamento"
    print("ok: etiquetas derivadas del nombre de columna")

    # Agregado sin grupos: enuncia medida Y filtros (lo que faltaba)
    r = {"total_grupos": 1, "filas": [{"total": 6430}]}
    txt = render_aggregate(r, "num_cantidad_capacidad_instalada", [], {"departamento": "Nariño"})
    assert "cantidad capacidad instalada" in txt and "Nariño" in txt and "seis mil cuatrocientos" in txt, txt
    print("ok: agregado autodescriptivo ->", txt)

    # Agregado con grupos
    r = {"total_grupos": 2, "filas": [
        {"nom_grupo_capacidad": "CAMAS", "total": 2632},
        {"nom_grupo_capacidad": "CONSULTORIOS", "total": 2000}]}
    txt = render_aggregate(r, "num_cantidad_capacidad_instalada", ["nom_grupo_capacidad"],
                           {"departamento": "Nariño"})
    assert "CAMAS" in txt and "grupo capacidad" in txt and "Nariño" in txt, txt
    print("ok: agregado agrupado ->", txt)

    # Conteo enuncia filtros
    txt = render_count({"total": 12}, {"naturaleza": "Pública", "departamento": "Amazonas"})
    assert "Pública" in txt and "Amazonas" in txt, txt
    print("ok: conteo autodescriptivo ->", txt)

    # Desambiguación estilo arquitectura
    assert render_disambiguation("Cali", ["municipio", "departamento"]) == \
        "¿Cali el municipio, o el departamento?", render_disambiguation("Cali", ["municipio", "departamento"])
    print("ok: desambiguación '¿Cali el municipio, o el departamento?'")


if __name__ == "__main__":
    demo()
