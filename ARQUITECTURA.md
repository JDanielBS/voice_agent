# Agente de voz sobre base de datos vía API paginada

Diseño de referencia. Las recomendaciones de la revisión ya están aplicadas: un solo modelo de lenguaje en el camino crítico, consultas mediante tools tipadas (no SQL generado libremente), e índices en lugar de encoding de categóricos.

---

## 1. Contexto y restricciones

| Elemento | Valor |
|---|---|
| Fuente | API REST que expone una base de datos estructurada |
| Límite de la API | 1000 registros por petición |
| Dataset de referencia | REPS — capacidad instalada de IPS (Colombia) |
| Tamaño | 41.427 filas × 20 columnas (~17 MB CSV) |
| Peticiones para sincronizar todo | 42 |
| Canal de salida | Voz (presupuesto de latencia: 1,5–2,5 s por turno) |

El diseño es agnóstico al dataset. La estructura de la base se descubre por perfilado automático, no se codifica a mano.

---

## 2. Decisión central: sincronizar, no paginar en vivo

El dataset completo cabe en 42 peticiones. Un agente de voz tiene un presupuesto de latencia de 1,5–2,5 s por turno. Paginar en el momento de responder es inviable.

**La paginación es un problema de ingesta, no de consulta.** Se sincroniza una vez a un almacén local y las consultas se resuelven ahí, en menos de 10 ms.

Consecuencia práctica: el límite de 1000 registros deja de ser una restricción del producto. Nadie pide 1000 registros por voz; pide agregados ("¿cuántas camas de UCI hay en Nariño?").

Si el dataset creciera a millones de filas, la decisión cambia: se sincronizan agregados precomputados en lugar de filas crudas, o se delega la agregación a la API cuando la soporte.

---

## 3. Arquitectura

```
┌─────────────────────────────────────────────────────────┐
│  MONTAJE (offline, idempotente, un comando)             │
│                                                          │
│  API paginada ──> sync ──> SQLite (raw)                 │
│                              │                           │
│                              ├─> perfilado ─> schema.json│
│                              ├─> índices                 │
│                              ├─> FTS5                    │
│                              ├─> vocabulario             │
│                              ├─> catálogo de tools        │
│                              └─> prompt de sistema        │
└─────────────────────────────────────────────────────────┘
                              │
┌─────────────────────────────────────────────────────────┐
│  RUNTIME (online, por turno de conversación)             │
│                                                          │
│  STT ──> LLM (tool-calling) ──> resolver ──> SQL         │
│                                                │         │
│  voz <── TTS <── plantilla/LLM <── post-proceso          │
│                                                          │
│  estado: 5 slots, TTL 3 turnos                          │
└─────────────────────────────────────────────────────────┘
```

### Estructura de archivos

```
src/
  ingest/
    client.py        # cliente paginado genérico (offset | page | cursor)
    sync.py          # API -> SQLite, con checkpoint, idempotente
  schema/
    infer.py         # perfilado por cardinalidad -> schema.json
    schema.json      # generado: dimensiones / medidas / identificadores
    vocab.py         # valor real + forma normalizada + alias
  query/
    tools.py         # 4 tools tipadas que ve el LLM
    resolve.py       # texto hablado -> valor canónico de la base
    render.py        # resultado -> texto apto para oído
  agent/
    prompt.py        # prompt de sistema generado desde schema.json
    state.py         # slots de conversación
    voice.py         # STT -> agente -> TTS
data/
  db.sqlite
  prompt.txt
tests/
  golden.yaml        # 20 preguntas de oro
  test_smoke.py
```

Tres artefactos son la salida del montaje: `db.sqlite`, `schema.json`, `prompt.txt`. Todos regenerables con un comando.

---

## 4. Flujo A — Montaje del servicio (una vez)

```
API paginada
  -> descubrir contrato de paginación (offset/limit | page/per_page | cursor)
  -> sync completo: 42 peticiones x 1000 registros, con checkpoint de offset
  -> volcado crudo a SQLite (tabla raw, sin transformar)
  -> perfilado de esquema (regla de cardinalidad)
       -> clasifica cada columna:
          constante | dimensión cerrada | dimensión abierta | medida | identificador
  -> emite schema.json
  -> construye artefactos derivados:
       a) índices SQLite en las dimensiones
       b) tabla FTS5 en los identificadores
       c) vocabulario: valor real + forma normalizada + alias
       d) catálogo de tools (firmas generadas desde schema.json)
       e) prompt de sistema (dimensiones, medidas, valores de las cerradas, 6 ejemplos)
  -> validación: 20 preguntas de oro, ejecutadas sin voz
  -> servicio arriba
```

### 4.1 Cliente paginado genérico

```python
def paginate(fetch, page_size=1000, max_pages=10_000):
    """fetch(offset, limit) -> list[dict]. Sirve para cualquier API con offset/limit."""
    offset = 0
    for _ in range(max_pages):
        batch = fetch(offset, page_size)
        if not batch:
            return
        yield from batch
        if len(batch) < page_size:   # última página
            return
        offset += len(batch)
```

Tres contratos de paginación se cubren con un adaptador de tres líneas cada uno:

| Contrato | Adaptador |
|---|---|
| `offset` / `limit` | directo |
| `page` / `per_page` | `page = offset // page_size + 1` |
| cursor / `next_token` | guardar el token en vez del offset |

Si la fuente es Socrata (el caso de datos.gov.co, donde vive REPS), los parámetros son `$limit` y `$offset`, y además soporta `$where`, `$group` y `$select`. En ese caso el sync puede traer agregados directamente y el volumen baja un orden de magnitud.

Reglas de ingesta:

- Reintento con backoff exponencial en 429 y 5xx.
- Checkpoint del offset en disco para reanudar una sincronización interrumpida.
- Fin de datos detectado por `len(batch) < page_size`, nunca por un contador total declarado por la API.
- Validación del tipo de cada campo al insertar; una fila malformada se registra y se salta, no aborta el sync.

### 4.2 Perfilado automático de esquema

Esta es la pieza que hace el diseño reutilizable. Clasifica cada columna por cardinalidad y asigna un rol:

| Veredicto | Regla | Rol | Uso en el agente |
|---|---|---|---|
| CONSTANTE | 1 valor único | descartar | ninguno |
| CATEGÓRICA cerrada | ≤ 100 únicos | dimensión | filtro y `GROUP BY`; sus valores van al prompt |
| CATEGÓRICA abierta | únicos ≤ 5% de las filas | dimensión jerárquica | filtro con resolución difusa |
| Numérica no identificadora | — | medida | `SUM`, `AVG`, `COUNT` |
| Texto libre, alta cardinalidad | — | identificador | búsqueda FTS; nunca `GROUP BY` |

Aplicado al dataset de referencia:

| Columna | Únicos | Rol |
|---|---|---|
| `fecha_corte`, `fuente` | 1 | descartar |
| `naturaleza` | 3 | dimensión cerrada |
| `nom_grupo_capacidad` | 7 | dimensión cerrada |
| `departamento` | 38 | dimensión cerrada |
| `nom_descripcion_capacidad` | 59 | dimensión cerrada |
| `municipio` | 1027 | dimensión abierta |
| `num_cantidad_capacidad_instalada` | — | medida |
| `num_nivel_atencion` | — | dimensión cerrada (numérica) |
| `nombre_prestador`, `nom_sede_ips`, `direccion` | miles | identificador (FTS) |
| `codigo_prestador`, `codigo_sede`, `nit_ips` | miles | identificador (lookup exacto) |
| `gerente`, `email`, `telefono` | miles | identificador (no consultable por voz) |

El resultado se escribe en `schema.json`, y de ahí se generan tanto el catálogo de tools como el prompt de sistema. Al apuntar a otra API, se vuelve a correr el perfilado y no se toca código.

### 4.3 Almacén

SQLite mediante el módulo `sqlite3` de la biblioteca estándar. Sin dependencias adicionales.

- 17 MB de CSV producen alrededor de 12 MB de base.
- Índices en todas las dimensiones.
- Tabla FTS5 sobre los identificadores de texto.
- Consultas resueltas en menos de 10 ms.

**Sobre encodear los categóricos a entero:** no aporta en este rango de tamaño. Con un índice, la consulta ya está en el orden de los milisegundos; el encoding ahorra microsegundos a cambio de mantener un mapa de decodificación en cada respuesta y abrir la puerta a errores del tipo "el agente dijo 17 en lugar de Nariño". SQLite y Parquet ya aplican *dictionary encoding* internamente.

El trabajo útil sobre los categóricos es otro:

1. **Vocabulario** de valores válidos, que se inyecta en el prompt de sistema (las cuatro dimensiones cerradas suman 107 valores: caben de sobra).
2. **Vocabulario** para el resolver difuso y para sesgar el reconocimiento del STT, de modo que transcriba "Chocó" y no "choco".
3. **Índices**, que son la herramienta real de velocidad.

Reconsiderar el encoding a partir de unos 5 millones de filas, donde deja de ser cosmético.

### 4.4 Artefactos generados

| Artefacto | Contenido | Se usa en |
|---|---|---|
| `schema.json` | columnas, roles, valores de las cerradas | generación de tools y prompt |
| índices | dimensiones | ejecución de consultas |
| FTS5 | identificadores de texto | `lookup` |
| vocabulario | valor canónico, forma normalizada, alias | `resolve.py`, sesgo del STT |
| catálogo de tools | firmas tipadas | tool-calling del LLM |
| `prompt.txt` | dimensiones, medidas, valores, ejemplos | prompt de sistema |

---

## 5. Flujo B — Uso, camino feliz

```
usuario habla
  -> STT (streaming, con vocabulario sesgado hacia el dominio)      ~300 ms
  -> LLM con tool-calling: intención + argumentos tipados           ~700 ms
       salida: aggregate(measure="capacidad",
                         group_by=["municipio"],
                         filters={"departamento": "Nariño",
                                  "nom_grupo_capacidad": "CAMAS"})
  -> resolver de entidades: "narinio" -> "Nariño"                     ~5 ms
  -> ejecución: SQL parametrizado desde la tool, conexión read-only  ~10 ms
  -> post-proceso para oído: top 3, redondeo, unidades                ~1 ms
  -> redacción:
       forma conocida -> plantilla determinista                        ~0 ms
       forma rara     -> segunda pasada del LLM                      ~500 ms
  -> TTS (streaming, primer byte)                                    ~250 ms
  -> usuario escucha
```

Presupuesto total: **~1,3 s** por la vía de plantilla, **~1,8 s** con segunda pasada del LLM. Por encima de 2,5 s la conversación se percibe como averiada.

### 5.1 Un solo modelo en el camino crítico

Un diseño con "agente de voz que extrae intención" más "modelo de interpretación que arma la consulta" encadena dos llamadas al LLM antes de tocar los datos. A unos 700 ms cada una, son 1,4 s gastados solo en interpretar.

El agente de voz y el intérprete son **el mismo modelo** con tool-calling. Una sola pasada: escucha la transcripción y emite la llamada a la tool. STT y TTS son servicios, no agentes con criterio propio.

Un segundo modelo enrutador se justifica cuando hay varias fuentes de datos heterogéneas que requieren decidir a cuál ir. Con una sola base, no.

### 5.2 Tools tipadas, no SQL generado

SQL producido libremente por un LLM sobre un canal de voz tiene tres problemas: inventa columnas, no es auditable cuando falla, y una sentencia destructiva o un `JOIN` cartesiano puede tumbar el servicio.

El LLM elige la tool y rellena los argumentos; **el código construye el SQL**. Si una medida o dimensión no existe en `schema.json`, la llamada se rechaza antes de tocar la base.

```
aggregate(measure, group_by[], filters{}, top_n<=5)
    "¿cuántas camas hay por municipio en Nariño?"

count(filters{})
    "¿cuántas IPS públicas hay en Chocó?"

lookup(text_query, filters{}, limit<=5)
    "el hospital San Rafael de Leticia"

list_values(dimension)
    "¿qué tipos de capacidad existen?"
```

El tope de 5 resultados es duro: por voz, más de cinco elementos no se pueden escuchar. Si hay más, la tool devuelve el conteo total más los tres primeros y pide desambiguar.

Si de todos modos se quiere SQL generado, el mínimo defensivo es: conexión read-only, `PRAGMA query_only = ON`, timeout de 500 ms, lista blanca de tablas y validación del AST con `sqlglot` antes de ejecutar. Son cuatro capas para obtener una flexibilidad que las tools ya cubren.

**Selector de desarrollo.** Para comparar respuestas existe `QUERY_MODE` (default `tools`, sin cambio de comportamiento):

- `tools`: el LLM elige entre las 4 tools tipadas; el código construye el SELECT (lo descrito arriba, regla 2 vigente).
- `gpt`: se expone solo `consulta_sql` y el LLM redacta el SELECT de todas las consultas. Rompe la regla 2 a propósito; es un modo de experimentación que conserva las capas defensivas de `run_sql` (un solo SELECT, sin verbos de escritura) y el rol read-only, pero **no debe usarse como camino de producción**. Ver `src/agent/agent.py`.

### 5.3 Resolución de entidades habladas

El STT entrega texto sin tildes y con errores fonéticos. El resolver convierte lo transcrito en el valor canónico de la base:

1. Normalizar: `NFKD` + minúsculas + quitar diacríticos (la misma función del notebook).
2. Coincidencia exacta contra el vocabulario normalizado.
3. Si falla, `difflib.get_close_matches` de la biblioteca estándar, con umbral 0.8.
4. Si hay empate o nada supera el umbral, no se consulta: se devuelve una petición de aclaración.

Nunca adivinar en silencio. Un número correcto sobre la entidad equivocada es peor que no responder.

### 5.4 Redacción para el oído

- Los números se verbalizan: `4647` → "cuatro mil seiscientos cuarenta y siete".
- Se redondea cuando la precisión no aporta: "unas cuatro mil seiscientas camas".
- Respuestas de una o dos frases. El dato primero, el contexto después.
- Nunca listas largas; máximo tres elementos enunciados.

El prompt de redacción es una plantilla determinista, no texto libre:

```
Pregunta: {pregunta}
Consulta ejecutada: {descripción legible de los filtros aplicados}
Resultado: {filas, máximo 5}
Instrucción: responde en 1 o 2 frases. Usa únicamente estos números.
             Si el resultado está vacío, dilo.
```

Para las formas frecuentes (un agregado, un conteo, una búsqueda) se omite el LLM por completo y se usa una plantilla de texto directa. Ahorra unos 500 ms y cerca del 60% del costo por turno. El LLM redacta solo los casos atípicos.

### 5.5 Barrera anti-invención

**El modelo solo pronuncia números que provienen de una tool.** Si la tool no devolvió resultado, no hay número que decir. Es el guardarraíl que determina si el sistema es demostrable o no.

---

## 6. Flujo C — Desambiguación

```
usuario: "camas en Cali"
  -> LLM -> filters = {"?": "Cali"}
  -> resolver: "Cali" existe como departamento Y como municipio
  -> no se consulta; se devuelve una necesidad de aclaración
  -> plantilla: "¿Cali el municipio, o el departamento?"
  -> TTS -> usuario responde "municipio"
  -> se completa el slot pendiente (sin reinterpretar la frase entera)
  -> continúa el Flujo B desde la ejecución
```

Esta rama es obligatoria en el dataset de referencia: la columna `departamento` contiene *Cali*, *Barranquilla* y *Bogotá D.C*, es decir, mezcla distritos con departamentos. Sin desambiguación, el agente responde con total confianza un número equivocado.

El conflicto se detecta en el montaje, no en runtime: al construir el vocabulario se marcan los valores que aparecen en más de una dimensión.

---

## 7. Flujo D — Vacío, exceso y error

| Situación | Respuesta |
|---|---|
| 0 filas | "No encontré X en Y" + sugerir el valor más cercano de esa dimensión |
| más de 5 filas | conteo total + los 3 primeros + "¿quieres el resto?" |
| el resolver falla | enunciar los 2 candidatos más cercanos y preguntar |
| tool inválida o timeout | "No pude consultar eso" + registro en log. Nunca un número inventado |
| pregunta fuera de dominio | "Solo puedo responder sobre capacidad instalada de IPS" |

Todo fallo produce una respuesta hablada. El silencio, en un canal de voz, se interpreta como que el sistema se cayó.

---

## 8. Flujo E — Refresco de datos

```
cron (diario o semanal)
  -> sync a db.sqlite.new
  -> correr las 20 preguntas de oro contra la base nueva
  -> si pasan  -> swap atómico (rename)
  -> si fallan -> conservar la base vigente y alertar
```

Nunca se escribe sobre la base que está sirviendo.

En el dataset de referencia, `fecha_corte` es una columna constante: los datos son un snapshot, no un flujo. Eso confirma que refrescar por programación es correcto y que refrescar por petición sería desperdicio.

---

## 9. Estado de conversación

Por voz la gente dice "¿y en Chocó?" esperando que se arrastre el resto del contexto. Sin estado, cada pregunta arranca de cero y el usuario lo nota de inmediato.

Un diccionario de 5 slots (`measure`, `dimensión activa`, `filtros`, `última tool`, `desambiguación pendiente`) con TTL de 3 turnos es suficiente. Al cambiar de tema, se limpia.

No hace falta memoria de largo plazo ni almacén de sesiones. El estado vive en memoria por conexión.

---

## 10. Flujo consolidado

```
voz
  -> STT (vocabulario sesgado)
  -> un LLM: intención + tool tipada
  -> resolver de entidades
  -> ¿ambiguo? -> preguntar y volver
  -> SQL parametrizado, read-only
  -> post-proceso para oído
  -> plantilla determinista (o LLM si la forma es atípica)
  -> TTS
  -> voz

estado: 5 slots, TTL 3 turnos
regla: ningún número que no venga de una tool
```

---

## 11. Plan de implementación

| Fase | Entregable | Criterio de salida |
|---|---|---|
| 1 | `client.py` + `sync.py` | `db.sqlite` con 41.427 filas, sync reanudable |
| 2 | `infer.py` | `schema.json` con roles correctos para las 20 columnas |
| 3 | `tools.py` + `resolve.py` | las 4 tools responden por texto, sin voz |
| 4 | `prompt.py` + agente | 20 preguntas de oro aprobadas en modo texto |
| 5 | `voice.py` | STT y TTS encadenados, turno completo en menos de 2,5 s |
| 6 | optimización | plantillas deterministas, conexiones precalentadas, caché del último turno |

El orden importa: la fase 4 se valida **sin voz**. La voz se añade cuando la lógica ya es correcta, porque depurar un error de consulta a través de audio cuesta diez veces más.

### Preguntas de oro (banco mínimo)

Fijar unas 20 antes de escribir el agente. Deben cubrir:

- agregado simple por una dimensión
- agregado con dos filtros
- conteo
- búsqueda por nombre propio
- entidad ambigua (*Cali*)
- entidad con error fonético de STT
- resultado vacío
- resultado con muchas filas
- pregunta fuera de dominio
- pregunta de seguimiento que depende del turno anterior

Este banco es la suite de regresión y también el criterio de aceptación del refresco de datos.

---

## 12. Escalabilidad

| Eje | Veredicto | Nota |
|---|---|---|
| Volumen de datos | Bien | SQLite aguanta millones; migrar a DuckDB o Postgres sin tocar las tools |
| Usuarios concurrentes | Bien | capa de consulta sin estado; SQLite read-only admite lectores en paralelo |
| Nuevas fuentes o APIs | Bien, condicionado | depende de que el perfilado sea automático; codificar columnas a mano rompe esta propiedad |
| Latencia | **Cuello de botella** | cada salto al LLM es irreductible; reducir saltos es la única palanca real |
| Costo por turno | Lineal en saltos | dos modelos duplican la factura por pregunta |
| Complejidad de preguntas | Techo real | las tools cubren filtro, agregado y búsqueda; comparaciones de tendencia requieren tools nuevas |

Palancas de latencia, en orden de rendimiento por esfuerzo:

1. Plantilla determinista en lugar de segunda pasada del LLM (−500 ms).
2. STT en streaming, procesando antes del fin del enunciado (−200 ms percibidos).
3. TTS en streaming, hablando sobre el primer fragmento (−200 ms percibidos).
4. Conexión HTTP precalentada hacia el LLM (−100 ms).

---

## 13. Simplificaciones deliberadas

| Se omite | Se añade cuando |
|---|---|
| Encoding entero de categóricos | el dataset pase de unos 5 millones de filas |
| SQL generado por LLM | las preguntas excedan lo que cubren las 4 tools |
| Segundo modelo enrutador | haya varias fuentes de datos heterogéneas |
| Postgres o DuckDB | SQLite deje de responder en menos de 50 ms |
| Memoria de conversación persistente | se requiera continuidad entre sesiones |
| Resolución difusa de municipios | las preguntas de demostración bajen de nivel departamental a municipal |
| Agregados precomputados | la agregación en vivo supere los 100 ms |
