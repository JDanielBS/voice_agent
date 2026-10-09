"""Detección de patrones conversacionales antes del LLM.

Tres atajos que evitan un salto al LLM (cero latencia, cero costo):

1. Repetición  — "repita", "qué dijiste", "no escuché"
2. Corrección  — "no, dije X", "me refería a X"
3. Cambio de tema — detectado por ausencia de filtros compartidos
"""
from __future__ import annotations

import re

# ── Patrones de repetición ───────────────────────────────────────────────────

_REPETIR = re.compile(
    r"\b(repit[ea]|repet[ií]|qu[ée] dijiste|no entend[ií]|no escuch[ée]|"
    r"c[oó]mo dijiste|otra vez|de nuevo|perd[oó]n|me lo repites|no o[ií])\b",
    re.IGNORECASE,
)


def es_repeticion(texto: str) -> bool:
    """True si el usuario pide que le repitan la última respuesta."""
    t = texto.strip()
    # Frases muy cortas que son pura petición de repetición
    if len(t) < 40 and _REPETIR.search(t):
        return True
    return False


# ── Patrones de corrección ───────────────────────────────────────────────────

_CORRECCION = re.compile(
    r"(?:no,?\s*(?:dije|quise decir|me refer[ií]a a|era)\s+)(.+)",
    re.IGNORECASE,
)


def detectar_correccion(texto: str) -> str | None:
    """Si el usuario corrige un valor, devuelve el valor nuevo. Si no, None."""
    m = _CORRECCION.search(texto.strip())
    if m:
        return m.group(1).strip().rstrip(".")
    return None


# ── Función principal: interceptar antes del LLM ─────────────────────────────

def interceptar(texto: str, state) -> str | None:
    """Intenta resolver el turno sin LLM.

    Retorna:
      - str con la respuesta si logró resolver (no pasar al LLM)
      - None si no aplica ningún atajo (pasar al LLM)
    """
    # 1. Repetición
    if es_repeticion(texto):
        if state.last_response:
            return state.last_response
        return "No tengo una respuesta anterior para repetir."

    # 2. Corrección — "no, dije Chocó"
    valor_nuevo = detectar_correccion(texto)
    if valor_nuevo and state.last_tool and state.last_tool_args:
        # Guardamos la corrección para que agent.py la re-ejecute
        state.pending_correction = valor_nuevo
        return None  # agent.py se encargará de re-ejecutar con el valor corregido

    return None


# ── Verificación ─────────────────────────────────────────────────────────────

def demo():
    assert es_repeticion("¿qué dijiste?")
    assert es_repeticion("repita por favor")
    assert es_repeticion("no entendí")
    assert es_repeticion("cómo dijiste?")
    assert es_repeticion("otra vez")
    assert not es_repeticion("¿cuántas camas hay en Chocó?")
    assert not es_repeticion("dime los hospitales de Bogotá")
    print("ok: patrones de repetición")

    assert detectar_correccion("no, dije Chocó") == "Chocó"
    assert detectar_correccion("no, quise decir Nariño") == "Nariño"
    assert detectar_correccion("no, me refería a camas") == "camas"
    assert detectar_correccion("cuántas camas hay") is None
    print("ok: patrones de corrección")


if __name__ == "__main__":
    demo()

