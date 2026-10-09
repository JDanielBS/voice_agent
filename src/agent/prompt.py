"""Generador del prompt de sistema desde schema.json.

Inyecta las dimensiones, medidas y valores permitidos para que el LLM
pueda llamar a las tools sin inventar argumentos.
"""
from __future__ import annotations

import json
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parents[2] / "data"

BASE_PROMPT = """Eres un asistente de voz que responde preguntas sobre capacidad instalada de IPS en Colombia (base REPS).
Tu trabajo es interpretar la pregunta del usuario, y llamar a una de las tools proporcionadas para obtener los datos.
NUNCA inventes números. NUNCA asumas valores que no vengan de la tool. Si la tool devuelve 0, dices que no hay.

Reglas:
1. Solo puedes usar las columnas indicadas abajo para los filtros, medidas (measure) y agrupaciones (group_by).
2. Si un usuario te pide algo ambiguo o irreconocible, no intentes adivinar. Llama a la herramienta correspondiente y el sistema lo manejará.
3. Mantén tus respuestas conversacionales breves (1 o 2 frases).

Columnas disponibles en la base de datos:
{schema_info}

Ejemplos de interacción (solo llama a las tools, no expliques):
Usuario: "¿Cuántas camas hay en el Chocó?"
Llamada: aggregate(measure="num_cantidad_capacidad_instalada", filters={{"departamento": "Chocó", "nom_grupo_capacidad": "CAMAS"}}, group_by=[])

Usuario: "¿Cuántas IPS públicas hay en total?"
Llamada: count(filters={{"naturaleza": "Pública"}})

Usuario: "Busca el hospital san rafael de leticia"
Llamada: lookup(text_query="hospital san rafael", filters={{"municipio": "Leticia"}})

Usuario: "¿Qué tipos de capacidad hay?"
Llamada: list_values(dimension="nom_grupo_capacidad")
"""

def generate_prompt(schema_path: Path | None = None) -> str:
    path = schema_path or DATA_DIR / "schema.json"
    with open(path, "r", encoding="utf-8") as f:
        schema = json.load(f)
        
    lines = []
    for col in schema["columns"]:
        role = col["role"]
        if role in ("constant", "identifier"):
            continue
        
        name = col["name"]
        if role == "closed_dimension":
            vals = ", ".join(f"'{v}'" for v in col.get("values", []))
            lines.append(f"- Dimensión cerrada '{name}': Valores permitidos: [{vals}]")
        elif role == "open_dimension":
            lines.append(f"- Dimensión abierta '{name}': Muchos valores (ej. municipios).")
        elif role == "measure":
            lines.append(f"- Medida '{name}': Usar para sumar (ej. capacidad).")

    schema_info = "\n".join(lines)
    prompt = BASE_PROMPT.format(schema_info=schema_info)
    
    # Escribir a prompt.txt (como indica la arquitectura)
    out_path = DATA_DIR / "prompt.txt"
    out_path.write_text(prompt, encoding="utf-8")
    
    return prompt

if __name__ == "__main__":
    p = generate_prompt()
    print("Prompt generado:")
    print(p[:500] + "...\n(Ver data/prompt.txt para el prompt completo)")
