# Cómo funciona la memoria

La idea: que recuerde como una amiga, no como un buscador. Recuerda lo importante y lo reciente,
olvida los detalles triviales del ranking (nunca del almacén), no inventa, y deja que tú corrijas.

## Qué guarda (SQLite, `data/laika.db`)

| Tabla | Qué es |
|---|---|
| `episodes` | Un tramo de conversación (se corta tras 30 min de silencio): título, entrada de su diario en primera persona, ánimo, intensidad emocional, relevancia |
| `facts` | Hechos atómicos sobre ti y tu vida: texto, categoría, importancia, confianza, sensible, evidencia (ids de mensajes), estado (`active`, `tentative`, `superseded`, `archived`), `time_scope`, `valid_from`/`valid_to`, `review_after` |
| `entities`, `edges` | Personas, lugares, obras, temas y cosas que mencionas, y cuáles aparecen juntas |
| `intentions` | Pendientes suaves para preguntarte ("¿cómo te fue en el examen?"), con fechas y palabras clave |
| `rounds` | Cada mensaje tuyo y su respuesta, para recordar las palabras exactas (los viejos salen del índice) |
| `reminders` | Recordatorios explícitos con hora |
| `areas`, `area_links` | Áreas de vida y a qué hechos, entidades y episodios pertenecen |
| `items`, `items_fts`, `items_vec` | Índice común: texto completo (FTS5) y vectores (sqlite-vec, 1024 dimensiones) de hechos, episodios y rondas |
| `tombstones` | Lo que pediste olvidar: solo un hash y un vector, nunca el texto, para no volver a aprenderlo |
| `injections` | Qué recuerdos se le ofrecieron en cada turno y cuáles usó |

## 1. Durante la conversación: recordar

`recall.py`, en cada turno:
- Un filtro barato ignora relleno ("jaja", "ok").
- Busca candidatos por vector y por texto, más los hechos de las personas que nombras y sus vecinos.
- Puntúa cada candidato como la memoria humana:
  `A = B + 1.5·I + 1.0·z_sim + 0.4·z_bm25 + 0.5·spread + 0.3·ctx + 0.7·goal`.
  - `B` es la activación ACT-R (cuántas veces y qué tan recientemente se usó);
  - `I` es la importancia;
  - `z_*` miden cuánto destaca la similitud frente a todos sus recuerdos;
  - `spread` premia lo relacionado con quien nombraste;
  - `ctx` premia el idioma actual;
  - `goal` premia un pendiente abierto.
- Solo los ganadores claros entran (máx. 3 hechos + 1 línea de diario), con un tiempo aproximado
  ("la semana pasada"). Si nada destaca, no recuerda nada.
- Un recuerdo cuenta como "usado" solo si su respuesta realmente se apoyó en él.
- Los hechos **archivados** (ya pasados) también se pueden recordar, como "in the past: …" y con
  menos peso.
- Si tocas un **pendiente** (una palabra clave más un mensaje relacionado), la nota se lo recuerda.
  Cuando ella lo pregunta, queda marcado como preguntado.

## 2. Cuando te vas: consolidar

`consolidate.py`, tras 30 minutos sin mensajes, en el slot de fondo:
- Una llamada con esquema JSON escribe el episodio: título, diario, hechos, entidades, pendientes,
  momentos "de ustedes dos" y opiniones propias de Laika (para que sea consistente).
- Cada hecho se compara con lo que ya sabe: igual / actualiza / reemplaza (el viejo queda como
  historia) / nuevo.
- Cada hecho recibe hasta 2 **áreas** (de `config.yaml`) y un **alcance temporal**:
  - `permanent`: quién eres, tus gustos, tu gente. Sin fechas.
  - `stage`: verdad en esta etapa ("busca prácticas", "vive en otra ciudad por ahora"); se revisa
    tras `stage_review_days` (90).
  - `dated`: un evento o una fecha límite, con `valid_from`/`valid_to`.
- Los hechos de baja confianza esperan en el Inbox de Obsidian hasta que los confirmes.
- Las entidades y los episodios toman las áreas más comunes de sus hechos.

## 3. Cada noche: dormir

`nightly.py`, a las 03:00 (o en el primer momento tranquilo después):
1. Cierra los episodios abiertos.
2. Fusiona hechos casi idénticos.
3. **Archiva** los hechos `dated` cuyo día ya pasó (nunca borra).
4. **Revisa etapas**: un hecho `stage` vencido se vuelve un pendiente ("¿sigue siendo cierto…?").
5. Retira pendientes: los ya preguntados se dan por hechos; los abiertos caducan tras **3 días en
   que de verdad chateaste** después de su fecha (los días con el servidor apagado no cuentan).
6. Reescribe los resúmenes de personas y temas, y su **retrato** de ti (una revisión del anterior,
   no un resumen de resúmenes).
7. Arma la parte de memoria del prompt (retrato, pendientes de esta semana, lo reciente, "ustedes
   dos"). Solo esto cambia el prompt, una vez por noche.
8. Recorta el historial literal, hace copia de la base, hace commit de la bóveda y precalienta.

## 4. Recordatorios

`reminders.py`. "Recuérdame…", "remind me…", "напомни…", "提醒我…" disparan una pequeña llamada JSON
con la fecha y hora actuales y los últimos mensajes (así entiende "recuérdame hacer *eso*"):
- un día sin hora → `reminders.default_time` (10:00), y ella lo dice;
- una hora ya pasada → mañana; "en 20 minutos" → ahora + 20 min;
- sin hora → no guarda nada todavía: pregunta, y lee tu siguiente mensaje.

Un planificador entrega a la hora exacta (⏰ en el chat, Telegram con botones, evento WebSocket). Si
el servidor estaba apagado, entrega al arrancar con "(was due …)". Si no hay a quién entregar, queda
pendiente y reintenta. Estados: `pending` → `delivered` → `done`, o `snoozed`.

## 5. Tú mandas: comandos y Obsidian

- "Recuerda que…" lo guarda con confianza alta. "Olvida…" lo borra de verdad y deja una lápida.
  "No vuelvas a mencionar…" lo oculta sin borrarlo.
- En `vault/`: `Dasha/Profile.md` (tu perfil y lo que sabe; la carpeta lleva `user.name` de `config.yaml`), `Dasha/Now.md`, `Journal/`,
  `People/`, `Topics/`, `Laika/`, `Library/` e `Inbox`. Si **borras** una línea de un hecho, se
  rechaza y no se vuelve a aprender. Si la **reescribes**, tu versión pasa a ser el hecho. Si marcas
  una casilla del Inbox, se confirma. Si reescribes el retrato, tu versión manda.

## 6. El segundo cerebro

Además de lo que sabe de ti, guarda lo que tú le das (`brain.py`):

- **Tipos**: `idea`, `for_later` (algo para leer, ver o hacer algún día, sin hora: no es un
  recordatorio) y `saved` (un link, un archivo, un reel, una captura). Lo que te gusta o no te gusta
  no es un tipo: es un hecho con `polarity` (`like` / `dislike`).
- **Clasificar sin que tú archives**: dos ejes fijos y pequeños (10 áreas de vida como máximo, 3 tipos)
  y hasta 3 etiquetas libres. La memoria de trabajo humana maneja unas 4 cosas a la vez; pasando de
  ~10 opciones la gente deja de archivar con constancia. Así que ella elige y tú solo corriges.
- **Enlaces**: al guardar, una llamada al modelo elige hasta 3 nodos existentes (personas, proyectos,
  hechos, otras cosas guardadas) entre candidatos recuperados por nombre y por significado, con una
  razón corta. Nunca inventa un nodo. Los enlaces viven en `graph_links`.
- **Links y videos** (`links.py`): la página se baja y trafilatura saca el artículo; los videos se
  bajan con yt-dlp y Whisper los transcribe cuando no estás. El texto entra a la biblioteca.
- **PDF y capturas**: números de página por fragmento (para citar), sin encabezados repetidos; las
  páginas escaneadas y las capturas se leen con Tesseract.
- **Volver a traer**: una idea o algo "para después" vuelve a los 3, 10 y 30 días, como mucho uno al
  día, solo de día y en un momento ligero. Los domingos pregunta si sigues queriendo lo viejo de "para
  después"; lo que pasó por todo eso sin tocarse se archiva a los 60 días. Las etiquetas que significan
  lo mismo se fusionan de noche.
- **Editar todo** (`graph_edit.py`): cada nodo y cada enlace se edita o se borra desde la app. Borrar
  manda a una papelera de 30 días (con deshacer); un enlace automático que ocultas no vuelve a
  aparecer. "Olvida X" en el chat sigue borrando al instante.

## 7. Ver la memoria desde fuera

`GET /api/graph` ([`API.md`](API.md)) devuelve áreas, entidades, hechos, episodios, recordatorios,
cosas guardadas y etiquetas como un grafo (aristas `about`, `related`, `in_episode`, `in_area`,
`similar`, `tagged`, `link`), y las rutas de `API.md` permiten editarlo.
[laika-app](https://github.com/EstebanSantiag0/laika-app) lo dibuja.

## Evaluación

`scripts/memory_eval.py` corre un set de preguntas de prueba (las tuyas, guardadas en
`vault/.laika/memory_probes.yaml`) sobre una copia de la base. Mide si encuentra lo que debe
(un hecho, varias sesiones, entre idiomas) y si calla cuando no debe recordar nada.
