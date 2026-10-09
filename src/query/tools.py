"""Las 4 tools tipadas. El LLM elige tool y argumentos; este módulo construye el SQL.

Reglas que este archivo hace cumplir:
  - Ningún nombre de columna sale de otro sitio que schema.json (nunca del usuario).
  - Ningún valor de usuario se interpola en el SQL: todo va como placeholder %s.
  - Todo argumento inválido se rechaza con ValueError ANTES de tocar la base.
  - aggregate/count devuelven filas (el grano es sede x capacidad); el truncado
    "total + 3 primeros" lo aplica la capa de redacción (fase 4/6), no la tool.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parents[2] / "data"
SCHEMA_PATH = DATA_DIR / "schema.json"

MAX_RESULTS = 5
STATEMENT_TIMEOUT_MS = 500  # regla 12: timeouts cortos y explícitos
DIM_ROLES = ("closed_dimension", "open_dimension")

_schema_cache: dict | None = None


def load_schema(path: Path | None = None) -> dict:
    """schema.json una vez; de aquí salen todos los nombres de columna válidos."""
    global _schema_cache
    if _schema_cache is None or path is not None:
        with open(path or SCHEMA_PATH, "r", encoding="utf-8") as f:
            _schema_cache = json.load(f)
    return _schema_cache


def _columnas(schema: dict) -> dict[str, dict]:
    return {c["name"]: c for c in schema["columns"]}


def _exigir_columna(cols: dict, name: str) -> dict:
    try:
        return cols[name]
    except KeyError:
        raise ValueError(f"columna desconocida: {name!r}") from None


def _exigir_dimension(cols: dict, name: str) -> dict:
    col = _exigir_columna(cols, name)
    if col["role"] not in DIM_ROLES:
        raise ValueError(f"{name!r} no es dimensión filtrable (rol: {col['role']})")
    return col


def _exigir_medida(cols: dict, name: str) -> dict:
    col = _exigir_columna(cols, name)
    if col["role"] != "measure":
        raise ValueError(f"{name!r} no es medida (rol: {col['role']})")
    return col


def _pin(n: int, default: int = MAX_RESULTS) -> int:
    try:
        n = int(n)
    except (TypeError, ValueError):
        return default
    return min(max(n, 1), MAX_RESULTS)


def _where(filters: dict, cols: dict) -> tuple[str, list]:
    """Cláusula WHERE por containment (data @> {...}), no data->>%s = %s:
    sin índice por clave dinámica, el operador ->> hacía seq scan completo
    (3s sobre 38k filas). Con GIN(data jsonb_path_ops) + @>, baja a <0.2s.
    La clave sigue sin interpolarse: sale de schema.json ya validado."""
    clauses, params = [], []
    for k, v in (filters or {}).items():
        _exigir_dimension(cols, k)
        clauses.append("data @> jsonb_build_object(%s::text, %s::text)")
        params += [k, str(v)]
    return (" WHERE " + " AND ".join(clauses)) if clauses else "", params


# ── Columnas buscables para lookup ───────────────────────────────────────────

_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_FONO = re.compile(r"^[\d\s+\-()./]{6,}$")


def _es_contacto(samples: list) -> bool:
    """¿La mayoría de las muestras parecen email o teléfono? Sin muestras no se expone."""
    vals = [str(s).strip() for s in (samples or []) if s is not None]
    if not vals:
        return True
    def contacto(v: str) -> bool:
        return bool(_EMAIL.match(v)) or (
            bool(_FONO.match(v)) and sum(c.isdigit() for c in v) >= 6)
    return sum(map(contacto, vals)) / len(vals) >= 0.5


def search_columns(schema: dict | None = None) -> list[str]:
    """Identifiers con muestras de texto libre, menos emails/teléfonos.

    La decisión sale de los datos (role + sample_values + patrón de contacto),
    no de nombres hardcodeados: sirve para cualquier dataset.
    """
    schema = schema or load_schema()
    return [c["name"] for c in schema["columns"]
            if c["role"] == "identifier"
            and c.get("sample_values")
            and not _es_contacto(c["sample_values"])]


def _documento(col_names: list[str]) -> tuple[str, list]:
    """Expresión tsvector sobre las columnas buscables. Claves como placeholders."""
    partes, params = [], []
    for name in col_names:
        partes.append("coalesce(data->>%s, '')")
        params.append(name)
    return "to_tsvector('spanish', " + " || ' ' || ".join(partes) + ")", params


def build_search_index_sql(schema: dict | None = None) -> str:
    """SQL para crear el índice GIN del lookup. Función pura (testeable sin DB).

    El índice se crea UNA vez en el montaje, con rol de escritura. El runtime
    es read-only y jamás lo crea: si falta, lookup falla en cerrado, no lo inventa.
    """
    cols = search_columns(schema)
    if not cols:
        raise ValueError("schema sin columnas buscables para lookup")
    doc = " || ' ' || ".join(
        f"coalesce(data->>'{c}', '')" for c in cols)  # c sale de schema.json
    return (f"CREATE INDEX IF NOT EXISTS idx_raw_records_fts ON raw_records "
            f"USING GIN (to_tsvector('spanish', {doc}))")


# ── Las 4 tools ──────────────────────────────────────────────────────────────

def aggregate_sql(measure: str, group_by: list[str], filters: dict,
                  top_n: int = MAX_RESULTS, schema: dict | None = None):
    schema = schema or load_schema()
    cols = _columnas(schema)
    _exigir_medida(cols, measure)
    group_by = list(group_by or [])
    for g in group_by:
        _exigir_dimension(cols, g)
    where, params = _where(filters, cols)

    group_cols, group_params = [], []
    for g in group_by:
        group_cols.append("data->>%s")
        group_params.append(g)
    medida_expr = "(data->>%s)::double precision"
    # SUM ignora nulos; si un valor no fuera numérico la query falla en cerrado
    # (500, sin número inventado) en vez de devolver un total corrupto.
    # La medida cruda solo entra DENTRO de SUM(), nunca como columna suelta
    # del SELECT: si no, Postgres exige que esté en el GROUP BY.
    grp = ", ".join(str(i + 1) for i in range(len(group_by)))
    sel = group_cols + [f"SUM({medida_expr}) AS total", "COUNT(*) OVER () AS n_grupos"]
    sql = f"SELECT {', '.join(sel)} FROM raw_records{where}"
    if group_by:
        sql += f" GROUP BY {grp}"
    sql += " ORDER BY total DESC NULLS LAST LIMIT %s"
    return sql, group_params + [measure] + params + [_pin(top_n)]


def count_sql(filters: dict, schema: dict | None = None):
    schema = schema or load_schema()
    where, params = _where(filters, _columnas(schema))
    return f"SELECT COUNT(*) FROM raw_records{where}", params


def lookup_sql(text_query: str, filters: dict, limit: int = MAX_RESULTS,
               schema: dict | None = None):
    if not (text_query or "").strip():
        raise ValueError("text_query vacío")
    schema = schema or load_schema()
    cols = search_columns(schema)
    if not cols:
        raise ValueError("schema sin columnas buscables para lookup")
    doc, doc_params = _documento(cols)
    where, params = _where(filters, _columnas(schema))
    rank = f"ts_rank({doc}, plainto_tsquery('spanish', %s))"
    cond = f"{doc} @@ plainto_tsquery('spanish', %s)"
    if where:
        cond = f"({cond})"  # el WHERE de filtros ya existe; se añade con AND
        sql = (f"SELECT data, {rank} AS rank, COUNT(*) OVER () AS n_total FROM raw_records"
               f"{where} AND {cond} ORDER BY rank DESC LIMIT %s")
    else:
        sql = (f"SELECT data, {rank} AS rank, COUNT(*) OVER () AS n_total FROM raw_records"
               f" WHERE {cond} ORDER BY rank DESC LIMIT %s")
    q = text_query.strip()
    return sql, doc_params + [q] + params + doc_params + [q, _pin(limit)]


def list_values_sql(dimension: str, schema: dict | None = None):
    schema = schema or load_schema()
    col = _exigir_dimension(_columnas(schema), dimension)
    if col["role"] == "closed_dimension":
        return None, []  # valores ya enumerados en schema.json: cero consultas
    sql = ("SELECT DISTINCT data->>%s AS v, COUNT(*) OVER () AS total FROM raw_records "
           "WHERE data->>%s IS NOT NULL ORDER BY v LIMIT %s")
    return sql, [dimension, dimension, MAX_RESULTS]


# ── Ejecución ────────────────────────────────────────────────────────────────

def _connect():
    from dotenv import load_dotenv
    load_dotenv()
    url = os.environ.get("DATABASE_URL_READONLY") or os.environ["DATABASE_URL"]
    import psycopg
    conn = psycopg.connect(url)
    # SET no acepta placeholders (%s); STATEMENT_TIMEOUT_MS es constante del
    # código, no dato de usuario, por eso es seguro interpolarla directo.
    conn.execute(f"SET LOCAL statement_timeout = {STATEMENT_TIMEOUT_MS}")
    return conn
    # ponytail: conexión por request sobre el pooler; pool persistente si el
    # tráfico supera unas pocas req/s.


def aggregate(measure: str, group_by: list[str] | None = None,
              filters: dict | None = None, top_n: int = MAX_RESULTS) -> dict:
    sql, params = aggregate_sql(measure, group_by or [], filters or {}, top_n)
    with _connect() as conn:
        rows = conn.execute(sql, params).fetchall()
    grupos = list(group_by or [])
    out = []
    for r in rows:
        fila = {g: r[i] for i, g in enumerate(grupos)}
        fila["total"] = r[len(grupos)]
        out.append(fila)
    total = rows[0][len(grupos) + 1] if rows else 0
    return {"total_grupos": total, "filas": out}


def count(filters: dict | None = None) -> dict:
    sql, params = count_sql(filters or {})
    with _connect() as conn:
        total = conn.execute(sql, params).fetchone()[0]
    return {"total": total}


def lookup(text_query: str, filters: dict | None = None,
           limit: int = MAX_RESULTS) -> dict:
    sql, params = lookup_sql(text_query, filters or {}, limit)
    with _connect() as conn:
        rows = conn.execute(sql, params).fetchall()
    total = rows[0][2] if rows else 0
    return {"total": total, "filas": [r[0] for r in rows]}


def list_values(dimension: str) -> dict:
    schema = load_schema()
    col = _exigir_dimension(_columnas(schema), dimension)
    if col["role"] == "closed_dimension":
        vals = list(col.get("values", []))
        return {"total": len(vals), "values": vals}
    sql, params = list_values_sql(dimension, schema)
    with _connect() as conn:
        rows = conn.execute(sql, params).fetchall()
    total = rows[0][1] if rows else 0
    return {"total": total, "values": [r[0] for r in rows]}


# ── Verificación (sin DB) ────────────────────────────────────────────────────

def demo():
    falso = {"columns": [
        {"name": "capacidad", "role": "measure"},
        {"name": "depto", "role": "closed_dimension", "values": ["A", "B"]},
        {"name": "muni", "role": "open_dimension"},
        {"name": "nombre", "role": "identifier",
         "sample_values": ["Hospital San Rafael", "Clinica Leticia"]},
        {"name": "email", "role": "identifier",
         "sample_values": ["a@x.gov.co", "b@y.gov.co"]},
        {"name": "tel", "role": "identifier",
         "sample_values": ["3203016139", "8442020"]},
        {"name": "codigo", "role": "identifier"},
        {"name": "fuente", "role": "constant"},
    ]}
    # validación: rechaza antes de tocar la base
    for fn, exc in [
        (lambda: aggregate_sql("no_existe", [], {}), "medida"),
        (lambda: aggregate_sql("nombre", [], {}), "medida con rol"),
        (lambda: aggregate_sql("capacidad", ["nombre"], {}), "group_by"),
        (lambda: aggregate_sql("capacidad", [], {"fuente": "x"}), "filtro"),
        (lambda: count_sql({"capacidad": 1}), "filtro medida"),
        (lambda: lookup_sql("  ", {}), "text_query"),
        (lambda: list_values_sql("codigo"), "list_values"),
    ]:
        try:
            fn()
        except ValueError:
            pass
        else:
            raise AssertionError(f"no rechazó: {exc}")
    print("ok: argumentos inválidos se rechazan antes de la base")

    # top_n/limit se pinzan a [1, 5]
    _, p = aggregate_sql("capacidad", ["depto"], {}, 99, falso)
    assert p[-1] == 5, p
    _, p = lookup_sql("rafael", {}, 99, falso)
    assert p[-1] == 5, p
    print("ok: top_n/limit pinzados a máximo 5")

    # ningún valor de usuario aparece interpolado en el SQL
    sql, params = aggregate_sql("capacidad", ["depto"], {"depto": "Nariño"}, 5, falso)
    assert "Nariño" not in sql and "depto" not in sql.replace("data->>", ""), sql
    assert params == ["depto", "capacidad", "depto", "Nariño", 5], params
    print("ok: valores solo como placeholders:", params)

    # columnas buscables: texto libre sí, email/teléfono/códigos no
    assert search_columns(falso) == ["nombre"], search_columns(falso)
    idx = build_search_index_sql(falso)
    assert "USING GIN" in idx and "nombre" in idx and "email" not in idx, idx
    print("ok: lookup solo sobre identificadores de texto libre")

    # cerrada no toca la base
    sql, _ = list_values_sql("depto", falso)
    assert sql is None
    print("ok: list_values de dimensión cerrada no genera SQL")


if __name__ == "__main__":
    demo()
