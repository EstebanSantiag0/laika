# Laika

Laika es una amiga con IA para practicar idiomas que corre **entera en un servidor de casa**: sin
modelos en la nube, sin voz en la nube. Escribe como una persona (espera mientras tecleas, responde
en burbujas cortas), habla y escucha notas de voz, corrige con cariño, y **recuerda**: tus
conversaciones se convierten en una memoria organizada por áreas de tu vida, con fechas y recordatorios.

Está pensada para un hispanohablante que practica inglés, ruso y chino, pero los idiomas, niveles y
la personalidad se configuran.

> Cliente de escritorio y Android (Flutter): **[laika-app](https://github.com/EstebanSantiag0/laika-app)**.
> Habla con este servidor por la API descrita en [`docs/API.md`](docs/API.md).

## Índice
1. [Qué hace](#qué-hace)
2. [Cómo está hecha](#cómo-está-hecha)
3. [Hardware que necesitas](#hardware-que-necesitas)
4. [Instalación paso a paso](#instalación-paso-a-paso)
5. [Configuración](#configuración)
6. [Uso diario](#uso-diario)
7. [Cómo funciona la memoria](#cómo-funciona-la-memoria)
8. [Solución de problemas](#solución-de-problemas)
9. [Pruebas](#pruebas)
10. [Documentación](#documentación)
11. [Licencia](#licencia)
12. [Autoría](#autoría)

## Qué hace

- **Escribe como una persona.** No contesta mientras escribes o grabas; junta varios mensajes y
  responde en burbujas. Un "slop guard" en streaming reescribe frases de asistente ("¡Qué buena
  pregunta!") mientras se generan.
- **Voz en los dos sentidos.** Mandas notas de voz (Whisper, en CPU) y ella responde con voz
  (Qwen3-TTS en GPU, con una voz clonada de un audio de referencia).
- **Práctica de idiomas.** Un "tutor" aparte manda correcciones ✏️, agrega pinyin al chino y adapta
  el nivel por idioma. Si escribes en otro idioma de práctica, pregunta antes de cambiar.
- **Memoria ("Organic Knowledge").** Episodios (su diario), hechos, personas y temas, pendientes que
  quiere preguntarte, todo con búsqueda por significado. Cada noche "duerme": consolida, archiva lo
  pasado y reescribe su retrato de ti. Todo se refleja en una bóveda de **Obsidian**; si editas una
  nota, tu versión gana.
- **Áreas de vida y tiempo.** Cada recuerdo se clasifica (estudios, proyectos, finanzas, idiomas,
  salud, personas…; la lista se edita en `config.yaml`) y sabe si es permanente, de esta etapa, o de
  una fecha concreta.
- **Recordatorios de verdad.** "Recuérdame mañana a las 5 pagar la renta" crea un recordatorio que
  llega a esa hora por Telegram y a la app, aunque el servidor haya estado apagado (llega tarde,
  nunca se pierde).
- **Biblioteca.** Le mandas libros (EPUB, PDF, FB2, DOCX…): los cita con capítulo y página, resume
  capítulos, mide tu vocabulario y exporta mazos de Anki. Las páginas escaneadas se leen con OCR
  (Tesseract) mientras no estás.
- **Segundo cerebro.** "Apunta esta idea…", "para después…", un link, un reel o una captura: ella lo
  guarda, lo clasifica (área, tipo, etiquetas) y lo enlaza con lo que ya sabe de ti (tus proyectos,
  metas, personas), con una razón corta. Los links guardan el texto de la página (los links mueren);
  los reels se transcriben; las capturas se leen con OCR. Te recuerda con calma una idea o algo
  "para después" a los 3, 10 y 30 días, como mucho una vez al día. Lo que te gusta y no te gusta
  queda como hechos sobre ti. Todo se puede editar y borrar desde laika-app (con papelera de 30 días).
- **Varios frentes, una sola conversación.** PWA por Tailscale, bot de Telegram y la app Flutter.

## Cómo está hecha

```
             tu teléfono / laptop (en tu tailnet de Tailscale)
        ┌───────────────┬──────────────────┬────────────────────┐
        │  PWA (web)    │  laika-app       │  Telegram          │
        └──────┬────────┴────────┬─────────┴─────────┬──────────┘
               │ HTTPS (tailscale serve)             │ (sondeo saliente)
┌──────────────▼─────────────────▼───────────────────▼─────────────────┐
│ app  (FastAPI · 127.0.0.1:3010)                                      │
│  conversación · tutor · memoria · recordatorios · biblioteca · API   │
│  SQLite (data/laika.db) ───────► bóveda Obsidian (vault/)            │
└───────┬───────────────────────┬──────────────────────┬───────────────┘
        │                       │                      │
┌───────▼─────────┐   ┌─────────▼────────┐   ┌─────────▼───────────────┐
│ llm             │   │ embed            │   │ voice                   │
│ llama.cpp       │   │ llama.cpp        │   │ Qwen3-TTS (GPU)         │
│ Gemma 4 26B-A4B │   │ Qwen3-Embedding  │   │ faster-whisper (CPU)    │
│ GPU + expertos  │   │ 0.6B (CPU)       │   │                         │
│ MoE en RAM      │   │                  │   │                         │
└─────────────────┘   └──────────────────┘   └─────────────────────────┘
```

| Servicio | Qué corre | Puerto (solo localhost) |
|---|---|---|
| `llm` | llama.cpp, Gemma 4 26B-A4B (IQ3_XXS): atención en GPU, expertos MoE en RAM (`--cpu-moe`) | 8081 |
| `voice` | Qwen3-TTS 0.6B (GPU, bf16) + faster-whisper large-v3-turbo (CPU, int8) | 8082 |
| `embed` | llama.cpp, Qwen3-Embedding-0.6B (CPU); perfil `memory` | — |
| `app` | FastAPI: motor de conversación, memoria, recordatorios, PWA, Telegram, API | 3010 |

Más detalle en [`docs/architecture.md`](docs/architecture.md).

## Hardware que necesitas

Medido en: **i5-10400F, 16 GB de RAM, GTX 1660 SUPER (6 GB, sm_75), driver NVIDIA 550 (CUDA 12.4)**,
disco NVMe.

| | Mínimo razonable | Notas |
|---|---|---|
| GPU NVIDIA | 6 GB de VRAM | ~3 GB el LLM + ~2.6 GB la voz. Otra arquitectura → cambia `CUDA_DOCKER_ARCH` al compilar llama.cpp |
| RAM | 16 GB (32 GB mejor) | Los expertos del modelo ocupan ~10–12 GB como caché de página |
| Disco | ~40 GB libres | Modelos ~26 GB + imágenes Docker |
| CPU | 6 núcleos | Whisper y los expertos MoE corren en CPU; ventilación decente (la CPU se calienta al generar) |

Velocidad típica: ~15–20 tokens/s de respuesta, primera voz en unos segundos.
Sin GPU grande: usa el modelo ligero (Gemma 4 E4B, `scripts/download_models.sh B`, ver
[Configuración](#configuración)).

## Instalación paso a paso

Todo se hace en el servidor (Linux con Docker). Los comandos asumen que estás en la carpeta del repo.

**1. Requisitos**
- Docker con el plugin Compose y el
  [NVIDIA Container Toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/).
  Prueba: `docker run --rm --gpus all nvidia/cuda:12.4.1-base-ubuntu22.04 nvidia-smi`.
- [Tailscale](https://tailscale.com/) en el servidor y en tus dispositivos.
- Python 3 en el host (solo para los scripts de descarga).

**2. Clona el repo**
```bash
git clone https://github.com/EstebanSantiag0/laika.git && cd laika
```

**3. Compila la imagen de llama.cpp** (las imágenes oficiales no corren sm_75 con el driver 550).
Sigue [`llama/BUILD.md`](llama/BUILD.md). Tarda un rato (descarga ~5 GB de imágenes CUDA).

**4. Descarga los modelos** (verificados con sha256; se pueden reanudar):
```bash
scripts/download_models.sh C E      # C = Gemma 4 26B-A4B (chat), E = Qwen3-Embedding (memoria)
```

**5. Prepara la voz.** El servicio de voz clona una voz a partir de un audio corto de referencia en
`models/voice/refs/<nombre>.wav` (con `TTS_CLONE_MODE=icl` también hace falta la transcripción en `<nombre>.txt`). Usa una voz que tengas
derecho a usar. `scripts/make_neighbor_voice.py` muestra cómo se generó la voz original con un
modelo de diseño de voz. Pon el nombre en `DEFAULT_VOICE` (`compose.yaml`) y en `voice.default_voice`
(`config.yaml`).

**6. Preséntate (configuración inicial)**
```bash
python3 scripts/setup.py
```
Te pregunta, una sola vez:
- **tu nombre** (cómo te llama Laika);
- **tu género** (mujer, hombre, no binario o prefiero no decir): sirve para la gramática en español y
  ruso (*cansado/cansada*, *устал/устала*);
- **tus pronombres en inglés** (*he*, *she* o *they*): sus instrucciones internas hablan de ti con
  ellos;
- **una línea sobre ti** para su memoria, y tu **zona horaria**;
- tu **login de Tailscale** (solo ese usuario puede usar la app; no lo dejes vacío fuera de pruebas) y,
  si quieres, un **bot de Telegram** (token de @BotFather y tu ID numérico de @userinfobot).

Escribe la sección `user` y la zona horaria en `config.yaml`, y crea `.env` con permisos 600 (nunca se
sube a git). Puedes volver a ejecutarlo cuando quieras; cambiar el nombre después del primer arranque
deja la carpeta vieja del perfil en la bóveda.

**7. Revisa `config.yaml`** (ver [Configuración](#configuración)): idiomas, niveles, áreas de vida.

**8. Arranca**
```bash
docker compose --profile memory up -d --build
docker compose ps                      # espera a que los 4 servicios estén "healthy"
docker compose exec app python -m laika.admin warm   # precalienta el prompt (primera respuesta rápida)
```

**9. Publica la app en tu tailnet (no en internet)**
```bash
tailscale serve --bg 3010
```
Abre `https://<tu-servidor>.<tu-tailnet>.ts.net` en el teléfono y "Agregar a pantalla de inicio".
La seguridad depende de la cabecera de login que pone `tailscale serve`: **no expongas el puerto 3010
a internet** (ni con Funnel ni con un túnel).

**10. Ponle nombre.** Al primer arranque se crea `vault/Laika/Persona.md`: ahí está su personalidad y
el nombre que elijan juntos.

## Configuración

Todo en `config.yaml` (sin secretos):

| Sección | Para qué |
|---|---|
| `llm` | URL, slots (0 = conversación, 1 = tareas de fondo), muestreo |
| `voice` | voz por defecto, modo (`mirror` responde con voz si le hablas con voz) |
| `batching` | cuánto espera antes de responder (web, Telegram, ráfagas) |
| `language` | idioma de práctica por defecto, idioma nativo, pinyin, correcciones |
| `memory` | zona horaria (`tz`), hora del "sueño" nocturno, revisión de etapas (`stage_review_days`) |
| `reminders` | hora por defecto si dices "mañana" sin hora (`default_time`), posponer |
| `areas` | **las áreas de vida** (id, nombre, color). Puedes renombrar y recolorear; el `id` es lo que se guarda |
| `library` | tamaño de fragmentos y de subida de libros |

Elección del modelo: en `compose.yaml`, servicio `llm`, cambia `-m` a `gemma-4-E4B-it-Q4_K_M.gguf`
(descárgalo con `scripts/download_models.sh B`) si tienes menos RAM o VRAM.

**Tú.** La sección `user` de `config.yaml` (la escribe `scripts/setup.py`): `name` (por defecto
**Dasha**), `pronouns` (`he`, `she` o `they`; por defecto `they`), `gender` opcional (`woman`, `man`,
`nonbinary`) y `about`. El nombre va a sus prompts, a las transcripciones y a la carpeta de tu perfil en
la bóveda (`<nombre>/Profile.md`). Las plantillas de prompt están escritas con *he/him/his* y la app las
reescribe a *she* o *they* (con concordancia: *they are*, *they write*). Si arrancas sin configurarlo,
el log te lo recuerda.

## Uso diario

- **Chatea** desde la PWA, la app o Telegram: es una sola conversación.
- **Voz**: toca 🎤 para grabar y ■ para enviar (PWA), o manda una nota de voz (Telegram). `/voice mirror|always|never`.
- **Idioma de práctica**: `/lang en|ru|zh`, o acepta el "¿Cambiar a…?" cuando escribes en otro.
- **Correcciones**: `/corrections on|end|off`.
- **Memoria**: "recuerda que…", "olvida lo de…", "no vuelvas a mencionar…". También puedes editar
  las notas de `vault/` en Obsidian.
- **Recordatorios**: "recuérdame el viernes a las 5 pagar la renta". Ella confirma la hora; llega un
  ⏰ con botones "Done" / "Snooze 1 h" en Telegram.
- **Libros**: 📎 en la PWA, un documento en Telegram, o copia el archivo a `vault/Library/_inbox/`.
  Luego `/books`, `/reading <n> [capítulo]`, `/anki <n>`.
- **Segundo cerebro**: "apunta esta idea: …", "para después: …", "guarda esto" o simplemente un link
  o una foto. "No, es para después" cambia el tipo de lo último que guardaste; "olvida el link de …"
  lo borra; "¿qué tengo guardado sobre …?" lo busca. Las notas quedan en `vault/Brain/`. Si Instagram
  cambia y los reels dejan de bajar: `docker compose exec app python -m laika.admin update-ytdlp`.
- **Mantenimiento**: tras reiniciar `llm`, ejecuta `docker compose exec app python -m laika.admin warm`.
  `admin nightly` y `admin consolidate` fuerzan el sueño y la consolidación.

## Cómo funciona la memoria

En corto (todo en [`docs/memory.md`](docs/memory.md)):

1. **Mientras hablan**, cada turno puede recibir una nota con recuerdos relevantes ("You remember: …"),
   elegidos por similitud, por las personas que nombras y por lo reciente/importante. Si nada destaca,
   no "recuerda" nada (mejor que inventar).
2. **Cuando te vas** (30 min sin mensajes), la conversación se cierra en un **episodio**: una entrada
   de su diario, hechos nuevos o actualizados, personas y temas, pendientes para preguntarte después,
   cada hecho con su área de vida y su alcance en el tiempo.
3. **Cada noche** (03:00): fusiona duplicados, archiva lo que ya pasó, pregunta por las "etapas"
   viejas, reescribe su retrato de ti, hace copia de la base de datos y precalienta el prompt.
4. **Recordatorios** explícitos van aparte: se guardan al momento y un planificador los entrega.
5. **Obsidian**: `vault/` tiene tu perfil, diario, personas, temas y la biblioteca. Si borras o
   reescribes una línea de un hecho, Laika lo acepta.

## Solución de problemas

| Síntoma | Qué revisar |
|---|---|
| La primera respuesta tarda mucho | Normal tras reiniciar `llm`: ejecuta `admin warm`. |
| `llm` no arranca / "no kernel image" | La imagen de llama.cpp no coincide con tu GPU: recompila con tu `CUDA_DOCKER_ARCH` (`llama/BUILD.md`). |
| Se queda sin memoria (OOM) o va lentísimo | Poca RAM para el modelo 26B: usa Gemma 4 E4B o agrega RAM. Revisa `free -h` y el swap. |
| La voz sale con ruido o NaN | En GPUs Turing usa `TTS_DTYPE: bfloat16` (fp16 desborda). |
| `403 forbidden` en el navegador | `ALLOWED_TS_LOGIN` no coincide con tu login de Tailscale, o no entras por `tailscale serve`. |
| Telegram no responde | Token o ID equivocados; revisa `docker compose logs app`. Al arrancar, un fallo de DNS en `set_my_commands` es inofensivo. |
| No llega un recordatorio | `docker compose logs app \| grep reminder`; si no había Telegram ni app abierta, queda pendiente y se reintenta. |
| La memoria no encuentra algo | `scripts/memory_eval.py` corre un set de pruebas sobre una copia de la base. |
| Un reel no se guarda | Algunos piden sesión en Instagram (no se puede); si fallan todos, `admin update-ytdlp`. |
| Un PDF se queda en "ocr_pending" | Tiene páginas escaneadas: el OCR corre cuando llevas 2 min sin escribir. |
| Ventilador muy fuerte al generar | La CPU trabaja los expertos MoE: mejora la refrigeración o limita la frecuencia. |

Logs: `docker compose logs -f app` (y `llm`, `voice`, `embed`). Salud: `curl 127.0.0.1:3010/health`.

## Pruebas

```bash
for t in test_engine test_render test_memory test_consolidate test_edits test_recall \
         test_reminders test_api test_areas test_telegram_user test_library test_summaries test_books \
         test_sb0 test_brain test_brain_api test_links test_sb3 test_sb5 test_sb6; do
  docker run --rm -v "$PWD/app:/app:ro" -e PYTHONDONTWRITEBYTECODE=1 -w /app local/laika-app:0.1 python -m tests.$t
done
```

## Documentación

- [`docs/architecture.md`](docs/architecture.md): servicios, flujo de un mensaje, decisiones técnicas.
- [`docs/memory.md`](docs/memory.md): el modelo de memoria, áreas, tiempo, recordatorios, el segundo
  cerebro, Obsidian.
- [`docs/API.md`](docs/API.md): HTTP y WebSocket (lo que usan la PWA y laika-app).
- [`llama/BUILD.md`](llama/BUILD.md): compilar llama.cpp para tu GPU.
- [`CLAUDE.md`](CLAUDE.md): notas para sesiones futuras de Claude Code en este repo.

## Licencia

MIT, ver [`LICENSE`](LICENSE). Los modelos tienen sus propias licencias (Gemma, Qwen, Whisper);
revísalas antes de usarlos.

## Autoría

- **Idea, diseño y dirección: Esteban Santiago.** Él concibió a Laika (una amiga, no un asistente),
  definió cómo debe escribir y sentirse (estilo WhatsApp, sin frases de IA, memoria orgánica, áreas de
  vida, recordatorios), eligió las opciones en cada etapa, probó cada versión en su día a día y decidió
  qué se quedaba.
- **Implementación: con Claude (Anthropic), a través de Claude Code.** Claude escribió la mayor parte
  del código, las pruebas y la documentación, investigó opciones, midió rendimiento y propuso diseños,
  siempre por pasos aprobados por Esteban y registrados en una bitácora.
- Usa trabajo abierto de otros: llama.cpp, Gemma (Google), Qwen3-TTS y Qwen3-Embedding (Alibaba),
  faster-whisper, FastAPI, sqlite-vec, aiogram y más (ver `app/requirements.txt` y `voice/requirements.txt`).
