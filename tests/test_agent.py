"""Prueba de regresión para el agente completo (Fase 4).

Ejecuta las 20 preguntas de oro en modo texto.
Requiere configuración de Azure OpenAI correcta en el .env.
"""
import sys
import yaml
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.agent.agent import process_turn
from src.agent.state import manager

GOLDEN_PATH = Path(__file__).resolve().parent / "golden.yaml"

def run_tests():
    try:
        with open(GOLDEN_PATH, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
    except Exception as e:
        print(f"Error cargando golden.yaml: {e}")
        return

    session_id = "test_session_golden"
    manager.get(session_id).clear() # Asegurar estado limpio inicial

    print("Iniciando evaluación de golden questions...\n")
    
    passed = 0
    total = len(data["preguntas"])
    
    for q in data["preguntas"]:
        print(f"[{q['id']}] {q['desc']}")
        print(f"Usuario: {q['input']}")
        try:
            # Procesar el turno
            resp = process_turn(session_id, q['input'])
            print(f"Agente : {resp}\n")
            passed += 1
        except Exception as e:
            print(f"ERROR: {e}\n")
            
        # Si es un turno que no es de seguimiento, limpiamos el estado
        if "seguimiento" not in q['desc'].lower():
            manager.get(session_id).clear()

    print(f"Completados {passed}/{total} test(s) de interacción en modo texto.")

if __name__ == "__main__":
    try:
        import yaml
    except ImportError:
        print("Instala pyyaml para ejecutar los tests (pip install pyyaml).")
        sys.exit(1)
        
    run_tests()
