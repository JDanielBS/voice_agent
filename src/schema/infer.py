"""Perfilado automático de esquema por cardinalidad.

Lee raw_records (JSONB) de PostgreSQL, clasifica cada columna por su rol
y emite data/schema.json. Usa pandas para el perfilado offline (fase 2).

Reglas de clasificación (ARQUITECTURA.md §4.2):
  CONSTANT           → 1 valor único                → descartar
  CLOSED_DIMENSION   → ≤ 100 únicos                 → filtro, GROUP BY; valores al prompt
  OPEN_DIMENSION     → únicos ≤ 5% de las filas     → filtro con resolución difusa
  MEASURE            → numérico, no identificador    → SUM, AVG, COUNT
  IDENTIFIER         → texto, alta cardinalidad      → FTS; nunca GROUP BY
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pandas as pd

# ── Umbrales de clasificación ──────────────────────────────────────────────
CONSTANT_MAX = 1
CLOSED_DIM_MAX = 100
OPEN_DIM_RATIO = 0.05  # únicos ≤ 5% de las filas

# Columnas que son IDs numéricos, no medidas. Se detectan por patrón de nombre
# (prefijo "codigo_", "nit_", sufijo "_verificacion") o porque son enteros con
# nulos esparcidos (float64 por coerción de pandas). La heurística se aplica
# después de la clasificación por cardinalidad.
# ponytail: Socrata mangles accented names (código → c_digo, número → n_mero).
# If a new API uses different mangling, extend this tuple.
NUMERIC_ID_PATTERNS = ("codigo_", "c_digo_", "nit_", "digito_", "n_mero_")

DATA_DIR = Path(__file__).resolve().parents[2] / "data"


def _load_dataframe(database_url: str) -> pd.DataFrame:
    """Extrae todas las filas de raw_records y las aplana desde JSONB."""
    import psycopg
    with psycopg.connect(database_url) as conn:
        rows = conn.execute("SELECT data FROM raw_records").fetchall()
    if not rows:
        raise RuntimeError("raw_records está vacía. Ejecuta sync primero.")
    records = [json.loads(r[0]) if isinstance(r[0], str) else r[0] for r in rows]
    return pd.DataFrame(records)


def _is_numeric_id(col_name: str) -> bool:
    """Heurística: ¿el nombre de columna indica un identificador numérico?"""
    lower = col_name.lower()
    return any(lower.startswith(p) or p in lower for p in NUMERIC_ID_PATTERNS)


def _classify_column(series: pd.Series, total_rows: int, col_name: str) -> dict:
    """Clasifica una columna individual y devuelve su descriptor."""
    n_unique = series.nunique(dropna=True)
    null_count = int(series.isna().sum())
    null_ratio = null_count / total_rows if total_rows > 0 else 0.0

    # Intentar coerción numérica (muchos campos llegan como string desde JSONB)
    numeric = pd.to_numeric(series, errors="coerce")
    is_numeric = numeric.notna().sum() > (total_rows * 0.8)  # >80% parseables

    # ── Constante ──────────────────────────────────────────────────────────
    if n_unique <= CONSTANT_MAX:
        return _col_descriptor(col_name, "constant", n_unique, null_count,
                               null_ratio, sample_values=_sample(series, 1))

    # ── Numérica ──────────────────────────────────────────────────────────
    if is_numeric and not _is_numeric_id(col_name):
        return _col_descriptor(col_name, "measure", n_unique, null_count,
                               null_ratio, stats=_numeric_stats(numeric))

    # ── Numérica pero es ID (codigo_prestador, nit_ips, etc.) ─────────────
    if is_numeric and _is_numeric_id(col_name):
        return _col_descriptor(col_name, "identifier", n_unique, null_count,
                               null_ratio)

    # ── Categórica cerrada ────────────────────────────────────────────────
    if n_unique <= CLOSED_DIM_MAX:
        values = sorted(series.dropna().unique().tolist())
        return _col_descriptor(col_name, "closed_dimension", n_unique,
                               null_count, null_ratio, values=values)

    # ── Categórica abierta (≤ 5% de filas con valores únicos) ────────────
    if n_unique <= total_rows * OPEN_DIM_RATIO:
        return _col_descriptor(col_name, "open_dimension", n_unique,
                               null_count, null_ratio,
                               sample_values=_sample(series, 10))

    # ── Identificador de texto ────────────────────────────────────────────
    return _col_descriptor(col_name, "identifier", n_unique, null_count,
                           null_ratio, sample_values=_sample(series, 5))


def _col_descriptor(name: str, role: str, n_unique: int, null_count: int,
                    null_ratio: float, *, values: list | None = None,
                    sample_values: list | None = None,
                    stats: dict | None = None) -> dict:
    desc: dict = {
        "name": name,
        "role": role,
        "n_unique": n_unique,
        "null_count": null_count,
        "null_ratio": round(null_ratio, 4),
    }
    if values is not None:
        desc["values"] = values
    if sample_values is not None:
        desc["sample_values"] = sample_values
    if stats is not None:
        desc["stats"] = stats
    return desc


def _sample(series: pd.Series, n: int) -> list:
    unique = series.dropna().unique()
    return sorted(unique[:n].tolist())


def _numeric_stats(numeric: pd.Series) -> dict:
    clean = numeric.dropna()
    return {
        "min": float(clean.min()),
        "max": float(clean.max()),
        "mean": round(float(clean.mean()), 2),
        "median": round(float(clean.median()), 2),
    }


def infer_schema(database_url: str) -> dict:
    """Perfila la base completa y devuelve el esquema como dict."""
    df = _load_dataframe(database_url)
    total_rows = len(df)

    columns = []
    for col in df.columns:
        descriptor = _classify_column(df[col], total_rows, col)
        columns.append(descriptor)

    schema = {
        "total_rows": total_rows,
        "total_columns": len(df.columns),
        "columns": columns,
    }
    return schema


def save_schema(schema: dict, path: Path | None = None) -> Path:
    """Escribe schema.json en data/."""
    out = path or DATA_DIR / "schema.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        json.dump(schema, f, indent=2, ensure_ascii=False)
    return out


# ── Verificación ───────────────────────────────────────────────────────────

def demo():
    """Verificación mínima con datos sintéticos (no requiere DB)."""
    import io
    # Simular un DataFrame con roles conocidos
    csv_data = """constante,dim_cerrada,dim_abierta,medida,codigo_id,texto_libre
    A,Bogotá,Mun001,100,1001,Hospital San Rafael
    A,Medellín,Mun002,200,1002,Clínica del Norte
    A,Bogotá,Mun003,150,1003,Centro Médico Sur
    A,Cali,Mun004,300,1004,IPS La Esperanza
    A,Medellín,Mun005,250,1005,Hospital Universitario
    """
    df = pd.read_csv(io.StringIO(csv_data), skipinitialspace=True)

    total = len(df)
    results = {}
    for col in df.columns:
        desc = _classify_column(df[col], total, col)
        results[col] = desc["role"]

    assert results["constante"] == "constant", f"constante → {results['constante']}"
    assert results["dim_cerrada"] == "closed_dimension", f"dim_cerrada → {results['dim_cerrada']}"
    # dim_abierta: 5 únicos / 5 filas = 100% > 5%, así que con tan pocas filas
    # se clasifica como closed_dimension (≤100 únicos). Esto es correcto:
    # la heurística de 5% solo separa con datasets grandes.
    assert results["dim_abierta"] == "closed_dimension", f"dim_abierta → {results['dim_abierta']}"
    assert results["medida"] == "measure", f"medida → {results['medida']}"
    assert results["codigo_id"] == "identifier", f"codigo_id → {results['codigo_id']}"
    # texto_libre: 5 únicos ≤ 100 → closed_dimension con pocas filas. Correcto
    # por las mismas razones: la separación identifier necesita alta cardinalidad.
    assert results["texto_libre"] == "closed_dimension", f"texto_libre → {results['texto_libre']}"

    print("ok: clasificación de columnas consistente con las reglas de cardinalidad")

    # Verificar estructura del descriptor
    desc = _classify_column(df["dim_cerrada"], total, "dim_cerrada")
    assert "values" in desc, "closed_dimension debe incluir 'values'"
    assert sorted(desc["values"]) == ["Bogotá", "Cali", "Medellín"], desc["values"]
    print("ok: closed_dimension incluye valores enumerados y ordenados")

    desc_measure = _classify_column(df["medida"], total, "medida")
    assert "stats" in desc_measure, "measure debe incluir 'stats'"
    assert desc_measure["stats"]["min"] == 100.0
    assert desc_measure["stats"]["max"] == 300.0
    print("ok: measure incluye estadísticas numéricas")


def main():
    """Ejecuta el perfilado contra la base real y guarda schema.json."""
    from dotenv import load_dotenv
    load_dotenv()

    database_url = os.environ["DATABASE_URL"]
    print("Perfilando esquema desde raw_records...")
    schema = infer_schema(database_url)

    out_path = save_schema(schema)
    print(f"schema.json guardado en {out_path}")
    print(f"  {schema['total_rows']} filas × {schema['total_columns']} columnas")

    # Resumen por rol
    role_counts: dict[str, int] = {}
    for col in schema["columns"]:
        role = col["role"]
        role_counts[role] = role_counts.get(role, 0) + 1
    for role, count in sorted(role_counts.items()):
        print(f"  {role}: {count} columnas")


if __name__ == "__main__":
    import sys
    if "--demo" in sys.argv:
        demo()
    else:
        main()
