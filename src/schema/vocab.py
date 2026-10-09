"""Vocabulario: valor canónico + forma normalizada + alias.

Construye la tabla de vocabulario a partir de schema.json y los datos en la DB.
El vocabulario se usa para:
  1. Resolver entidades habladas (query/resolve.py): "narinio" → "Nariño"
  2. Sesgar el STT hacia el dominio: lista de formas esperadas
  3. Detectar ambigüedades en montaje: un valor que aparece en >1 dimensión

Salida: data/vocab.json con esta estructura:
  {
    "entries": [
      {
        "canonical": "Nariño",
        "normalized": "narino",
        "aliases": ["narino", "nariño"],
        "dimension": "departamento"
      }, ...
    ],
    "ambiguous": {
      "cali": ["departamento", "municipio"]
    }
  }
"""
from __future__ import annotations

import json
import os
import unicodedata
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parents[2] / "data"


def normalize(text: str) -> str:
    """NFKD + minúsculas + quitar diacríticos. Misma función que usará resolve.py."""
    if not isinstance(text, str):
        text = str(text)
    # Descomponer caracteres unicode
    nfkd = unicodedata.normalize("NFKD", text)
    # Quitar marcas de combinación (tildes, diéresis, etc.)
    stripped = "".join(c for c in nfkd if unicodedata.category(c) != "Mn")
    return stripped.lower().strip()


def _generate_aliases(canonical: str) -> list[str]:
    """Genera variantes fonéticas comunes para español colombiano.

    ponytail: las alias se generan por reglas simples (quitar tildes,
    variantes sin 'h', etc.). Para un resolver más robusto, se puede
    entrenar un modelo fonético o usar Soundex para español.
    """
    base = normalize(canonical)
    aliases = {base}

    # Variante sin 'h' (habitual en STT): "choco" → "coco" no tiene sentido,
    # pero "huila" → "uila" sí ayuda. Solo quitar 'h' al inicio de palabra.
    words = base.split()
    without_h = " ".join(w.lstrip("h") if w.startswith("h") else w for w in words)
    if without_h != base and without_h:
        aliases.add(without_h)

    # "d.c" / "d.c." / "dc" para "bogota d.c."
    if "d.c" in base:
        aliases.add(base.replace("d.c.", "").replace("d.c", "").strip())
        aliases.add(base.replace("d.c.", "dc").replace("d.c", "dc"))

    # "san " → "sn " (error frecuente de STT)
    if "san " in base:
        aliases.add(base.replace("san ", "sn "))

    # "santa " → "sta " (abreviatura)
    if "santa " in base:
        aliases.add(base.replace("santa ", "sta "))

    return sorted(aliases)


def build_vocab(schema_path: Path | None = None) -> dict:
    """Construye el vocabulario completo desde schema.json.

    Solo procesa columnas con role closed_dimension u open_dimension.
    Para open_dimension, necesitaría los datos de la DB; aquí solo
    procesa las que tienen 'values' en el schema.
    """
    path = schema_path or DATA_DIR / "schema.json"
    with open(path, "r", encoding="utf-8") as f:
        schema = json.load(f)

    entries: list[dict] = []
    # Mapa de normalized → [dimensiones] para detectar ambigüedades
    norm_to_dims: dict[str, list[str]] = {}

    for col in schema["columns"]:
        role = col["role"]
        if role not in ("closed_dimension", "open_dimension"):
            continue

        col_name = col["name"]
        values = col.get("values", col.get("sample_values", []))

        for val in values:
            if val is None:
                continue
            canonical = str(val)
            normed = normalize(canonical)
            aliases = _generate_aliases(canonical)

            entries.append({
                "canonical": canonical,
                "normalized": normed,
                "aliases": aliases,
                "dimension": col_name,
            })

            # Rastrear en qué dimensiones aparece cada forma normalizada
            if normed not in norm_to_dims:
                norm_to_dims[normed] = []
            if col_name not in norm_to_dims[normed]:
                norm_to_dims[normed].append(col_name)

    # Detectar ambigüedades: valores que aparecen en >1 dimensión
    ambiguous = {
        normed: dims
        for normed, dims in norm_to_dims.items()
        if len(dims) > 1
    }

    vocab = {
        "total_entries": len(entries),
        "entries": entries,
        "ambiguous": ambiguous,
    }
    return vocab


def build_vocab_with_open_dims(schema_path: Path | None = None,
                                database_url: str | None = None) -> dict:
    """Construye vocabulario incluyendo open_dimension con datos de la DB.

    Para las dimensiones abiertas (ej. municipio con 1027 valores), schema.json
    solo tiene sample_values. Esta función consulta la DB para obtener todos
    los valores únicos de las dimensiones abiertas.
    """
    import psycopg

    path = schema_path or DATA_DIR / "schema.json"
    with open(path, "r", encoding="utf-8") as f:
        schema = json.load(f)

    if database_url is None:
        from dotenv import load_dotenv
        load_dotenv()
        database_url = os.environ["DATABASE_URL"]

    entries: list[dict] = []
    norm_to_dims: dict[str, list[str]] = {}

    for col in schema["columns"]:
        role = col["role"]
        if role not in ("closed_dimension", "open_dimension"):
            continue

        col_name = col["name"]

        if role == "closed_dimension":
            # Valores ya enumerados en el schema
            values = col.get("values", [])
        else:
            # open_dimension: consultar la DB para todos los valores únicos
            with psycopg.connect(database_url) as conn:
                rows = conn.execute(
                    "SELECT DISTINCT data->>%s FROM raw_records "
                    "WHERE data->>%s IS NOT NULL",
                    (col_name, col_name)
                ).fetchall()
            values = [r[0] for r in rows]

        for val in values:
            if val is None:
                continue
            canonical = str(val)
            normed = normalize(canonical)
            aliases = _generate_aliases(canonical)

            entries.append({
                "canonical": canonical,
                "normalized": normed,
                "aliases": aliases,
                "dimension": col_name,
            })

            if normed not in norm_to_dims:
                norm_to_dims[normed] = []
            if col_name not in norm_to_dims[normed]:
                norm_to_dims[normed].append(col_name)

    ambiguous = {
        normed: dims
        for normed, dims in norm_to_dims.items()
        if len(dims) > 1
    }

    vocab = {
        "total_entries": len(entries),
        "entries": entries,
        "ambiguous": ambiguous,
    }
    return vocab


def save_vocab(vocab: dict, path: Path | None = None) -> Path:
    """Escribe vocab.json en data/."""
    out = path or DATA_DIR / "vocab.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        json.dump(vocab, f, indent=2, ensure_ascii=False)
    return out


# ── Verificación ───────────────────────────────────────────────────────────

def demo():
    """Verificación con datos sintéticos."""
    # normalize
    assert normalize("Nariño") == "narino", normalize("Nariño")
    assert normalize("BOGOTÁ D.C.") == "bogota d.c.", normalize("BOGOTÁ D.C.")
    assert normalize("Chocó") == "choco", normalize("Chocó")
    assert normalize("  Huila ") == "huila", normalize("  Huila ")
    print("ok: normalize quita tildes y pasa a minúsculas")

    # aliases
    aliases_bogota = _generate_aliases("Bogotá D.C.")
    assert "bogota d.c." in aliases_bogota
    assert "bogota" in aliases_bogota or "bogota dc" in aliases_bogota
    print(f"ok: aliases para 'Bogotá D.C.' = {aliases_bogota}")

    aliases_huila = _generate_aliases("Huila")
    assert "huila" in aliases_huila
    assert "uila" in aliases_huila
    print(f"ok: aliases para 'Huila' = {aliases_huila}")

    aliases_santa = _generate_aliases("Santa Marta")
    assert "santa marta" in aliases_santa
    assert "sta marta" in aliases_santa
    print(f"ok: aliases para 'Santa Marta' = {aliases_santa}")

    # build_vocab con schema sintético
    import tempfile
    schema = {
        "total_rows": 100,
        "total_columns": 3,
        "columns": [
            {"name": "departamento", "role": "closed_dimension",
             "n_unique": 3, "null_count": 0, "null_ratio": 0.0,
             "values": ["Nariño", "Cali", "Antioquia"]},
            {"name": "municipio", "role": "open_dimension",
             "n_unique": 50, "null_count": 0, "null_ratio": 0.0,
             "sample_values": ["Cali", "Pasto", "Medellín"]},
            {"name": "medida", "role": "measure",
             "n_unique": 80, "null_count": 0, "null_ratio": 0.0},
        ]
    }
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
        json.dump(schema, f, ensure_ascii=False)
        tmp_path = Path(f.name)
    try:
        vocab = build_vocab(tmp_path)
        assert vocab["total_entries"] == 6, f"esperaba 6 entries, got {vocab['total_entries']}"

        # "cali" aparece en departamento y municipio → ambiguo
        assert "cali" in vocab["ambiguous"], f"cali debería ser ambiguo: {vocab['ambiguous']}"
        assert set(vocab["ambiguous"]["cali"]) == {"departamento", "municipio"}
        print("ok: 'cali' detectado como ambiguo entre departamento y municipio")

        # Las medidas no generan entries
        measure_entries = [e for e in vocab["entries"] if e["dimension"] == "medida"]
        assert len(measure_entries) == 0, "measure no debe generar entries de vocabulario"
        print("ok: measure excluida del vocabulario")

    finally:
        tmp_path.unlink()

    print("ok: build_vocab genera estructura correcta y detecta ambigüedades")


def main():
    """Construye vocabulario desde schema.json real y lo guarda."""
    from dotenv import load_dotenv
    load_dotenv()

    schema_path = DATA_DIR / "schema.json"
    if not schema_path.exists():
        print("ERROR: data/schema.json no existe. Ejecuta infer.py primero.")
        raise SystemExit(1)

    database_url = os.environ.get("DATABASE_URL")

    # Intentar con open_dims si tenemos acceso a la DB
    if database_url:
        print("Construyendo vocabulario con dimensiones abiertas (desde DB)...")
        vocab = build_vocab_with_open_dims(schema_path, database_url)
    else:
        print("Construyendo vocabulario solo con valores del schema...")
        vocab = build_vocab(schema_path)

    out_path = save_vocab(vocab)
    print(f"vocab.json guardado en {out_path}")
    print(f"  {vocab['total_entries']} entries")
    if vocab["ambiguous"]:
        print(f"  ⚠ {len(vocab['ambiguous'])} valores ambiguos:")
        for val, dims in vocab["ambiguous"].items():
            print(f"    '{val}' → {dims}")
    else:
        print("  ✓ Sin ambigüedades detectadas")


if __name__ == "__main__":
    import sys
    if "--demo" in sys.argv:
        demo()
    else:
        main()
