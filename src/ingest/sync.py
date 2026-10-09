"""API Socrata -> PostgreSQL. Idempotente; checkpoint vive en la propia base
(no en disco local) porque el contenedor de despliegue puede ser efímero."""
from __future__ import annotations

import hashlib
import json
import os

import psycopg

from src.ingest.client import SocrataClient, paginate

DDL = """
CREATE TABLE IF NOT EXISTS raw_records (
    row_id TEXT PRIMARY KEY,
    data JSONB NOT NULL,
    synced_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS sync_checkpoint (
    dataset_id TEXT PRIMARY KEY,
    next_offset INTEGER NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""

UPSERT_ROW = """
INSERT INTO raw_records (row_id, data, synced_at)
VALUES (%s, %s, now())
ON CONFLICT (row_id) DO UPDATE
SET data = excluded.data, synced_at = now()
"""


def row_id(row: dict) -> str:
    # ponytail: hash del contenido como PK porque este dataset no expone :id
    # (Socrata lo acepta en $order pero no en $select). Si el dato cambia
    # entre syncs la fila vieja queda huérfana -> se resuelve en Flujo E
    # (swap atómico de tabla completa), no aquí.
    canonical = json.dumps(row, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

UPSERT_CHECKPOINT = """
INSERT INTO sync_checkpoint (dataset_id, next_offset, updated_at)
VALUES (%s, %s, now())
ON CONFLICT (dataset_id) DO UPDATE
SET next_offset = excluded.next_offset, updated_at = now()
"""


def get_checkpoint(conn: psycopg.Connection, dataset_id: str) -> int:
    row = conn.execute(
        "SELECT next_offset FROM sync_checkpoint WHERE dataset_id = %s", (dataset_id,)
    ).fetchone()
    return row[0] if row else 0


def upsert_batch(conn: psycopg.Connection, rows: list[dict]) -> int:
    inserted = 0
    for row in rows:
        conn.execute(UPSERT_ROW, (row_id(row), json.dumps(row, ensure_ascii=False)))
        inserted += 1
    return inserted


def sync(dataset_id: str, app_token: str | None, database_url: str,
          page_size: int = 1000) -> int:
    client = SocrataClient(dataset_id, app_token)
    total = 0
    with psycopg.connect(database_url) as conn:
        conn.execute(DDL)
        conn.commit()

        start_offset = get_checkpoint(conn, dataset_id)
        offset = start_offset
        for row in paginate(client.fetch, page_size=page_size, start_offset=start_offset):
            total += upsert_batch(conn, [row])
            offset = start_offset + total
            if total % page_size == 0:
                # checkpoint dentro del loop: si se interrumpe aquí, la próxima corrida
                # reanuda desde la última página confirmada, no desde cero.
                conn.execute(UPSERT_CHECKPOINT, (dataset_id, offset))
                conn.commit()

        conn.execute(UPSERT_CHECKPOINT, (dataset_id, offset))
        conn.commit()
    return total


def main():
    from dotenv import load_dotenv
    load_dotenv()

    dataset_id = os.environ["DATOS_GOV_DATASET_ID"]
    app_token = os.environ.get("DATOS_GOV_APP_TOKEN") or None
    database_url = os.environ["DATABASE_URL"]

    total = sync(dataset_id, app_token, database_url)
    print(f"sync completo: {total} filas nuevas/actualizadas")


if __name__ == "__main__":
    main()
