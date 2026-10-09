"""Configuración del logging para el servicio desplegado.

Un handler al stdout (Render lo agrega a los logs del servicio) con timestamp
y nivel. El nivel se controla con LOG_LEVEL (default INFO: eventos por turno;
DEBUG añade por-frame del VAD). No toca los loggers propios de uvicorn.
"""
from __future__ import annotations

import logging
import os
import sys


def setup_logging() -> None:
    level = os.environ.get("LOG_LEVEL", "INFO").upper()
    root = logging.getLogger()
    if not root.handlers:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(logging.Formatter(
            "%(asctime)s %(levelname)-7s %(name)s: %(message)s",
            datefmt="%Y-%m-%dT%H:%M:%S",
        ))
        root.addHandler(handler)
    root.setLevel(level)