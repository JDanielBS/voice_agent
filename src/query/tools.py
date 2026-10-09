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
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parents[2] / "data"
SCHEMA_PATH = DATA_DIR / "schema.json"

MAX_RESULTS = 5
# Regla 12: timeout corto y explícito. El statement mide ~150 ms en caliente,
# pero el pooler remoto (Supabase us-east-1) mete cientos de ms de varianza y
# una consulta fría supera 500 ms; 500 ms convertía una consulta válida en
# fallo (regla 7: nunca silencio). 2 s sigue cortando un runaway sin fallar.
STATEMENT_TIMEOUT_MS = 2000
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
    """Para group_by: agrupar por un identificador de alta cardinalidad
    (nombre, email, código) no tiene sentido por voz, exploraría miles de
    grupos. Esto se mantiene restringido a dimensión cerrada/abierta."""
    col = _exigir_columna(cols, name)
    if col["role"] not in DIM_ROLES:
        raise ValueError(f"{name!r} no es dimensión agrupable (rol: {col['role']})")
    return col


def _exigir_filtrable(cols: dict, name: str) -> dict:
    """Para WHERE: filtrar por un identificador exacto (código, email,
    gerente) sí tiene sentido (una igualdad, no una explosión de grupos)."""
    col = _exigir_columna(cols, name)
    if col["role"] not in (*DIM_ROLES, "identifier"):
        raise ValueError(f"{name!r} no es filtrable (rol: {col['role']})")
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
    """Cláusula WHERE por containment sobre un JSONB constante y parametrizado.

    `data @> %s::jsonb` con el objeto ya serializado deja que el planner use el
    índice GIN(data jsonb_path_ops): ~100 ms sobre 38k filas. Con el primitivo
    `jsonb_build_object(%s, %s)` el planner no puede usar el índice (no es
    constante) y hacía seq scan (~600 ms, por encima del timeout de 500 ms).
    La clave sale de schema.json ya validado y el valor viaja como parámetro.
    """
    clauses, params = [], []
    for k, v in (filters or {}).items():
        _exigir_filtrable(cols, k)
        clauses.append("data @> %s::jsonb")
        params.append(json.dumps({k: str(v)}, ensure_ascii=False))
    return (" WHERE " + " AND ".join(clauses)) if clauses else "", params


# ── Columnas buscables para lookup ───────────────────────────────────────────

def search_columns(schema: dict | None = None) -> list[str]:
    """Todos los identificadores (nombre, email, teléfono, código, gerente):
    el dataset es público (REPS/datos.gov.co), se expone todo sin filtrar
    por tipo de columna. La decisión sale solo del rol en schema.json."""
    schema = schema or load_schema()
    return [c["name"] for c in schema["columns"] if c["role"] == "identifier"]


def _documento_texto(col_names: list[str]) -> tuple[str, list]:
    """Concatenación plana de las columnas buscables, sin tsvector: la usa
    tanto _documento() (FTS) como el fallback de trigramas (texto plano)."""
    partes, params = [], []
    for name in col_names:
        partes.append("coalesce(data->>%s, '')")
        params.append(name)
    return " || ' ' || ".join(partes), params


def _documento(col_names: list[str]) -> tuple[str, list]:
    """Expresión tsvector sobre las columnas buscables. Claves como placeholders."""
    texto, params = _documento_texto(col_names)
    return f"to_tsvector('spanish', {texto})", params


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


UMBRAL_TRGM = 0.3


def build_trgm_index_sql(schema: dict | None = None) -> list[str]:
    """Un índice de trigramas POR columna buscable (regla 6 de CLAUDE.md: el
    resolver difuso no es opcional, el STT a 8 kHz se equivoca en nombres
    propios). Un solo índice sobre el documento concatenado no sirve aquí:
    el operador `%` (y el GUC pg_trgm.similarity_threshold, default 0.3,
    coincide con UMBRAL_TRGM) solo usa el índice si compara contra la MISMA
    expresión indexada; con columnas separadas, el planner puede resolver un
    OR de varias con bitmap index scan en vez de escanear toda la tabla.
    Requiere `CREATE EXTENSION IF NOT EXISTS pg_trgm` una vez en el montaje."""
    cols = search_columns(schema)
    if not cols:
        raise ValueError("schema sin columnas buscables para lookup")
    return [
        f"CREATE INDEX IF NOT EXISTS idx_raw_records_trgm_{c} ON raw_records "
        f"USING GIN ((data->>'{c}') gin_trgm_ops)"
        for c in cols  # c sale de schema.json
    ]


# ── Las 4 tools ──────────────────────────────────────────────────────────────

def aggregate_sql(measure: str, group_by: list[str], filters: dict,
                  top_n: int = MAX_RESULTS, schema: dict | None = None):
    schema = schema or load_schema()
    cols = _columnas(schema)
    _exigir_medida(cols, measure)
    group_by = list(group_by or [])
    for g in group_by:
        # Agrupar por un identificador (ej. nombre_prestador) es válido para
        # un RANKING: aggregate siempre acota con ORDER BY ... LIMIT top_n,
        # nunca enumera sin límite (eso es list_values, que sigue estricto).
        _exigir_filtrable(cols, g)
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


def lookup_fuzzy_sql(text_query: str, filters: dict, limit: int = MAX_RESULTS,
                     schema: dict | None = None):
    """Fallback cuando la búsqueda exacta/FTS no encuentra nada: similitud de
    trigramas (pg_trgm) tolera errores fonéticos del STT en nombres propios
    ("saida bibiana" por "Saida Viviana"), que ts_rank no detecta porque
    exige coincidencia de palabra completa, no distancia de edición.

    similarity() por COLUMNA, no sobre el documento concatenado: comparar
    "saida bibiana..." contra gerente+email+dirección+nombre... a la vez
    diluye la razón de trigramas compartidos (similarity cae de 0.70 a casi
    0 solo por el ruido de las demás columnas). GREATEST() toma la mejor
    columna, igual que ts_rank hace implícitamente al tokenizar por palabra.
    """
    if not (text_query or "").strip():
        raise ValueError("text_query vacío")
    schema = schema or load_schema()
    cols = search_columns(schema)
    if not cols:
        raise ValueError("schema sin columnas buscables para lookup")
    q = text_query.strip()
    # WHERE con `%` sobre data->>'col' LITERAL (no placeholder): el índice
    # GIN por columna se construyó sobre esa misma expresión exacta. Si la
    # clave viajara como %s, el planner no puede casar el índice y hace seq
    # scan completo (2.2s medidos sobre 38k filas, por encima del timeout).
    # c sale de schema.json, ya validado -> interpolar es seguro.
    ors = " OR ".join(f"(data->>'{c}') %% %s" for c in cols)
    or_params = [q for _ in cols]
    sims = ", ".join(f"similarity(coalesce(data->>'{c}', ''), %s)" for c in cols)
    sim_params = [q for _ in cols]
    rank = f"GREATEST({sims})"
    where, params = _where(filters, _columnas(schema))
    if where:
        sql = (f"SELECT data, {rank} AS rank, COUNT(*) OVER () AS n_total FROM raw_records"
               f"{where} AND ({ors}) ORDER BY rank DESC LIMIT %s")
    else:
        sql = (f"SELECT data, {rank} AS rank, COUNT(*) OVER () AS n_total FROM raw_records"
               f" WHERE {ors} ORDER BY rank DESC LIMIT %s")
    return sql, sim_params + params + or_params + [_pin(limit)]


def list_values_sql(dimension: str, schema: dict | None = None):
    schema = schema or load_schema()
    col = _exigir_dimension(_columnas(schema), dimension)
    if col["role"] == "closed_dimension":
        return None, []  # valores ya enumerados en schema.json: cero consultas
    # COUNT(*) OVER() se calcula ANTES del DISTINCT externo si va en la misma
    # consulta: cuenta filas totales, no valores únicos (bug real: reportaba
    # "38300 valores" para municipio en vez de ~1027). El DISTINCT va en una
    # subconsulta y se cuenta aparte, sobre el resultado ya deduplicado.
    sql = ("SELECT v, COUNT(*) OVER () AS total FROM "
           "(SELECT DISTINCT data->>%s AS v FROM raw_records WHERE data->>%s IS NOT NULL) t "
           "ORDER BY v LIMIT %s")
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
    if not grupos:
        # Sin GROUP BY la query es un solo agregado: con 0 filas devuelve 1 fila
        # con SUM(NULL) y COUNT(*) OVER () = 1 (no 0). Un total nulo = sin datos.
        if not out or out[0]["total"] is None:
            return {"total_grupos": 0, "filas": []}
        return {"total_grupos": 1, "filas": out}
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
    # Una sola conexión para el intento FTS y el fallback: dos conexiones al
    # pooler remoto (una por consulta) sumaban ~0.5s cada una y por poco
    # superaban el timeout de 2s, cuando la query real tarda ~40 ms.
    with _connect() as conn:
        rows = conn.execute(sql, params).fetchall()
        if not rows:
            # Fallback difuso solo si la búsqueda exacta no encontró nada.
            # ponytail: umbral fijo 0.3, sin mezclar el score con el de FTS
            # (escalas distintas); mejorar con un ranking combinado si hace falta.
            sql2, params2 = lookup_fuzzy_sql(text_query, filters or {}, limit)
            rows = conn.execute(sql2, params2).fetchall()
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
        (lambda: aggregate_sql("capacidad", ["fuente"], {}), "group_by constante"),
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

    # Ranking por identificador (ej. "qué IPS tiene más capacidad"): antes
    # se rechazaba por regla ("no es dimensión agrupable"), ahora es válido
    # porque aggregate siempre acota con LIMIT top_n. list_values NO cambia.
    sql, params = aggregate_sql("capacidad", ["nombre"], {}, 5, falso)
    assert "GROUP BY" in sql and params[0] == "nombre", (sql, params)
    print("ok: aggregate agrupa por identificador (ranking), list_values sigue estricto")

    # ningún valor de usuario aparece interpolado en el SQL
    sql, params = aggregate_sql("capacidad", ["depto"], {"depto": "Nariño"}, 5, falso)
    assert "Nariño" not in sql and "depto" not in sql.replace("data->>", ""), sql
    assert params == ["depto", "capacidad", '{"depto": "Nariño"}', 5], params
    print("ok: valores solo como placeholders:", params)

    # columnas buscables: TODOS los identificadores (dataset público, sin filtrar)
    assert search_columns(falso) == ["nombre", "email", "tel", "codigo"], search_columns(falso)
    idx = build_search_index_sql(falso)
    assert "USING GIN" in idx and "nombre" in idx and "email" in idx, idx
    trgm = build_trgm_index_sql(falso)
    assert len(trgm) == 4 and all("gin_trgm_ops" in s for s in trgm), trgm
    assert any("email" in s for s in trgm), trgm
    print("ok: lookup sobre todos los identificadores (nombre, email, tel, código)")

    # filtrar por un identificador (código exacto) sí funciona, antes se rechazaba
    sql, params = count_sql({"codigo": "504512253"}, falso)
    assert '"codigo": "504512253"' in params[0], params
    print("ok: count admite filtro por identificador exacto")

    # cerrada no toca la base
    sql, _ = list_values_sql("depto", falso)
    assert sql is None
    print("ok: list_values de dimensión cerrada no genera SQL")


if __name__ == "__main__":
    demo()
