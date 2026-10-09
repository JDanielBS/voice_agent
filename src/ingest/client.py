"""Cliente paginado genérico. REPS vive en datos.gov.co (Socrata/SODA3)."""
from __future__ import annotations

import time
from collections.abc import Callable, Iterator

import requests

FetchFn = Callable[[int, int], list[dict]]


class SocrataClient:
    """Adaptador de 3 líneas para el contrato offset/limit de Socrata ($limit/$offset)."""

    def __init__(self, dataset_id: str, app_token: str | None = None,
                 base_url: str = "https://www.datos.gov.co/resource"):
        self.dataset_id = dataset_id
        self.app_token = app_token
        self.base_url = base_url

    def fetch(self, offset: int, limit: int) -> list[dict]:
        headers = {"X-App-Token": self.app_token} if self.app_token else {}
        params = {"$limit": limit, "$offset": offset, "$order": ":id"}
        url = f"{self.base_url}/{self.dataset_id}.json"
        last_exc = None
        for attempt in range(5):
            try:
                resp = requests.get(url, headers=headers, params=params, timeout=30)
            except requests.RequestException as exc:
                last_exc = exc
                time.sleep(2 ** attempt)
                continue
            if resp.status_code == 429 or resp.status_code >= 500:
                time.sleep(2 ** attempt)
                continue
            resp.raise_for_status()
            return resp.json()
        raise RuntimeError(f"Socrata fetch falló tras 5 intentos: {last_exc}")


def paginate(fetch: FetchFn, page_size: int = 1000, start_offset: int = 0,
             max_pages: int = 10_000) -> Iterator[dict]:
    """fetch(offset, limit) -> list[dict]. start_offset permite reanudar un sync interrumpido."""
    offset = start_offset
    for _ in range(max_pages):
        batch = fetch(offset, page_size)
        if not batch:
            return
        yield from batch
        if len(batch) < page_size:   # última página
            return
        offset += len(batch)


def demo():
    # fetch simulado: 3 páginas de 2, última a medias -> fin de datos
    pages = [[{"a": 1}, {"a": 2}], [{"a": 3}, {"a": 4}], [{"a": 5}]]
    calls = []

    def fake_fetch(offset, limit):
        calls.append(offset)
        idx = offset // 2
        return pages[idx] if idx < len(pages) else []

    rows = list(paginate(fake_fetch, page_size=2))
    assert len(rows) == 5, rows
    assert calls == [0, 2, 4], calls

    # reanudar desde offset 2 -> se salta la primera página
    rows_resumed = list(paginate(fake_fetch, page_size=2, start_offset=2))
    assert len(rows_resumed) == 3, rows_resumed
    print("ok: paginate termina en última página corta y respeta start_offset")


if __name__ == "__main__":
    demo()
