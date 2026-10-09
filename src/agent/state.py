"""Manejo de estado de conversación en memoria.

La arquitectura requiere 5 slots con TTL de 3 turnos:
  1. measure (medida)
  2. dimensión activa
  3. filtros
  4. última tool
  5. desambiguación pendiente

El estado vive en memoria por sesión (conexión). Si pasan 3 turnos sin renovarlo o 
se cambia de tema bruscamente, se limpia.
"""
from __future__ import annotations

import time

TTL_TURNS = 3


class ConversationState:
    def __init__(self):
        self.measure: str | None = None
        self.active_dimension: str | None = None
        self.filters: dict[str, str | int | float] = {}
        self.last_tool: str | None = None
        self.pending_disambiguation: dict | None = None
        
        self.turns_left: int = TTL_TURNS
        self.last_accessed: float = time.time()
        self.history: list[dict] = []  # Ventana deslizante de 6 mensajes

    def add_message(self, role: str, content: str):
        """Añade un mensaje a la ventana deslizante (máx 6)."""
        self.history.append({"role": role, "content": content})
        if len(self.history) > 6:
            self.history = self.history[-6:]
            
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

    def set_disambiguation(self, original_text: str, candidates: list[str]):
        """Guarda el estado pendiente cuando un término es ambiguo."""
        self.pending_disambiguation = {
            "text": original_text,
            "candidates": candidates
        }
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
        self.pending_disambiguation = None
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
