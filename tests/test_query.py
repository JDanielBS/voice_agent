"""Prueba pequeña de fase 3: validadores, SQL parametrizado y resolver.

Sin frameworks: asserts directos. Lo que toca DB se prueba con curl (server
arriba), no aquí. Ejecutar desde la raíz del proyecto: python tests/test_query.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.query import tools
from src.query import resolve as res


FALSO = {"columns": [
    {"name": "capacidad", "role": "measure"},
    {"name": "depto", "role": "closed_dimension", "values": ["A", "B"]},
    {"name": "muni", "role": "open_dimension"},
    {"name": "nombre", "role": "identifier",
     "sample_values": ["Hospital San Rafael", "Clinica XYZ"]},
    {"name": "email", "role": "identifier", "sample_values": ["a@x.co", "b@y.co"]},
    {"name": "tel", "role": "identifier", "sample_values": ["1234567", "7654321"]},
    {"name": "codigo", "role": "identifier"},
    {"name": "fuente", "role": "constant"},
]}

VAB = {"entries": [
    {"canonical": "Nariño", "normalized": "narino", "aliases": ["narino"],
     "dimension": "depto"},
    {"canonical": "Cali", "normalized": "cali", "aliases": ["cali"],
     "dimension": "depto"},
    {"canonical": "CALI", "normalized": "cali", "aliases": ["cali"],
     "dimension": "muni"},
], "ambiguous": {"cali": ["depto", "muni"]}}


def test_validacion_antes_de_la_base():
    casos = [
        lambda: tools.aggregate_sql("otra", [], {}),
        lambda: tools.aggregate_sql("nombre", [], {}),
        lambda: tools.aggregate_sql("capacidad", ["codigo"], {}),
        lambda: tools.aggregate_sql("capacidad", [], {"email": "a"}),
        lambda: tools.count_sql({"capacidad": 1}),
        lambda: tools.lookup_sql("", {}),
        lambda: tools.list_values_sql("fuente"),
        lambda: tools.list_values_sql("no_existe"),
    ]
    for fn in casos:
        try:
            fn()
        except ValueError:
            continue
        raise AssertionError(f"no rechazó: {fn}")
    print("ok: 8 rechazos de validación")


def test_placeholders_y_top():
    sql, params = tools.aggregate_sql("capacidad", ["depto"], {"depto": "X"}, 99, FALSO)
    assert "X" not in sql, sql
    assert params[-1] == 5, params
    sql, params = tools.lookup_sql("san rafael', '1'='1", {}, 5, FALSO)
    assert "1'='1" not in sql, sql  # inyección queda como dato, no como SQL
    assert params.count("san rafael', '1'='1") == 2, params
    print("ok: inyección de ejemplo viaja como placeholder")


def test_search_columns():
    assert tools.search_columns(FALSO) == ["nombre"]
    print("ok: search_columns excluye email/teléfono/códigos")


def test_resolve():
    assert res.resolve("narino", vocab=VAB)["canonical"] == "Nariño"
    assert res.resolve("Cali", vocab=VAB)["status"] == "aclarar"
    assert res.resolve("Cali", dimension="muni", vocab=VAB)["status"] == "ok"
    assert res.resolve("zzz", vocab=VAB)["motivo"] == "sin_coincidencia"
    print("ok: resolve exacto/difuso/ambiguo/desconocido")


if __name__ == "__main__":
    test_validacion_antes_de_la_base()
    test_placeholders_y_top()
    test_search_columns()
    test_resolve()
    print("FASE3-SMOKE OK")
