"""Texto hablado -> valor canónico de una dimensión.

  1. Normaliza con vocab.normalize() (definida en fase 2; aquí solo se reusa).
  2. Match exacto contra normalized + aliases de vocab.json.
  3. Si falla: difflib.get_close_matches, umbral 0.7.
  4. Valor ambiguo, empate o nada sobre el umbral -> NO adivina: pide aclaración.
"""
from __future__ import annotations

import difflib
import json
from pathlib import Path

from src.schema.vocab import normalize

DATA_DIR = Path(__file__).resolve().parents[2] / "data"
VOCAB_PATH = DATA_DIR / "vocab.json"

UMBRAL = 0.7

_vocab_cache: dict | None = None


def load_vocab(path: Path | None = None) -> dict:
    global _vocab_cache
    if _vocab_cache is None or path is not None:
        with open(path or VOCAB_PATH, "r", encoding="utf-8") as f:
            _vocab_cache = json.load(f)
    return _vocab_cache


def _indice(entries: list[dict]) -> dict[str, dict]:
    """normalized y cada alias apuntan a su entry (gana la primera)."""
    idx: dict[str, dict] = {}
    for e in entries:
        for forma in [e["normalized"], *e.get("aliases", [])]:
            idx.setdefault(forma, e)
    return idx


def _ratio(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, a, b).ratio()


def resolve(texto: str, dimension: str | None = None,
            vocab: dict | None = None, umbral: float = UMBRAL) -> dict:
    """Resuelve lo transcrito a valor canónico.

    Devuelve {"status": "ok", "canonical", "dimension"} o
    {"status": "aclarar", "motivo", ...}. Nunca adivina en silencio.
    """
    vocab = vocab or load_vocab()
    norm = normalize(texto)
    if not norm:
        return {"status": "aclarar", "motivo": "vacio", "candidatos": []}

    entries = [e for e in vocab["entries"]
               if dimension is None or e["dimension"] == dimension]
    formas = _indice(entries)

    if norm in formas:
        entry = formas[norm]
    else:
        cerc = difflib.get_close_matches(norm, list(formas), n=3, cutoff=umbral)
        if not cerc:
            return {"status": "aclarar", "motivo": "sin_coincidencia", "candidatos": []}
        if len(cerc) > 1 and abs(_ratio(norm, cerc[0]) - _ratio(norm, cerc[1])) < 1e-9:
            return {"status": "aclarar", "motivo": "empate",
                    "candidatos": [formas[c]["canonical"] for c in cerc[:2]]}
        entry = formas[cerc[0]]

    # Ambiguo entre dimensiones: solo se acepta si la dimensión ya venía dada
    # (el slot la fija); con dimensión abierta se pregunta voz + DTMF (fase 6).
    amb = vocab.get("ambiguous", {})
    if entry["normalized"] in amb and entry["dimension"] != dimension:
        return {"status": "aclarar", "motivo": "ambiguo",
                "dimensiones": amb[entry["normalized"]],
                "candidatos": [entry["canonical"]]}
    return {"status": "ok", "canonical": entry["canonical"],
            "dimension": entry["dimension"]}


# ── Verificación (sin DB) ────────────────────────────────────────────────────

def demo():
    falso = {"entries": [
        {"canonical": "Nariño", "normalized": "narino",
         "aliases": ["narino"], "dimension": "departamento"},
        {"canonical": "Cali", "normalized": "cali",
         "aliases": ["cali"], "dimension": "departamento"},
        {"canonical": "CALI", "normalized": "cali",
         "aliases": ["cali"], "dimension": "municipio"},
        {"canonical": "Chocó", "normalized": "choco",
         "aliases": ["choco"], "dimension": "departamento"},
        {"canonical": "Chocontá", "normalized": "choconta",
         "aliases": ["choconta"], "dimension": "municipio"},
    ], "ambiguous": {"cali": ["departamento", "municipio"]}}

    r = resolve("Nariño", vocab=falso)
    assert r == {"status": "ok", "canonical": "Nariño", "dimension": "departamento"}, r
    print("ok: match exacto con tildes")

    r = resolve("narinio", vocab=falso)  # error fonético típico de STT
    assert r["status"] == "ok" and r["canonical"] == "Nariño", r
    print("ok: difuso 'narinio' -> 'Nariño'")

    r = resolve("choco", vocab=falso)
    assert r["status"] == "ok" and r["canonical"] == "Chocó", r
    print("ok: alias normalizado resuelve sin difuso")

    r = resolve("Cali", vocab=falso)  # ambiguo depto/municipio, sin dimensión
    assert r["status"] == "aclarar" and r["motivo"] == "ambiguo", r
    print("ok: 'Cali' ambiguo pide aclaración, no adivina")

    r = resolve("Cali", dimension="municipio", vocab=falso)
    assert r["status"] == "ok" and r["dimension"] == "municipio", r
    print("ok: 'Cali' con dimensión dada sí resuelve")

    r = resolve("xyzqw", vocab=falso)
    assert r == {"status": "aclarar", "motivo": "sin_coincidencia", "candidatos": []}, r
    print("ok: desconocido pide aclaración con cero candidatos")

    r = resolve("   ", vocab=falso)
    assert r["status"] == "aclarar" and r["motivo"] == "vacio", r
    print("ok: texto vacío no resuelve")


if __name__ == "__main__":
    demo()
