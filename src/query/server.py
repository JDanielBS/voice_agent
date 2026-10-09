"""Las 4 tools por HTTP (webhook para la plataforma de voz).

La plataforma nunca toca la base: llama aquí, el servidor valida argumentos
contra schema.json y responde. Error de validación -> 400 (falla en cerrado);
error interno -> 500 sin filtrar detalles.
"""
from __future__ import annotations

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from src.query import tools

app = FastAPI(title="voice-tools-fase3")


class AggregateReq(BaseModel):
    measure: str
    group_by: list[str] = []
    filters: dict[str, str | int | float] = {}
    top_n: int = 5


class CountReq(BaseModel):
    filters: dict[str, str | int | float] = {}


class LookupReq(BaseModel):
    text_query: str
    filters: dict[str, str | int | float] = {}
    limit: int = 5


class ListValuesReq(BaseModel):
    dimension: str


def _cerrado(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from None
    except Exception:
        raise HTTPException(status_code=500, detail="error interno de consulta") from None


@app.get("/healthz")
def healthz():
    return {"ok": True}


@app.post("/aggregate")
def aggregate(req: AggregateReq):
    return _cerrado(tools.aggregate, req.measure, req.group_by, req.filters, req.top_n)


@app.post("/count")
def count(req: CountReq):
    return _cerrado(tools.count, req.filters)


@app.post("/lookup")
def lookup(req: LookupReq):
    return _cerrado(tools.lookup, req.text_query, req.filters, req.limit)


@app.post("/list_values")
def list_values(req: ListValuesReq):
    return _cerrado(tools.list_values, req.dimension)


@app.post("/resolve")
def resolve(texto: str, dimension: str | None = None):
    from src.query import resolve as res
    return res.resolve(texto, dimension)
