"""Manejo de estado de conversación en memoria.

La arquitectura requiere 5 slots con TTL de 5 turnos:
  1. measure (medida)
  2. dimensión activa
  3. filtros
  4. última tool
  5. desambiguación pendiente

El estado vive en memoria por sesión (conexión). Si pasan 5 turnos sin renovarlo o 
se cambia de tema bruscamente, se limpia.
"""
from __future__ import annotations

import time

TTL_TURNS = 5
HISTORY_MAX = 20

class ConversationState:
    def __init__(self):
        self.measure: str | None = None
        self.active_dimension: str | None = None
        self.filters: dict[str, str | int | float] = {}
        self.last_tool: str | None = None
        self.last_tool_args: dict | None = None
        self.last_response: str | None = None
        self.pending_disambiguation: dict | None = None
        self.pending_correction: dict | None = None
        
        self.turns_left: int = TTL_TURNS
        self.last_accessed: float = time.time()
        self.history: list[dict] = []  # Ventana deslizante de HISTORY_MAX mensajes

    def add_message(self, role: str, content: str):
        """Añade un mensaje a la ventana deslizante (máx HISTORY_MAX)."""
        self.history.append({"role": role, "content": content})
        if len(self.history) > HISTORY_MAX:
            self.history = self.history[-HISTORY_MAX:]
            
    def resumen_slots(self) -> str:
        """Devuelve un resumen de texto de los slots activos para contexto."""
        res = []
        if self.measure: res.append(f"medida: {self.measure}")
        if self.active_dimension: res.append(f"dimensión activa: {self.active_dimension}")
        if self.filters: res.append(f"filtros: {self.filters}")
        if self.last_tool: res.append(f"última tool: {self.last_tool}")
        return ", ".join(res) if res else "No hay contexto previo."

    def update(self, tool_name: str | None = None, filters: dict | None = None, 
               measure: str | None = None, active_dimension: str | None = None):
        if tool_name:
            self.last_tool = tool_name
        if filters:
            self.filters.update(filters)
        if measure:
            self.measure = measure
        if active_dimension:
            self.active_dimension = active_dimension
            
        self.turns_left = TTL_TURNS
        self.last_accessed = time.time()

    def set_disambiguation(self, original_text: str, candidates: list[str],
                           payload: dict | None = None):
        """Guarda el estado pendiente cuando un término es ambiguo.

        `payload` trae la tool, los args y la dimensión para poder ejecutar la
        consulta original una vez el usuario aclara (sin reinterpretar la frase).
        """
        self.pending_disambiguation = {
            "text": original_text,
            "candidates": candidates,
        }
        if payload:
            self.pending_disambiguation.update(payload)
        self.turns_left = TTL_TURNS
        self.last_accessed = time.time()

    def clear_disambiguation(self):
        self.pending_disambiguation = None

    def tick(self):
        """Avanza un turno. Si expira, se limpia."""
        self.turns_left -= 1
        if self.turns_left <= 0:
            self.clear()

    def clear(self):
        self.measure = None
        self.active_dimension = None
        self.filters = {}
        self.last_tool = None
        self.last_tool_args = None
        self.last_response = None
        self.pending_disambiguation = None
        self.pending_correction = None
        self.turns_left = TTL_TURNS
        self.history.clear()


class StateManager:
    def __init__(self):
        self.sessions: dict[str, ConversationState] = {}

    def get(self, session_id: str) -> ConversationState:
        if session_id not in self.sessions:
            self.sessions[session_id] = ConversationState()
        return self.sessions[session_id]

    def cleanup_idle(self, max_idle_seconds: int = 3600):
        """Limpia sesiones inactivas (para no fugar memoria)."""
        now = time.time()
        stale = [sid for sid, st in self.sessions.items() 
                 if now - st.last_accessed > max_idle_seconds]
        for sid in stale:
            del self.sessions[sid]

# Instancia global por defecto para el servidor
manager = StateManager()
