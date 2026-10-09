# CLAUDE.md

Instrucciones para cualquier modelo que trabaje en este repositorio.

**Lee `ARQUITECTURA.md` antes de escribir código.** Este archivo son las reglas operativas; `ARQUITECTURA.md` es el diseño completo con los cinco flujos. No dupliques su contenido aquí ni lo contradigas: si algo debe cambiar, edita `ARQUITECTURA.md` en el mismo commit.

---

## Qué se está construyendo

Un agente de voz que responde preguntas sobre una base de datos estructurada que se consume por una API REST paginada.

El usuario habla, el sistema interpreta la intención, consulta datos locales y responde por voz con cifras reales.

### Restricciones que definen el diseño

| Restricción | Consecuencia |
|---|---|
| La API devuelve máximo 1000 registros por petición | Se sincroniza todo una vez (42 peticiones); no se pagina al responder |
| El canal es voz: presupuesto de 1,5–2,5 s por turno | Un solo salto al LLM en el camino crítico |
| Dataset de referencia: 41.427 filas × 20 columnas (~17 MB) | Cabe entero en SQLite local; consultas en menos de 10 ms |
| La solución no debe quedar atada a este dataset | El esquema se descubre por perfilado automático, no se codifica a mano |

### Dataset de referencia

`db.csv` — REPS, capacidad instalada de IPS en Colombia. Está en el repositorio solo como referencia para desarrollo y pruebas. **No escribas lógica que dependa de sus nombres de columna.** Todo acceso a columnas pasa por `schema.json`.

Columnas y roles detectados (ver `ARQUITECTURA.md` §4.2 para la tabla completa):

- dimensiones cerradas: `naturaleza` (3), `nom_grupo_capacidad` (7), `departamento` (38), `nom_descripcion_capacidad` (59)
- dimensión abierta: `municipio` (1027)
- medida: `num_cantidad_capacidad_instalada`
- identificadores de texto: `nombre_prestador`, `nom_sede_ips`, `direccion`
- constantes a descartar: `fecha_corte`, `fuente`

---

## Reglas no negociables

Estas reglas existen porque romperlas produce fallos que se ven en la demostración. No las relajes sin pedir confirmación explícita.

1. **Ningún número que no venga de una tool.** El LLM redacta, no calcula ni recuerda cifras. Si la tool no devolvió resultado, la respuesta es "no tengo ese dato".

2. **El LLM no genera SQL.** Elige una tool y rellena argumentos tipados; el código construye el `SELECT` con parámetros. Un argumento que no exista en `schema.json` se rechaza antes de tocar la base.

3. **Un solo salto al LLM en el camino crítico.** El agente de voz y el intérprete de intención son el mismo modelo con tool-calling. No encadenes dos modelos.

4. **Conexión a la base en read-only.** `PRAGMA query_only = ON`. El runtime nunca escribe.

5. **Máximo 5 resultados por respuesta.** Por voz, más no se puede escuchar. Si hay más, devuelve el conteo total más los 3 primeros.

6. **Entidad ambigua o no resuelta: preguntar, nunca adivinar.** Un número correcto sobre la entidad equivocada es peor que no responder.

7. **Todo fallo produce respuesta hablada.** En un canal de voz, el silencio se lee como caída del sistema.

8. **Nunca escribir sobre la base que está sirviendo.** El refresco construye `db.sqlite.new`, valida y hace swap atómico.

---

## Estructura de archivos

```
src/
  ingest/
    client.py        # cliente paginado genérico (offset | page | cursor)
    sync.py          # API -> SQLite, con checkpoint, idempotente
  schema/
    infer.py         # perfilado por cardinalidad -> schema.json
    vocab.py         # valor canónico + forma normalizada + alias
  query/
    tools.py         # las 4 tools tipadas
    resolve.py       # texto hablado -> valor canónico
    render.py        # resultado -> texto apto para oído
  agent/
    prompt.py        # prompt de sistema generado desde schema.json
    state.py         # 5 slots, TTL 3 turnos
    voice.py         # STT -> agente -> TTS
data/
  db.sqlite          # generado, no se versiona
  schema.json        # generado, no se versiona
  prompt.txt         # generado, no se versiona
tests/
  golden.yaml        # banco de preguntas de oro
  test_smoke.py
db.csv               # dataset de referencia
ARQUITECTURA.md
CLAUDE.md
```

Todo lo que está en `data/` es generado y regenerable con un comando. No lo edites a mano ni lo versiones.

---

## Las cuatro tools

Firmas fijas. Se generan desde `schema.json`, pero su forma no cambia:

```
aggregate(measure, group_by[], filters{}, top_n<=5)
count(filters{})
lookup(text_query, filters{}, limit<=5)
list_values(dimension)
```

Si una pregunta no se puede responder con estas cuatro, **no inventes una quinta sin discutirlo**. Primero verifica que no sea un problema de argumentos.

---

## Orden de trabajo

No saltes fases. Cada una tiene criterio de salida verificable.

| Fase | Entregable | Criterio de salida |
|---|---|---|
| 1 | `client.py` + `sync.py` | `db.sqlite` con 41.427 filas; el sync se reanuda tras interrupción |
| 2 | `infer.py` | `schema.json` con el rol correcto para las 20 columnas |
| 3 | `tools.py` + `resolve.py` | las 4 tools responden correctamente **por texto, sin voz** |
| 4 | `prompt.py` + agente | las 20 preguntas de oro aprueban **en modo texto** |
| 5 | `voice.py` | turno completo en menos de 2,5 s |
| 6 | optimización | plantillas deterministas, conexiones precalentadas |

**La fase 4 se valida sin voz.** Depurar un error de consulta a través de audio cuesta diez veces más. La voz se añade cuando la lógica ya es correcta.

---

## Convenciones de código

- Python 3.11 o superior. Español en nombres de dominio (`departamento`, `capacidad`), inglés en nombres técnicos (`fetch`, `paginate`, `resolve`).
- Biblioteca estándar primero. `sqlite3`, `difflib`, `unicodedata`, `pathlib` cubren casi todo lo necesario. No añadas una dependencia por algo que resuelvan diez líneas.
- Dependencias justificadas hasta ahora: `pandas` solo para el perfilado offline (fase 2), un SDK de LLM, y los servicios de STT/TTS. Nada más sin preguntar.
- Sin abstracciones no pedidas: ninguna interfaz con una sola implementación, ninguna factory para un solo producto, ninguna configuración para un valor que nunca cambia.
- Comentarios solo cuando el *por qué* no sea obvio. No describas lo que el código ya dice.
- Las simplificaciones deliberadas con techo conocido se marcan con un comentario `ponytail:` que nombre el techo y la ruta de mejora.

### Verificación

Cada pieza con lógica no trivial deja **una** comprobación ejecutable: un `demo()` con `assert` o un `test_*.py` pequeño. Sin frameworks, sin fixtures, sin una suite por función. Las funciones de una línea no necesitan prueba.

El banco `tests/golden.yaml` es la suite de regresión y también el criterio de aceptación de cada refresco de datos. Debe cubrir, como mínimo:

- agregado simple por una dimensión
- agregado con dos filtros
- conteo
- búsqueda por nombre propio
- entidad ambigua (*Cali* existe como departamento y como municipio en este dataset)
- entidad con error fonético de STT
- resultado vacío
- resultado con muchas filas
- pregunta fuera de dominio
- pregunta de seguimiento que depende del turno anterior

No declares una fase terminada sin ejecutar su verificación y mostrar la salida.

---

## Trampas conocidas de este dataset

Están documentadas porque ya costaron análisis. No las redescubras.

- **`departamento` mezcla distritos con departamentos.** Contiene *Cali*, *Barranquilla* y *Bogotá D.C* junto a *Antioquia* o *Nariño*. La desambiguación no es opcional.
- **Un mismo prestador aparece en muchas filas**, una por tipo de capacidad. Contar filas no es contar IPS. Para contar entidades, usa `COUNT(DISTINCT codigo_sede)`.
- **`fecha_corte` es constante**: los datos son un snapshot, no un flujo. El refresco va por cron, nunca por petición.
- **El CSV necesita fallback de encoding**: intenta `utf-8-sig` y luego `latin1`.
- **`num_nivel_atencion` y `num_digito_verificion` tienen nulos.** Son `float64` por eso, no porque sean medidas.

---

## Lo que no se construye todavía

No añadas nada de esta lista sin que se cumpla su gatillo:

| Omitido | Añadir cuando |
|---|---|
| Encoding entero de categóricos | el dataset pase de unos 5 millones de filas |
| SQL generado por LLM | las preguntas excedan lo que cubren las 4 tools |
| Segundo modelo enrutador | haya varias fuentes de datos heterogéneas |
| Postgres o DuckDB | SQLite deje de responder en menos de 50 ms |
| Memoria de conversación persistente | se requiera continuidad entre sesiones |
| Agregados precomputados | la agregación en vivo supere los 100 ms |
| Autenticación, multiusuario, panel de administración | alguien lo pida |

---

## Datos que faltan por confirmar

Pregunta antes de asumir:

- URL y contrato real de la API: ¿`offset`/`limit`, `page`/`per_page` o cursor? ¿Soporta filtros o agregación del lado del servidor?
- ¿Requiere autenticación? ¿Hay límite de tasa?
- Proveedores de STT y TTS elegidos, y si admiten streaming.
- Modelo de lenguaje disponible y si soporta tool-calling nativo.
- Si la evaluación del reto es a nivel departamental o municipal (define si el resolver difuso de los 1027 municipios es necesario).
