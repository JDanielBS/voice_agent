"""Post-proceso para oído (render).

Transforma los resultados crudos de las consultas en un texto fluido que el
STT puede leer, o que sirve como base para que el LLM lo redacte en casos atípicos.
Respuestas de una o dos frases. El dato primero, el contexto después.
Nunca listas largas; máximo 3 elementos enunciados.
"""
from __future__ import annotations

UNIDADES = ["", "un", "dos", "tres", "cuatro", "cinco", "seis", "siete", "ocho", "nueve"]
DECENAS = ["", "diez", "veinte", "treinta", "cuarenta", "cincuenta", "sesenta", "setenta", "ochenta", "noventa"]
ESPECIALES = {11: "once", 12: "doce", 13: "trece", 14: "catorce", 15: "quince",
              16: "dieciséis", 17: "diecisiete", 18: "dieciocho", 19: "diecinueve",
              21: "veintiún", 22: "veintidós", 23: "veintitrés", 24: "veinticuatro", 
              25: "veinticinco", 26: "veintiséis", 27: "veintisiete", 28: "veintiocho", 29: "veintinueve"}
CENTENAS = ["", "ciento", "doscientos", "trescientos", "cuatrocientos", "quinientos", "seiscientos", "setecientos", "ochocientos", "novecientos"]

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

def aplicar_tono(texto: str, sentimiento: str) -> str:
    """Adapta el tono de la respuesta según el sentimiento detectado."""
    s = sentimiento.lower()
    if s == "urgente":
        return "Claro, aquí tienes la información rápida: " + texto
    elif s == "frustrado":
        return "Entiendo la molestia. " + texto
    elif s == "positivo":
        return "¡Con gusto! " + texto
    return texto


def render_aggregate(result: dict, measure: str, group_by: list[str]) -> str:
    total_grupos = result.get("total_grupos", 0)
    filas = result.get("filas", [])
    
    if total_grupos == 0 or not filas:
        return "No encontré datos para esa consulta."

    if not group_by:
        total = filas[0].get("total", 0)
        return f"El total es {verbalizar(total)}."

    # Si hay grupos, enunciamos los 3 primeros
    partes = []
    for f in filas[:3]:
        nombres = [str(f[g]) for g in group_by]
        nombre_grupo = " - ".join(nombres)
        val = f.get("total", 0)
        partes.append(f"{nombre_grupo} tiene {verbalizar(val)}")
        
    texto = "Los principales son: " + ", ".join(partes)
    if total_grupos > 3:
        texto += f", y {total_grupos - 3} más."
    else:
        texto += "."
        
    return texto


def render_count(result: dict) -> str:
    total = result.get("total", 0)
    if total == 0:
        return "No encontré registros que coincidan."
    return f"Encontré un total de {verbalizar(total)}."


def render_lookup(result: dict) -> str:
    total = result.get("total", 0)
    filas = result.get("filas", [])
    
    if total == 0 or not filas:
        return "No encontré ninguna entidad con ese nombre."
        
    nombres = []
    for f in filas[:3]:
        nombre = f.get("nombre_prestador") or f.get("nom_sede_ips") or "Una entidad"
        nombres.append(nombre)
        
    texto = f"Encontré {total} resultados. Los principales son: " + ", ".join(nombres)
    if total > 3:
        texto += f", y {total - 3} más."
    else:
        texto += "."
    return texto


def render_list_values(result: dict) -> str:
    total = result.get("total", 0)
    valores = result.get("values", [])
    
    if total == 0 or not valores:
        return "No encontré valores disponibles."
        
    partes = [str(v) for v in valores[:3]]
    texto = f"Encontré {total} valores. Algunos son: " + ", ".join(partes)
    if total > 3:
        texto += f", y {total - 3} más."
    else:
        texto += "."
    return texto

def render_disambiguation(original: str, candidates: list[str]) -> str:
    """Renderiza una pregunta de desambiguación."""
    return f"Tengo un conflicto con '{original}'. ¿Te refieres a {candidates[0]} o {candidates[1]}?"

