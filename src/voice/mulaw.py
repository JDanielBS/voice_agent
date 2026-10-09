"""Decodificación G.711 mu-law a PCM 16-bit.

Como audioop se eliminó en Python 3.13+, usamos una tabla lookup estática
generada en tiempo de ejecución.
"""
from __future__ import annotations

import struct


def _build_mulaw_decoding_table() -> list[int]:
    """Genera la tabla de decodificación mu-law según la especificación G.711."""
    table = []
    for byte in range(256):
        # Invertir bits
        u_val = ~byte & 0xFF
        sign = u_val & 0x80
        exponent = (u_val >> 4) & 0x07
        mantissa = u_val & 0x0F
        
        sample = (mantissa << 3) + 132
        sample <<= exponent
        sample -= 132
        
        if sign:
            sample = -sample
        table.append(sample)
    return table


_MULAW_TABLE = _build_mulaw_decoding_table()


def mulaw_to_pcm(mulaw_bytes: bytes) -> bytes:
    """Convierte bytes G.711 mu-law a PCM 16-bit signed little-endian."""
    samples = [_MULAW_TABLE[b] for b in mulaw_bytes]
    return struct.pack(f"<{len(samples)}h", *samples)

