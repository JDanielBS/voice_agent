# CLAUDE.md

Instrucciones para cualquier modelo que trabaje en este repositorio.

**Lee `ARQUITECTURA.md` antes de escribir código.** Este archivo son las reglas operativas; `ARQUITECTURA.md` es el diseño completo con los siete flujos. No dupliques su contenido aquí ni lo contradigas: si algo debe cambiar, edita `ARQUITECTURA.md` en el mismo commit.

> Nota: este archivo manda sobre cualquier copia desactualizada dentro de subdirectorios. El diseño vigente es Postgres + 7 flujos + Twilio Media Streams + Azure AI Speech.

---

## Qué se está construyendo

Un agente de voz, accesible por llamada telefónica, que responde preguntas sobre una base de datos estructurada que se consume por una API REST paginada.

El usuario marca un número desde cualquier teléfono y habla. **No necesita internet**: el servicio es el que está conectado. Esa es la propiedad central del proyecto.

### Restricciones que definen el diseño

| Restricción | Consecuencia |
|---|---|
| La API devuelve máximo 1000 registros por petición | Se sincroniza todo una vez (42 peticiones); no se pagina al responder |
| El canal es voz telefónica: 1,5–2,5 s por turno | Un solo salto al LLM en el camino crítico; plantillas deterministas obligatorias |
| El audio de PSTN es de 8 kHz banda angosta | El STT pierde precisión; el resolver difuso no es opcional |
| Dataset de referencia: 41.427 filas × 20 columnas (~17 MB) | Cabe entero en una tabla propia; consultas en menos de 10 ms |
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

4. **Conexión a la base en read-only.** El runtime se conecta con un rol de Postgres sin permisos de escritura (`GRANT SELECT` únicamente). El runtime nunca escribe.

5. **Máximo 5 resultados por respuesta.** Por voz, más no se puede escuchar. Si hay más, devuelve el conteo total más los 3 primeros.

6. **Entidad ambigua o no resuelta: preguntar, nunca adivinar.** Un número correcto sobre la entidad equivocada es peor que no responder.

7. **Nunca silencio.** Todo fallo produce audio. En una llamada, el silencio se lee como caída del sistema. Si pasan 2 s sin audio saliente, suena `espera.wav`.

8. **Nunca escribir sobre la base que está sirviendo.** El refresco construye una tabla `reps_new`, valida y hace swap atómico (`ALTER TABLE ... RENAME TO` dentro de una transacción).

9. **Las frases fijas son audio pregrabado, no TTS.** Si el TTS está caído no puedes generar la disculpa por TTS. Los 8 WAV de `data/audio/` se generan en el montaje.

10. **El contexto enviado al LLM tiene tamaño fijo.** Ventana de 6 mensajes más un resumen de slots en una línea. El turno 50 debe costar lo mismo que el turno 2. No envíes el historial completo.

11. **Repetición, sin LLM.** "¿Qué dijiste?", "repita" y similares se detectan por patrón antes del modelo y reproducen el audio anterior desde caché. Cero latencia, cero costo.

12. **Timeouts cortos y explícitos.** STT 2 s, LLM 5 s, tool 500 ms, TTS 3 s. Los valores por defecto de los SDK son demasiado largos para una llamada.

---

## Estructura de archivos

```
src/
  ingest/
    client.py        # cliente paginado genérico (offset | page | cursor)
    sync.py          # API -> PostgreSQL, con checkpoint, idempotente
  schema/
    infer.py         # perfilado por cardinalidad -> schema.json
    vocab.py         # valor canónico + forma normalizada + alias
  query/
    tools.py         # las 4 tools tipadas, expuestas por HTTP
    resolve.py       # texto hablado -> valor canónico
    render.py        # resultado -> texto apto para oído
  agent/
    prompt.py        # prompt de sistema generado desde schema.json
    state.py         # 5 slots, TTL 3 turnos, ventana deslizante
    turn.py          # repetición, corrección, cambio de tema
    fallback.py      # timeouts, frases fijas, modo degradado
data/
  schema.json        # generado, no se versiona
  prompt.txt         # generado, no se versiona
  audio/             # 8 frases fijas en WAV 8 kHz, generadas
.env                 # DATABASE_URL (Postgres), credenciales Azure/Twilio — no se versiona
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

Se exponen por HTTP para que la plataforma de voz las llame como *custom tools* por webhook. La plataforma nunca toca la base: pasa por este servidor.

---

## Telefonía

**El transporte y el habla ya no vienen del mismo proveedor.** STT y TTS van por Azure AI Speech (tiempo real, español). Lo que queda por resolver es solo **transporte**: número DID + audio crudo bidireccional. Elegido: **Twilio Media Streams** (`<Stream>` en TwiML, WebSocket de audio). Es transporte puro — no hace STT/TTS/VAD/barge-in por ti, eso ya no lo regala el proveedor.

**Consecuencia directa:** VAD y barge-in, que antes daba la plataforma (Vapi), ahora son responsabilidad del código:
- Detección de fin de turno (~700 ms de silencio) se implementa sobre el stream de audio entrante, antes de mandarlo a Azure STT.
- Barge-in: al detectar habla del usuario mientras el TTS está sonando, cortar el envío de audio saliente al WebSocket y cancelar cualquier consulta en vuelo. No dejar la respuesta abandonada en el historial del LLM.

Umbral de VAD: ~700 ms de silencio como punto de partida. Es un valor de calibración, no una constante — ajústalo con llamadas reales.

**El número personal se conecta por desvío**, no directamente: `**21*<número destino>#` marcado en el celular. El operador no expone el audio de una línea móvil, así que un agente no puede contestar en ella; lo que se hace es desviar al DID de Twilio.

**No tramites un DID colombiano.** Exige cédula o cámara de comercio, dirección en la misma ciudad y factura de servicios de menos de 3 meses, con días hábiles de verificación. Queda documentado como paso de despliegue en `ARQUITECTURA.md` §12.5, no como bloqueante.

**Aviso legal al inicio de la llamada**: sistema automatizado, y grabación si se graba (habeas data, Ley 1581). Va dentro de `saludo.wav`.

---

## Orden de trabajo

No saltes fases. Cada una tiene criterio de salida verificable.

| Fase | Entregable | Criterio de salida |
|---|---|---|
| 1 | `client.py` + `sync.py` | tabla Postgres con 41.427 filas; el sync se reanuda tras interrupción |
| 2 | `infer.py` | `schema.json` con el rol correcto para las 20 columnas |
| 3 | `tools.py` + `resolve.py` | las 4 tools responden por HTTP, probadas con curl |
| 4 | `prompt.py` + agente | las 20 preguntas de oro aprueban **en modo texto** |
| 5 | Twilio Media Streams + Azure Speech + ngrok | llamada real contestada, consulta correcta, menos de 2,5 s |
| 6 | `turn.py` + `fallback.py` | repetición sin LLM, frases fijas, ventana deslizante |
| 7 | desvío + calibración | número personal desviado; VAD ajustado con llamadas reales |

**La fase 4 se valida sin voz.** Depurar un error de consulta a través de audio cuesta diez veces más. La voz se añade cuando la lógica ya es correcta.

Las fases 6 y 7 son las que separan una demostración de tres preguntas de una conversación sostenida. Si el tiempo aprieta, **la 6 vale más que la 7**.

---

## Convenciones de código

- Python 3.11 o superior. Español en nombres de dominio (`departamento`, `capacidad`), inglés en nombres técnicos (`fetch`, `paginate`, `resolve`).
- Biblioteca estándar primero para todo lo que no sea acceso a datos: `difflib`, `unicodedata`, `re`, `pathlib`. No añadas una dependencia por algo que resuelvan diez líneas.
- Dependencias justificadas hasta ahora: `psycopg` (driver Postgres, sin ORM — son 4 queries fijas), `pandas` solo para el perfilado offline (fase 2), `fastapi` + `uvicorn` para el servidor de tools, Azure Speech SDK (STT/TTS), Twilio SDK (transporte de llamada). Nada más sin preguntar.
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
- corrección sobre la marcha ("no, dije Chocó")
- petición de repetición
- cambio de tema con un filtro que debe limpiarse

No declares una fase terminada sin ejecutar su verificación y mostrar la salida.

---

## Trampas conocidas de este dataset

Están documentadas porque ya costaron análisis. No las redescubras.

- **`departamento` mezcla distritos con departamentos.** Contiene *Cali*, *Barranquilla* y *Bogotá D.C* junto a *Antioquia* o *Nariño*. La desambiguación no es opcional, y por teléfono se resuelve ofreciendo voz **y** DTMF a la vez.
- **Un mismo prestador aparece en muchas filas**, una por tipo de capacidad. Contar filas no es contar IPS. Para contar entidades, usa `COUNT(DISTINCT codigo_sede)`.
- **`fecha_corte` es constante**: los datos son un snapshot, no un flujo. El refresco va por cron, nunca por petición.
- **El CSV necesita fallback de encoding**: intenta `utf-8-sig` y luego `latin1`.
- **`num_nivel_atencion` y `num_digito_verificion` tienen nulos.** Son `float64` por eso, no porque sean medidas.
- **Los 1027 municipios no se resuelven bien con audio de 8 kHz.** "Chocó", "Chocontá" y "Chontaduro" colisionan. El resolver difuso con umbral 0.7 y la confirmación del filtro en la respuesta son las dos defensas.

---

## Trampas de conversación

- **Los filtros persisten y eso es un arma de doble filo.** Permiten "¿y en Chocó?" pero contaminan el turno siguiente. Reglas de limpieza en `ARQUITECTURA.md` §10.6; el agente debe **enunciar los filtros activos** en cada respuesta.
- **Una respuesta interrumpida no va al historial.** Si el usuario hace barge-in, lo que no escuchó no existe para el contexto.
- **La memoria lejana está limitada a propósito.** Una referencia a cuatro turnos atrás puede quedar fuera de la ventana; el agente pide reformular, no adivina. No implementes resolución anafórica profunda.

---

## Lo que no se construye todavía

No añadas nada de esta lista sin que se cumpla su gatillo:

| Omitido | Añadir cuando |
|---|---|
| Encoding entero de categóricos | el dataset pase de unos 5 millones de filas |
| SQL generado por LLM | las preguntas excedan lo que cubren las 4 tools |
| Segundo modelo enrutador | haya varias fuentes de datos heterogéneas |
| Memoria de conversación persistente | se requiera continuidad entre sesiones |
| Resolución anafórica profunda | las pruebas muestren referencias lejanas frecuentes |
| Resumen del historial por LLM | la ventana de 6 mensajes se quede corta **y** haya presupuesto de latencia |
| Redundancia de proveedor de telefonía | el servicio tenga compromiso de disponibilidad |
| DID colombiano o línea 01800 | se pase de demostración a servicio público real |
| Llamadas salientes | alguien lo pida — y entonces aplica el RNE (Ley 2300 de 2023) |
| Agregados precomputados | la agregación en vivo supere los 100 ms |
| Autenticación, multiusuario, panel de administración | alguien lo pida |

---

## Datos que faltan por confirmar

Pregunta antes de asumir:

- URL y contrato real de la API: ¿`offset`/`limit`, `page`/`per_page` o cursor? ¿Soporta filtros o agregación del lado del servidor?
- ¿Requiere autenticación? ¿Hay límite de tasa?
- Si la evaluación del reto es a nivel departamental o municipal (los 1027 municipios con audio de 8 kHz son el caso difícil).
- Tarifa del desvío de llamadas del operador del usuario hacia un número internacional.

### Ya decidido, no volver a preguntar

- Modelo: **GPT-4o-mini vía Azure OpenAI**, un solo salto, tool-calling. También etiqueta sentimiento en la misma respuesta (ver `ARQUITECTURA.md` §10.9).
- STT/TTS: **Azure AI Speech**, tiempo real, español.
- Transporte de llamada: **Twilio Media Streams**. VAD y barge-in ya no los da el proveedor, se implementan en código.
- Base de datos: **PostgreSQL** (Azure Database for PostgreSQL), driver directo (`psycopg`), sin ORM.
- Conexión del número personal: **desvío GSM** `**21*<número>#`.
- DID colombiano: **fuera de alcance** para la hackatón.
- UI: transcripción diarizada en vivo + panel de sentimiento, vía WebSocket al backend.
