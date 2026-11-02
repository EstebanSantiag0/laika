# Arquitectura

## Servicios (Docker Compose)

| Servicio | Imagen | Recursos | Papel |
|---|---|---|---|
| `voice` | `local/laika-voice` (`voice/`) | GPU (~2.6 GB, bf16), CPU para Whisper | `POST /v1/tts` (Qwen3-TTS 0.6B, clonación de voz desde `models/voice/refs/`), `POST /v1/stt` (faster-whisper large-v3-turbo int8) |
| `llm` | `local/llama.cpp:server-cuda12.4-sm75` (`llama/BUILD.md`) | GPU (~3 GB) + ~10–12 GB de RAM | Gemma 4 26B-A4B IQ3_XXS. `--cpu-moe`: la atención va en GPU y los expertos MoE en RAM. 2 slots: 0 = conversación (su caché de prompt se conserva), 1 = tareas de fondo |
| `embed` | la misma imagen de llama.cpp | CPU, 4 hilos, prioridad baja (`cpu_shares: 512`) | Qwen3-Embedding-0.6B, vectores de 1024 dimensiones para la memoria y la biblioteca |
| `app` | `local/laika-app` (`app/`) | CPU | FastAPI: conversación, tutor, memoria, recordatorios, biblioteca, PWA, Telegram, API |

Solo se publican puertos en `127.0.0.1`. El acceso desde tus dispositivos pasa por
`tailscale serve`, que agrega la cabecera `Tailscale-User-Login`; la app solo atiende a
`ALLOWED_TS_LOGIN`. `GET /health` queda abierto para saber si está despierta.

## Datos

| Ruta | Contenido |
|---|---|
| `data/laika.db` | SQLite (WAL): mensajes, turnos, ajustes, memoria (episodios, hechos, entidades, pendientes, rondas), índices FTS5 y sqlite-vec, recordatorios, áreas, vocabulario |
| `data/library.db` | La biblioteca (documentos, secciones, fragmentos, índices). Se puede reconstruir |
| `data/audio/`, `data/uploads/` | Audio de las notas de voz y de sus respuestas |
| `data/library/` | Originales de la biblioteca, copias HTML de los links (`snapshots/`), texto OCR por página (`ocr/`) y páginas de PDF en PNG (`pages/`) |
| `vault/` | Bóveda de Obsidian que la app escribe (perfil, diario, personas, temas, biblioteca, `Brain/`), con su propio historial git local y una copia nocturna de la base en `vault/.laika/backup/` |
| `models/` | Los modelos (~26 GB) |

Nada de esto está en git, y nada sale del servidor salvo los mensajes de Telegram si lo usas.

## El camino de un mensaje

1. **Entrada.** Llega por la PWA o la app (WebSocket `/ws`), o por Telegram (sondeo saliente). Las
   notas de voz se transcriben primero. El mensaje se guarda al momento y se publica a todos los
   clientes.
2. **Espera.** El motor (`conversation.py`) junta mensajes en un lote. No responde mientras escribes,
   grabas o tienes un borrador (las señales `typing` del cliente); en Telegram adivina con pausas más
   largas tras mensajes que parecen inacabados.
3. **Notas del turno.** Ganchos `before_turn` agregan notas para ella: comandos de memoria
   ("recuerda…"), recuerdos relevantes (`recall.py`), pendientes que tocaste, recordatorios guardados
   (`reminders.py`), pasajes de libros (`books.py`) y una palabra para aprender (`vocab.py`).
4. **Prompt.** `persona.py` arma el prompt de sistema (persona, reglas de estilo, niveles, memoria
   nocturna) y el historial. `render.py` lo reproduce **byte a byte** igual que en turnos anteriores,
   así llama.cpp reutiliza la caché y solo lee lo nuevo.
5. **Generación.** Streaming por `/completion` en el slot 0. En inglés, el "slop guard"
   (`slop.py`) revisa cada oración; si detecta una frase de asistente, retrocede justo antes y
   prohíbe ese primer token ahí.
6. **Burbujas.** La respuesta se corta en burbujas por líneas en blanco, con pausas de "escribiendo".
   Si vuelves a escribir, se detiene y espera.
7. **Después.** Ganchos `after_turn`: el tutor (correcciones ✏️, pinyin), la voz (TTS por burbuja si
   toca), marcar qué recuerdos usó, marcar pendientes que ya preguntó.
8. **En segundo plano** (`jobs.py`, solo cuando no estás): cerrar episodios, el sueño nocturno,
   indexar libros, resúmenes y glosas. Si escribes, la tarea se cancela y se reintenta después.
9. **Recordatorios.** Un planificador revisa cada 20 s y entrega lo vencido por Telegram y WebSocket.

## Decisiones técnicas principales

- **Todo local.** Ni el chat ni la voz pasan por servicios en la nube (Telegram es opcional).
- **Un modelo MoE grande con los expertos en RAM** en vez de uno pequeño que quepa en 6 GB: más
  calidad por el mismo hardware; la RAM pesa más que la VRAM.
- **Reproducción exacta del prompt** (`/completion` crudo + `render.py`): sin ella, cada turno
  releería miles de tokens a ~150 tok/s.
- **Estilo con ejemplos y una guardia de código**, no con listas de frases prohibidas dentro del
  prompt (que el modelo tiende a repetir).
- **SQLite + sqlite-vec + FTS5** en un solo archivo, sin base de datos vectorial aparte.
- **Obsidian como vista editable** de la memoria: lo que escribes ahí gana.
- **Los recordatorios son mensajes del sistema**, no burbujas de ella, para no romper la reproducción
  exacta del historial.
- **Versiones fijadas**: llama.cpp v0.7.0, torch 2.6.0+cu124, faster-qwen3-tts 0.5.4 con
  transformers 5.15.1, faster-whisper 1.2.1 con av 16.0.1. Actualiza de a una.

## Módulos de `app/laika/`

| Módulo | Qué hace |
|---|---|
| `main.py` | Arma todo, rutas HTTP, middleware de Tailscale |
| `conversation.py` | Lotes, turnos, burbujas, interrupciones, cambio de idioma |
| `persona.py`, `render.py`, `slop.py` | Prompt, reproducción exacta, guardia de estilo |
| `llm.py`, `embed.py`, `voice.py` | Clientes de llama.cpp, embeddings y voz |
| `tutor.py`, `lang.py` | Correcciones, pinyin, detección de idioma |
| `memory.py`, `consolidate.py`, `nightly.py`, `recall.py`, `memory_commands.py` | La memoria ([`memory.md`](memory.md)) |
| `areas.py`, `reminders.py` | Áreas de vida y recordatorios |
| `brain.py`, `links.py`, `graph_edit.py`, `api_brain.py`, `retry.py` | El segundo cerebro: guardar, enlazar, links y reels, volver a traer, editar el grafo, su API; reintentos a los servicios de modelos |
| `vault.py`, `edits.py` | Notas de Obsidian y tus ediciones |
| `library.py`, `extract.py`, `books.py`, `summaries.py`, `vocab.py`, `anki.py`, `words.py` | Biblioteca y vocabulario |
| `jobs.py`, `compaction.py`, `admin.py` | Tareas de fondo, compactar historial, comandos de admin |
| `api.py`, `channels/` | Grafo y recordatorios para laika-app, WebSocket/PWA, Telegram |

Los comentarios del código citan a veces pasos y decisiones como `P3.3`, `D15` o `BENCHMARKS P4.1`:
vienen de la bitácora privada de desarrollo (con datos personales), que no se publica. Este
documento y [`memory.md`](memory.md) resumen lo esencial.
