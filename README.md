# Hefesto

Framework de orquestación multiagente para trabajar con tus suscripciones de **Claude**, **ChatGPT (Codex)** y **Google (Gemini)**, sin usar la API de pago. El estado, los intentos, la verificación y los commits los gestiona la CLI `hefesto`, con lógica determinista y cubierta por tests. Ningún prompt es fuente de verdad.

> Estado: **v2.1.0 — framework multiagente determinista**. La especificación completa está en [`HEFESTO_V2_1.md`](HEFESTO_V2_1.md) y las desviaciones, en [`DECISIONS.md`](DECISIONS.md).

## Requisitos

- Python ≥ 3.11 (solo librería estándar) y git con `worktree`.
- Bash para los `verify`: en Windows, el de Git for Windows.
- Al menos una CLI con sesión iniciada: `claude`, `codex`, `gemini` o `agy`.
- Plataformas: Linux, macOS, WSL y Windows nativo (ADR-001).

## Instalación

```bash
git clone <url-de-tu-repo> ~/hefesto
bash ~/hefesto/install.sh        # en Windows, desde Git Bash
hefesto doctor
```

`install.sh` es idempotente: crea `~/.local/bin/hefesto` y, en Windows, también `hefesto.cmd` para PowerShell y cmd. Añade `~/.local/bin` al PATH si el instalador lo indica.

## Instalar en cualquier proyecto

Para usar Hefesto en cualquier proyecto (nuevo o existente), abre tu orquestador (Claude Code, Codex CLI o Gemini CLI/agy) en la carpeta del proyecto y dile:

> *"Instala Hefesto desde https://github.com/MGarcia3849/Hefesto y úsalo en este proyecto"*

El agente seguirá automáticamente las instrucciones de [`BOOTSTRAP.md`](BOOTSTRAP.md):
1. Te pedirá una única autorización inicial para confirmar la URL, la versión fijada (`v2.1.0`, nunca `main` sin fijar), los proveedores autorizados (`gemini,codex`) y la autorización para configurar el PATH si hace falta.
2. Clonará e instalará Hefesto en `~/hefesto` con `install.sh` (o actualizará el clon existente con `git fetch`/`checkout` e `install.sh`).
3. Ejecutará `hefesto doctor` para comprobar el entorno.
4. Ejecutará `hefesto bootstrap --yes --providers <lista>`, que detecta de forma determinista si debe **crear** (`init` + `/hefesto-plan`), **continuar** (`hefesto resume`) o **mejorar** (`hefesto adopt` sin tocar tu código).
5. Instala por defecto en **modo local** (dentro de `.hefesto/` y excluido en `.git/info/exclude`), dejando `git status` limpio sin ensuciar tu repositorio de GitHub.
6. Te mostrará el plan o el estado y esperará tu aprobación antes de tocar código.

## Uso en un proyecto

Por defecto, `hefesto init` instala en **modo local**: todo vive en `.hefesto/` y queda excluido en `.git/info/exclude`. No toca `.gitignore` ni deja nada que commitear, manteniendo `git status` limpio. Si prefieres versionar la configuración, el ROADMAP y las evidencias en el repositorio compartido de tu equipo, usa `hefesto init --shared`.

```bash
cd mi-proyecto
hefesto init                     # modo local por defecto; no toca .gitignore ni deja nada que commitear
# Escribe .hefesto/ROADMAP.md (o pídeselo a tu orquestador; formato en el propio fichero)
hefesto trust                    # TÚ apruebas los comandos verify (exige terminal)
hefesto next
hefesto start TASK-001           # rama hefesto/<fase>; si tienes cambios sin commitear, usa un worktree y no los toca
hefesto run runner "Implementa la tarea"      # va a Gemini → Codex → Claude haiku según cuota
hefesto run reviewer "Revisa el diff"         # proveedor distinto del autor
hefesto review TASK-001 --verdict pass --run-id <run_id del reviewer>
hefesto done TASK-001            # verify + commit + evidencia encadenada
hefesto finish                   # integra la fase en la rama principal (rebase, tests, tu confirmación)
```

### Dependencias compartidas en los worktrees (`[execution]`)

`hefesto finish` ejecuta la suite (`execution.test_command`) y los `verify` en un worktree temporal limpio que no incluye las carpetas ignoradas por git de la copia principal (como `node_modules`). Para compartir esas dependencias sin reinstalarlas, configura `execution.worktree_links` (por defecto `[]`) en `.hefesto/hefesto.toml`, con rutas relativas a la raíz del proyecto:

```toml
[execution]
worktree_links = ["sentinel-app/node_modules"]
```

- **Dónde se aplica:** en el worktree temporal de `hefesto finish`, en el worktree de la fase (`hefesto start` con aislamiento en worktree) y en los worktrees de tareas en paralelo (`hefesto parallel`).
- **Solo carpetas existentes e ignoradas por git:** crea una *junction* en Windows o un enlace simbólico (`symlink`) a directorio en el resto de plataformas únicamente si la carpeta existe en la copia principal, no existe en el worktree y git la ignora (si no está ignorada o ensucia `git status --porcelain`, el enlace se retira al instante para que nunca acabe en un commit). Las rutas absolutas, con `..`, `.git` o `.hefesto` se rechazan.
- **Borrado seguro del worktree:** `git worktree remove` entra por las *junctions* y borraría el contenido real de la carpeta enlazada en la copia principal; por eso Hefesto registra los enlaces creados y los quita siempre (`gitops.remove_worktree`) antes de borrar cualquier worktree.

## Orquestador intercambiable

Cualquiera de las tres CLIs puede conducir el ciclo. Abre una en el proyecto y lanza el paso `next`:

| Herramienta | Ciclo de una tarea | Planificar | Retro |
|---|---|---|---|
| Claude Code | `/hefesto-next` | `/hefesto-plan` | `/hefesto-retro` |
| Codex CLI | `$hefesto-next` | `$hefesto-plan` | `$hefesto-retro` |
| Gemini CLI | `/hefesto:next` | `/hefesto:plan` | `/hefesto:retro` |
| Otra | `hefesto prompt next` | `hefesto prompt plan` | `hefesto prompt retro` |

`hefesto sync` genera todo eso desde una única fuente (`prompts/protocol/`): skills y subagentes de Claude, skills en `.agents/skills/` (Codex y Gemini), comandos de Gemini y hooks de las tres CLIs. Si editas a mano un fichero generado, `sync` lo detecta y no lo sobrescribe.

### Relevo cuando se acaba la cuota

El orquestador registra su progreso con `hefesto note`. Si te quedas sin cuota en una CLI:

```bash
hefesto handoff --reason quota        # nota final, cooldown del saliente y el comando para abrir el siguiente
codex "$hefesto-next"                 # el nuevo orquestador empieza con `hefesto resume`
```

`hefesto resume` muestra la tarea, los intentos, las delegaciones ya hechas (para no repetirlas), las notas, el último error y el `git diff --stat`.

### Modo autónomo

```bash
hefesto loop --orchestrator auto --max-tasks 3 --wait
hefesto stop                          # desde otra terminal: para antes de la siguiente iteración
```

Con `auto`, si el orquestador se queda sin cuota a mitad, la siguiente iteración la retoma el siguiente de `orchestrator.use` desde `hefesto resume`, sin sumar intentos. Si el rol que toca no tiene ningún proveedor con cuota, el loop se **pausa**: sin `--wait` termina con código 3 indicando cuándo vuelve a haber cuota; con `--wait` espera, comprobando `STOP`. El loop no ejecuta nunca `trust`, `unblock` ni `finish`: son acciones humanas.

Antes de delegar, `hefesto providers check [--probe]` muestra el preflight de cada proveedor. La sonda (`--probe`, o `quota.probe = true`) gasta una llamada mínima y se cachea; confirma que el proveedor responde **ahora**, no que la cuota alcance para toda la tarea.

### Modo paralelo y concurrencia adaptativa

Hefesto permite ejecutar múltiples tareas en paralelo en fases con tareas independientes:

```bash
hefesto route --dry-run               # simula el lote paralelo previsto según la capacidad disponible
hefesto parallel                      # ejecuta el lote concurrente respetando límites globales y por modelo
hefesto parallel --max-parallel 4     # limita el número máximo de agentes concurrentes
```

- **Aislamiento en worktrees propios:** cada tarea paralela se ejecuta en un worktree independiente fuera del repositorio (`~/.hefesto/worktrees/`), evitando colisiones y manteniendo la copia principal limpia.
- **Límites de concurrencia y reparto por modelo:** respeta el límite global (`limits.max_parallel_tasks`) y el tope configurado por modelo (`limits.max_parallel_per_model`, p. ej. 3 instancias de Haiku o Gemini Flash, 1 de Sonnet).
- **Concurrencia adaptativa:** si un proveedor devuelve `rate_limit`, Hefesto reduce a la mitad la concurrencia permitida para ese modelo; tras varios éxitos consecutivos sin saturación, recupera progresivamente el nivel original.
- **Integración serial con control de conflictos:** las tareas completadas se integran una a una en la rama de la fase (`rebase` + `verify`). Si una tarea genera un conflicto de rebase con los cambios de otra tarea previa ya integrada, el scheduler no aborta: marca esa tarea como en conflicto (`[!]`) en el ROADMAP, preserva intacta su rama de trabajo para inspección o resolución manual, y continúa integrando las restantes tareas.
- `hefesto route --dry-run` sin indicar tarea muestra el plan de ejecución paralelo previsto según el estado actual, dependencias y capacidad.

### Mantenimiento y reversión: clean y discard

```bash
hefesto clean                         # detecta y limpia worktrees y ramas hefesto huérfanos sin cambios
hefesto clean --include-unmerged      # incluye también ramas temporales no fusionadas (pide confirmación)
hefesto discard TASK-003              # revierte los commits de la tarea y restaura su estado a pendiente [ ]
hefesto discard fase-1                # descarta una fase completa dejando constancia en DECISIONS
```

- `hefesto clean`: inspecciona el repositorio en busca de worktrees obsoletos o ramas `hefesto/*` huérfanas ya integradas o sin trabajo pendiente, eliminándolos de forma segura. Requiere terminal interactiva o `--yes`.
- `hefesto discard`: deshace de forma limpia el trabajo de una tarea (revierte el commit de código y el de evidencia de forma atómica) o descarta una fase entera, dejando constancia documentada de la decisión en `.hefesto/DECISIONS.md`.

### Supervisión, límites y panel de actividad

Supervisa el uso de cuota y el progreso de los agentes en tiempo real sin gastar llamadas de API:

```bash
hefesto limits                        # tabla de uso en ventanas de 5 h y semanal de cada proveedor (offline)
hefesto jobs                          # lista de agentes activos, tarea asignada y última línea de actividad
hefesto watch                         # monitor interactivo en directo (refresco configurable con --interval)
hefesto watch --once                  # captura instantánea del estado del monitor y salida inmediata
```

- `hefesto limits`: lee el porcentaje de uso real de las ventanas deslizantes de 5 horas y semanales analizando los registros locales de sesión de Codex y la caché de Claude Code (`unknown` si la CLI no lo expone, como Gemini).
- `hefesto jobs`: muestra una tabla limpia con los procesos en ejecución (`TIPO`, `ROL`, `PROVEEDOR`, `TAREA`, `TIEMPO`, `ÚLTIMA LÍNEA`), resumiendo llamadas a herramientas y salidas significativas.
- `hefesto watch`: pantalla completa dividida que combina la tabla de límites en la cabecera con la actividad detallada de los últimos logs de cada agente en marcha.
- `hefesto statusline`: comando integrado para que Claude Code muestre en su línea de estado el contexto de Hefesto y el consumo de cuota (lee JSON por stdin).

### Memoria técnica global entre proyectos

Hefesto incorpora un sistema de memoria técnica entre proyectos con revisión humana obligatoria:

```bash
hefesto remember "lección técnica aprendida"                 # guarda en .hefesto/LEARNINGS.md del proyecto
hefesto remember "lección global" --scope global             # registra propuesta en ~/.hefesto/memory/_pendientes/
hefesto remember "patrón React" --scope global --kind stacks --stack react
hefesto memory search "docker"                               # busca notas en la memoria global indexada
hefesto memory review                                        # revisa interactivamente notas pendientes globales
hefesto memory prune                                         # limpia notas duplicadas u obsoletas
```

- **Nada entra en la memoria global sin aprobación humana:** las notas añadidas con `--scope global` quedan en `~/.hefesto/memory/_pendientes/`. Solo pasan a la memoria permanente tras la validación interactiva del usuario mediante `hefesto memory review` (requiere TTY o `--yes`).
- **Inyección contextual relevante:** `hefesto resume` y el paquete de contexto de la tarea (`packet`) incluyen de forma automática las notas más relevantes según el stack y la tarea actual, sin desbordar los tokens disponibles. Los agentes únicamente tienen acceso de lectura a la memoria global.

## Comandos

| Comando | Qué hace |
|---|---|
| `init [dir] [--shared] [--providers …]` | Inicializa sin sobrescribir nada; modo local por defecto; ejecuta `doctor`. |
| `bootstrap [dir] [--yes] [--providers LISTA]` | Detecta modo (crear, continuar o mejorar) y arranca en modo local. |
| `adopt [dir] [--providers LISTA]` | Adopta un proyecto existente sin modificar código; propone ROADMAP. |
| `self-update [--to TAG]` | Actualiza a un tag oficial mostrando el changelog con confirmación. |
| `migrate` | Migra proyectos antiguos aplicando copias de seguridad automáticas. |
| `doctor` | Dependencias, CLIs, autenticación, recursos (CPU/RAM), allowlist, lock y estado. |
| `config validate` / `config show [--json]` | Valida las capas y muestra la configuración efectiva con el origen de cada valor. |
| `status [--protocol]` / `next` | Estado compacto y siguiente tarea disponible. |
| `start TASK` / `fail TASK "motivo"` | Inicia una tarea (con lock y aislamiento git) o registra un intento fallido; al 3.º salta el circuit breaker. |
| `trust [--yes]` / `unblock TASK "nota" [--yes]` | Solo humanos y con terminal: aprobar `verify` y desbloquear tareas. |
| `run ROL "…" [--task] [--dry-run] [--challenge]` | Delegación con paquete de contexto, cadena de fallback, cooldowns y crítica previa opcional (`--challenge`). |
| `packet TASK [--role] [--model]` / `route [TASK] --dry-run` | Paquete que recibiría el agente y ruta prevista con las skills del paquete (o lote paralelo sin tarea). |
| `parallel [--max-parallel N]` | Ejecuta tareas en paralelo respetando concurrencia y límites por modelo. |
| `review TASK --verdict pass\|fail` | Veredicto con regla de independencia (proveedor distinto, o modelo superior). |
| `done TASK` / `finish [FASE] [--pr] [--yes]` | Aceptación y commit; integración de la fase en la rama principal. |
| `evidence TASK` / `evidence verify` | Evidencia de una tarea; integridad de la cadena de hashes SHA-256. |
| `clean [--yes] [--include-unmerged]` | Elimina worktrees y ramas hefesto huérfanos sin cambios. |
| `discard TARGET [--yes]` | Revierte una tarea o descarta una fase dejando constancia en DECISIONS. |
| `providers status\|check\|add\|remove\|reset` | Proveedores, allowlist del proyecto, preflight y cooldowns. |
| `local enable\|disable` | Modelo local compatible con OpenAI (desactivado por defecto). |
| `sync [--check]` / `prompt plan\|next\|retro` | Materializa el protocolo para las tres CLIs / imprime un paso. |
| `note "…"` / `resume [--json]` / `handoff` | Progreso continuo y relevo entre orquestadores. |
| `loop [--orchestrator …] [--max-tasks N] [--wait]` / `stop` | Ciclo headless con topes, relevo automático y parada limpia. |
| `skill [find\|fetch\|update\|suggest\|auto\|sync\|TÉRMINO]` | Busca y gestiona skills locales y del registro público con informe de riesgos; sincroniza el catálogo en sus commits fijados (`hefesto skill sync`), sugiere (`skill suggest TASK`) o instala automáticamente (`skill auto TASK`) skills para una tarea. |
| `promote NAME [--yes]` | Promueve una propuesta de skill a promovidas. |
| `pack list\|enable\|disable\|update` | Catálogo y gestión de packs externos curados. |
| `limits [--json]` | Uso real de ventanas de 5 h y semanal de cada proveedor. |
| `statusline` | Comando de línea de estado para Claude Code (lee JSON por stdin). |
| `jobs [--json]` | Muestra los agentes activos y la última línea de su log de forma legible. |
| `watch [--once] [--interval N]` | Monitor de límites y actividad de agentes en directo. |
| `remember "…" [--scope …]` | Guarda una nota en la memoria local o propuesta global. |
| `memory review\|search\|prune` | Gestión y revisión humana de la memoria global entre proyectos. |
| `learn extract\|skills` | Extrae lecciones candidatas desde evidencias, revisiones y fallos; propone globales y borradores de skills. |
| `notify test` | Envía un aviso de escritorio de prueba si `[notify] enabled = true`. |
| `autopilot schedule\|status\|cancel` | Programa, consulta o cancela una ejecución única de `hefesto loop` al fin del cooldown. |

Códigos de salida: `0` correcto · `1` entrada inválida o `verify` en rojo con intentos restantes · `2` circuit breaker o barrera de seguridad · `3` dependencia o proveedor no disponible · `4` conflicto de estado o lock.

## Protección real según el orquestador

| Orquestador | Protección |
|---|---|
| Claude Code | Hooks `SessionStart` y `PreToolUse` + reglas `deny` de permisos + comprobaciones de la CLI. |
| Codex CLI | Hooks `SessionStart` y `PreToolUse` (`.codex/hooks.json`) + sandbox `workspace-write` sin red (`.codex/config.toml`; `read-only` para revisar) + comprobaciones de la CLI. |
| Gemini CLI | Hooks `SessionStart` y `BeforeTool` (`.gemini/settings.json`) + modo de aprobación (`auto_edit`/`plan`) + comprobaciones de la CLI. |
| agy | Solo el modo (`accept-edits`/`plan`) + comprobaciones de la CLI: sin hooks verificados. |

Las comprobaciones de la CLI (`guard.py`) se aplican siempre, pase lo que pase en el agente: rama `hefesto/*`, `git add` limitado a los `files` de la tarea, secretos por nombre y por contenido, y binarios grandes.

## Cuota

- Hefesto no inventa cuota restante: si una CLI no la expone, el dato es `unknown`.
- Ante `quota` o `rate_limit`, el proveedor entra en cooldown (hasta la hora de reinicio si la CLI la indica, o 60 minutos) y la petición pasa al siguiente de la cadena **sin gastar intento**.
- Las variables `*_API_KEY` se eliminan del entorno de las CLIs hijas, para que facturen por suscripción, y `doctor` avisa si existen.

## Skills del framework

`sync` instala en cada proyecto (como `hefesto-skill-<nombre>`): `git-checkpointer`, `test-runner`, `skill-creator`, `retro`, `frontend-taste`, `motion-system` y `ui-anatomy`. Las propuestas nuevas van a `skills/_proposals/` y nunca se activan solas.

### Skills en el paquete de tarea

Al delegar una tarea, Hefesto selecciona de forma determinista (sin LLM) las skills locales aplicables y las inyecta directamente en el paquete del trabajador:

- **Orígenes y orden de preferencia:** se descubren skills en las carpetas del **proyecto** (`.claude/skills` y `.agents/skills`), **promovidas** (`~/hefesto/skills/promoted`), **framework** (`skills/`) y **usuario** (`skills_selection.user_roots`, por defecto `["~/.claude/skills", "~/.codex/skills"]`). Al deduplicar por nombre, prevalece el primer origen en ese orden (`proyecto` > `promovida` > `framework` > `usuario`). Las skills de protocolo (`hefesto-next`, `hefesto-plan`, `hefesto-retro`) y las listadas en `exclude` nunca se seleccionan.
- **Cómo se eligen:** se extraen palabras clave del título de la tarea, el criterio `accept`, las extensiones de `files` (`.component.ts` → `angular`, `.ts` → `typescript`, `.tsx`/`.jsx` → `react`, `.css`/`.scss` → `css`, `.html` → `html`, `.py` → `python`, `.dart` → `flutter`) y el `role`. La coincidencia es siempre por palabra entera (nunca subcadena) y cada palabra clave suma una sola vez, en el primer campo donde casa: `name` (`+3`), si no `tags` (`+2`), si no `description` (`+1`) —no se acumulan entre campos—. Entran las que alcanzan al menos `min_score`, ordenadas por puntuación descendente hasta completar los huecos que dejen las fijadas hasta `max_skills`.
- **Inyección en el paquete:** el cuerpo de cada `SKILL.md` elegida entra saneado (sin bytes nulos ni secuencias de control), con secretos redactados (`[REDACTED]`) y recortado a `max_chars_per_skill` bajo la sección `## Skills que aplican (síguelas)` del paquete, de modo que cualquier proveedor las siga aunque no tenga acceso fuera del repositorio. Ante límite de tokens del paquete, esta sección se recorta después de la memoria y antes que los ficheros de la tarea; si no hay skills seleccionadas, la sección se omite.
- **Fijar skills en el ROADMAP:** una tarea puede declarar el campo opcional `- skills: a, b`. Las skills fijadas así entran siempre en primer lugar aunque no puntúen por palabras clave (e incluso si `skills_selection.enabled = false`) y aunque superen `max_skills`, salvo que figuren en `exclude`. Como `max_skills` es el tope total, las automáticas solo ocupan los huecos libres que dejan las fijadas (por ejemplo, con `max_skills = 3` y 2 fijadas entra como mucho 1 automática). Si una skill fijada no está instalada, se ignora sin fallar.

| Clave de `[skills_selection]` | Por defecto | Descripción |
|---|---|---|
| `enabled` | `true` | Activa la selección automática por palabras clave (si es `false`, solo entran las fijadas con `- skills:`). |
| `max_skills` | `3` | Número máximo total de skills en el paquete: las automáticas solo ocupan los huecos que dejan las fijadas (las fijadas entran todas aunque superen `max_skills`). |
| `min_score` | `2` | Puntuación mínima para que una skill entre por selección automática. |
| `max_chars_per_skill` | `6000` | Límite de caracteres por cuerpo de skill antes de añadir `[skill recortada]`. |
| `user_roots` | `["~/.claude/skills", "~/.codex/skills"]` | Carpetas de usuario adicionales donde descubrir skills instaladas. |
| `exclude` | `[]` | Lista de nombres de skills vetadas en el proyecto (ni automáticas ni fijadas). |

```bash
hefesto skill suggest TASK-001            # muestra las skills locales seleccionadas, su origen, puntuación y motivos
hefesto skill suggest TASK-001 --online   # propone además candidatas de skills.sh según las palabras clave de la tarea
hefesto route TASK-001 --dry-run          # muestra el proveedor/modelo previsto y las skills que llevará el paquete
```

`hefesto skill suggest TASK --online` **solo propone**: consulta el registro público con las palabras clave de la tarea y muestra cada candidata con su nivel de confianza y el comando `hefesto skill fetch <id>` correspondiente.

### Instalación automática de skills

Cuando las skills instaladas no bastan para cubrir una tarea, Hefesto puede buscar, auditar, descargar y promover automáticamente desde `skills.sh` las skills necesarias (sustituyendo a la regla de promoción solo humana únicamente para este circuito acotado, ADR 2026-10-16):

- **Cuándo y qué busca:** se activa automáticamente al ejecutar `hefesto start TASK` y bajo demanda con `hefesto skill auto TASK`, **solo si ninguna de las skills que la selección elige para la tarea (las mismas que muestra `hefesto skill suggest TASK`) alcanza `good_score`**. Toma las **dos primeras palabras clave** de la tarea y busca en `skills.sh`, sin repetir la misma búsqueda dentro de la ventana `search_ttl_hours`.
- **Filtros de calidad y límites:** exige que cada candidata alcance al menos `min_installs` y `min_stars` (cualquier dato desconocido o `unknown` descarta la candidata), omite las skills ya instaladas (y las que empiezan por `hefesto-`) y ordena las restantes por instalaciones y estrellas descendentes. En cada ejecución (`start` o `skill auto`) intenta como máximo **3 descargas** (`fetch`) y hasta `max_per_task` instalaciones (límite por invocación, no total acumulado de la tarea).
- **Informe de riesgos y promoción:** cada candidata se descarga con `skills.fetch` a `skills/_proposals/`. Si el informe de riesgos detecta cualquier riesgo (en `SOURCE.json` o `SKILL.md`), **se descarta y se borra su propuesta** inmediatamente. Si está limpia de riesgos, se promueve sin preguntar a `~/hefesto/skills/promoted`, desde donde entra sola en la selección del paquete de la tarea.
- **Seguridad, auditoría y desactivación:** nunca ejecuta `npx skills add` ni código incluido en la skill; un trabajador delegado (`HEFESTO_ROLE` en el entorno) **nunca instala**; ningún fallo de red, `npx` o `git` hace fallar la tarea; y todas las búsquedas, instalaciones, descartes y errores quedan registrados en `~/hefesto/skills/auto.jsonl` (`skills/auto.jsonl` dentro de `HEFESTO_HOME`). Para desactivarlo, configura `skills_auto.enabled = false`.

| Clave de `[skills_auto]` | Por defecto | Descripción |
|---|---|---|
| `enabled` | `true` | Activa la búsqueda e instalación automática de skills (`skills_auto.enabled = false` la desactiva). |
| `min_installs` | `1000` | Mínimo de instalaciones requerido en `skills.sh` (un valor desconocido descarta la candidata). |
| `min_stars` | `100` | Mínimo de estrellas en GitHub requerido (un valor desconocido descarta la candidata). |
| `max_per_task` | `1` | Número máximo de skills que pueden instalarse automáticamente en cada ejecución (`start` o `skill auto`), no el total acumulado de la tarea (hasta 3 intentos de descarga por ejecución). |
| `good_score` | `6` | Puntuación a partir de la cual se considera que alguna de las skills que la selección elige para la tarea (las mismas que muestra `hefesto skill suggest`) basta y se omite la búsqueda en red. |
| `search_ttl_hours` | `24` | Horas durante las que no se repite en `skills.sh` una misma búsqueda ya registrada en `skills/auto.jsonl`. |

```bash
hefesto skill auto TASK-001               # ejecuta bajo demanda la búsqueda e instalación automática para la tarea
```

### Catálogo de skills por rol y modo (`catalog/skills.toml`)

El catálogo único [`catalog/skills.toml`](catalog/skills.toml) declara las skills y herramientas curadas de Hefesto y cómo se reparten entre roles, modos y tipos de tarea (`hefesto/skill_catalog.py`, `hefesto/skill_select.py`):

- **Las skills no pertenecen a ningún proveedor:** el orquestador decide qué agente o subagente usa cada skill o herramienta, en qué modo y para qué; dónde esté instalada una skill en disco (`.claude/skills`, `.agents/skills`, etc.) no decide quién la usa. El campo `providers` es únicamente una preferencia de orden cuando el router elige ese proveedor (p. ej. `full-output-enforcement` con `gemini` o `gpt-taste` con `codex`), nunca propiedad ni exclusividad (`provider_only` y `exclusive` están prohibidos).
- **Campos de una entrada (`[skills.<nombre>]`):**
  - `kind`: `"skill"` (documento `SKILL.md` descargable) o `"tool"` (binario externo).
  - `source`: repositorio `"owner/repo"`, acompañado en toda `skill` de un `commit` fijado (hash SHA-1 de 40 hex en minúsculas; nunca `main` ni ramas móviles) y un `path` relativo portable opcional.
  - `license`: licencia permisiva obligatoria (`MIT`, `Apache-2.0`, `BSD-2-Clause`, `BSD-3-Clause`, `ISC`, `0BSD`, `CC0-1.0`; se rechazan `AGPL`, `GPL` y propietarias).
  - `roles`: subconjunto de `core`, `runner`, `forge`, `visual`, `reviewer` y `verify`.
  - `modes`: tabla opcional `[skills.<nombre>.modes]` que asigna a cada rol la lista de modos concretos que debe aplicar (un rol sin entrada en `modes` aplica la skill entera).
  - `kinds`: tipos de tarea a los que aplica (`app`, `web`, `backend`, `docs`, `infra`); si se omite o va vacía, aplica a todos.
  - `interactive`: modos que exigen conversación directa con un humano.
  - `providers`: lista opcional de proveedores que priorizan la entrada en el orden del paquete sin excluir al resto.
  - `binary`: nombre del ejecutable requerido en el `PATH` (obligatorio en `kind = "tool"`, prohibido `npx`).
- **Campo `kind` en las tareas del ROADMAP:** cada tarea puede declarar opcionalmente `- kind: app | web | backend | docs | infra` (un valor fuera de esos cinco es error de validación en `roadmap.validate`). Si se omite, `roadmap.infer_kind(files)` lo deduce de forma determinista clasificando cada fichero de `files` (`.component.*`, `.tsx`, `.jsx`, `.vue`, `.svelte` → `app`; `.html`, `.css`, `.scss` sueltos → `web`; `.md` o rutas en `docs/` → `docs`; `Dockerfile`, `.github/`, `*.yml`/`*.yaml` → `infra`; el resto → `backend`) por mayoría simple y desempate en orden `app > web > backend > infra > docs` (sin ficheros → `backend`).
- **Puertas interactivas y aislamiento de `verify`:** los modos declarados en `interactive` (`init`, `shape`, `live` y `generate` de `impeccable`) están reservados en exclusiva al rol `core` trabajando con un humano. A los trabajadores delegados (`runner`, `forge`, `visual`, `reviewer`) `skill_select.from_catalog` les filtra siempre esos modos interactivos y los modos de `verify`, y el paquete de contexto les indica explícitamente qué modos tienen asignados (`modos: …`) y, cuando la skill está instalada o el rol la recibe entera, emite el aviso `modos prohibidos para este rol (exigen un humano o son de verify): X — no los ejecutes`. Las herramientas y modos del rol `verify` (como `detect` en `impeccable` o el binario `agentshield`) nunca entran en un paquete de tarea. En `impeccable` (flujo v4), `craft` (deprecado en v4), `overdrive` y los hooks quedan fuera del catálogo.
- **Visibilidad en el enrutado (`hefesto route TASK --dry-run`):** explica para cada rol de la ruta prevista (tanto el rol de implementación de la tarea como el `reviewer`) qué entradas del catálogo y qué modos concretos viajarán en su paquete (mostrando `toda la skill (prohibidos: …)` cuando el rol recibe la skill entera con modos prohibidos).
- **Sincronización desde commits fijados (`hefesto skill sync`):** recorre las entradas `kind = "skill"` de `catalog/skills.toml`, descarga cada una en su commit exacto mediante `skills.fetch("<source>@<name>", commit=commit)` a `~/hefesto/skills/_proposals/` junto con su informe de riesgos (`SOURCE.json` y verificación de identidad de `SKILL.md`), y la promueve automáticamente a `~/hefesto/skills/promoted` (`skills.promote(..., yes=True)`) **solo si** el commit descargado coincide con el fijado en el catálogo, no hay riesgos y AgentShield (si está disponible) no reporta hallazgos altos. Si el commit no coincide o aparece cualquier riesgo, borra la propuesta y rechaza la skill (cambiar de versión exige actualizar el hash en `catalog/skills.toml`). Nunca ejecuta `npx skills add`, instaladores externos ni código incluido en la skill; las entradas `kind = "tool"` se instalan aparte y `skill sync` las omite. El pack `design` de `catalog/packs.toml` referencia exactamente los seis nombres verificados de `Leonxlnx/taste-skill`.
- **AgentShield opcional (`agentshield`):** si el binario `agentshield` está instalado en el `PATH` (nunca se invoca vía `npx`), tanto `hefesto skill sync` como la instalación automática (`skill_auto.ensure`) ejecutan `[agentshield, "scan", "--path", <ruta>, "--format", "json"]` con `shell=False` antes de promover una skill y la descartan si aparece cualquier hallazgo con severidad `high` o `critical` (incluso si el binario devuelve código distinto de `0`). El análisis **falla cerrado**: una salida no JSON, sin lista `findings`, un error sin salida, `OSError` o un timeout se tratan como un hallazgo `high` y bloquean la promoción. `hefesto doctor` incluye el check opcional `agentshield` (no bloqueante si falta) y, cuando está instalado, sugiere auditar la configuración del proyecto con `agentshield scan --path .claude`.

| Entrada (`catalog/skills.toml`) | Tipo y origen | `kinds` | Roles y modos (`rol: modos` o skill entera) | Notas / preferencia |
|---|---|---|---|---|
| `impeccable` | `skill` · `pbakaus/impeccable` | `app`, `web` | `core`: `init`, `shape`, `document`, `critique`, `audit`, `live`, `generate`<br>`visual`: `document`, `extract`, `typeset`, `layout`, `colorize`, `harden`, `optimize`, `adapt`, `clarify`, `onboard`, `distill`, `quieter`, `bolder`, `animate`, `delight`, `polish`<br>`reviewer`: `critique`, `audit`<br>`verify`: `detect` | `interactive`: `init`, `shape`, `live`, `generate` (solo `core`); `craft` fuera (v4). |
| `web-design-guidelines` | `skill` · `vercel-labs/agent-skills` | `app`, `web` | `reviewer`, `visual` (skill entera) | Guías de interfaz web. |
| `playwright-cli` | `skill` · `microsoft/playwright-cli` | `app`, `web` | `visual`, `reviewer`, `verify` (skill entera) | `binary = "playwright-cli"`. |
| `design-taste-frontend`<br>`redesign-existing-projects`<br>`minimalist-ui`<br>`high-end-visual-design`<br>`gpt-taste`<br>`full-output-enforcement` | `skill` · `Leonxlnx/taste-skill` (6) | `app`, `web` (las 5 primeras); todos en `full-output-enforcement` | `design-taste-frontend`: `core`, `visual`<br>`redesign-existing-projects`, `minimalist-ui`, `high-end-visual-design`, `gpt-taste`: `visual`<br>`full-output-enforcement`: `runner`, `forge`, `visual` | Preferencia no exclusiva: `gpt-taste` (`codex`), `full-output-enforcement` (`gemini`). Pack `design`. |
| `angular-developer`<br>`frontend-a11y`<br>`security-review`<br>`tdd-workflow`<br>`python-patterns`<br>`python-testing`<br>`e2e-testing`<br>`verification-loop` | `skill` · `affaan-m/ECC` (8) | `angular-developer`: `app`<br>`frontend-a11y`, `e2e-testing`: `app`, `web`<br>`python-patterns`, `python-testing`: `backend`<br>`security-review`, `tdd-workflow`, `verification-loop`: todos | `angular-developer`: `runner`, `forge`, `visual`<br>`frontend-a11y`: `visual`, `reviewer`<br>`security-review`: `reviewer`, `forge`<br>`tdd-workflow`: `core`, `runner`, `forge`<br>`python-patterns`: `runner`, `forge`<br>`python-testing`: `core`, `runner`, `forge`<br>`e2e-testing`: `runner`, `visual`<br>`verification-loop`: `runner`, `forge` | Skills de ingeniería y pruebas de ECC en commit fijado. |
| `agentshield` | `tool` · `affaan-m/agentshield` | todos | `verify` | `binary = "agentshield"` (nunca entra en paquetes; nunca vía `npx`). |

```bash
hefesto skill sync                        # descarga y promueve las skills del catálogo en su commit fijado
hefesto route TASK-001 --dry-run          # explica por rol qué skills y modos del catálogo irán en el paquete
```

## Aprendizaje

Hefesto incorpora un circuito determinista de aprendizaje continuo (sin llamadas a LLM para extraer ni agrupar) donde **nada entra en la memoria aprobada ni se promueve a skill activa sin revisión humana**:

```bash
hefesto learn extract                         # extrae candidatas desde evidencias, revisiones y logs/failures.jsonl
hefesto memory review                         # aprueba o descarta interactivamente las candidatas en memory/_pendientes/
hefesto learn skills                          # propone candidatas globales (2+ proyectos) y borradores en skills/_proposals/
hefesto promote <nombre-propuesta>            # promueve manualmente un borrador de skill tras revisarlo
hefesto route TASK-001 --dry-run              # muestra la ruta y las tasas pass@1 / pass@k si [routing] history = true
hefesto run runner "Implementa…" --challenge  # pide antes a otro proveedor una crítica en solo lectura de la instrucción
hefesto notify test                           # comprueba los avisos de escritorio ([notify] enabled = true)
hefesto autopilot schedule                    # programa una única ejecución de `hefesto loop` al fin del cooldown
hefesto autopilot status                      # consulta la programación vigente del proyecto (hora local HH:MM)
hefesto autopilot cancel                      # retira la tarea programada del sistema (schtasks / crontab) y del registro
```

- **Extractor determinista de lecciones (`hefesto learn extract`):** cada fallo de tarea (`fail`, `verify` o `review`) se registra con el error redactado en `.hefesto/logs/failures.jsonl` (`workflow.register_failure`). `hefesto learn extract` recorre `.hefesto/evidence/*.json`, `.hefesto/logs/reviews.jsonl` y `.hefesto/logs/failures.jsonl` y escribe o actualiza notas candidatas en `~/hefesto/memory/_pendientes/` para las tareas que requirieron más de un intento (`attempts > 1`), tuvieron algún `verify` fallido o recibieron una revisión `fail`. Cada candidata resume el `Motivo` (notas de revisión y errores registrados) y la `Solución` (título de la tarea que acabó en verde y «`N` intentos»), con secretos redactados, y lleva en el frontmatter `confianza` (`0.3` al nacer, `+0.1` por cada nueva fuente `<proyecto>:<TASK>` que comparta la misma `clave` —hash estable de los términos del motivo— hasta el tope `0.9`; una fuente ya contada no vuelve a subir la confianza), `alcance` (`proyecto` o `global`), `proyecto`, `stack`, `tipo` (`errores-resueltos`), `tarea`, `fuentes` y `clave`. Nunca modifica notas ya aprobadas y nada pasa a la memoria permanente sin `hefesto memory review`.
- **Promoción a global y borradores de skills (`hefesto learn skills`):**
  - *Propuestas globales (`learning.global_candidates`):* cuando una misma lección (`clave`) ya aprobada con `alcance: proyecto` aparece en **2 o más proyectos distintos**, genera una candidata con `alcance: global` en `~/hefesto/memory/_pendientes/` para su revisión humana con `hefesto memory review`.
  - *Borradores de skills (`learning.skill_drafts`):* agrupa notas aprobadas del mismo `stack` con `confianza >= 0.7` que comparten un término clave en al menos 3 lecciones (`min_repeats = 3`, `min_confidence = 0.7`) y genera un borrador en `~/hefesto/skills/_proposals/<stack>-<termino>/SKILL.md` con frontmatter `name` y `description`. Ignora notas en `_pendientes/`, no reescribe borradores existentes y **nunca promueve automáticamente** (la promoción sigue siendo `hefesto promote`).
- **Lecciones relevantes y reglas del stack en el paquete (`packet.py`):**
  - *Puntuación de memoria local (`_memory_entries`):* puntúa cada bloque de `.hefesto/DECISIONS.md` y `.hefesto/LEARNINGS.md` por coincidencias con las rutas de `files` de la tarea (`+4`), etiquetas `#etiqueta` que casen con términos del título o nombres de ruta (`+2`) y términos del título (`+1`). Ordena de mayor a menor puntuación e inyecta los mejores en el paquete bajo `## Decisiones y lecciones relevantes` sin superar `memory.max_chars` (por defecto `2000`).
  - *Reglas por lenguaje (`stack_rules`, `[packet]`):* si `packet.rules_roots` apunta a carpetas de reglas por lenguaje (p. ej. una carpeta `rules/` estilo ECC con subcarpetas `common/`, `angular/`, `react/`, `typescript/`…), el paquete incluye bajo `## Reglas del stack` los `.md` de `common` y de las subcarpetas que coincidan con el stack detectado del proyecto (`distribution.detect_stack` y dependencias de `package.json`: `@angular/core` → `angular`, `react` → `react`, `vue` → `vue`, `typescript` → `typescript`, `next` → `nextjs`), saneados, con secretos redactados, sin seguir enlaces simbólicos ni junctions internos y dentro del presupuesto `packet.rules_max_chars` (por defecto `4000`).
- **Enrutado por historial con `pass@k` (`[routing]`, `router.py`):** con `routing.history = true` (por defecto `false`), `plan_route` calcula desde `.hefesto/logs/runs.jsonl` las estadísticas por `model_id` y rol (`router.success_stats`): tareas distintas (`tasks`), `pass@1` (fracción de tareas cuya primera ejecución del modelo tuvo éxito) y `pass@k` (fracción de tareas con alguna ejecución con éxito). Los modelos de la cadena del rol con `tasks >= routing.min_samples` (por defecto `5`) se reordenan entre las posiciones que ya ocupaban por `(pass@k, pass@1)` descendente; el resto conserva su sitio. Nunca se salta cooldowns, la allowlist del proyecto ni la regla de independencia del `reviewer` (el autor del cambio no puede revisarlo si hay otro proveedor disponible). `hefesto route TASK --dry-run` muestra las cifras junto a cada salto (`pass@1 X%, pass@k Y%, N tareas`).
- **Revisión adversarial de la instrucción (`hefesto run ROL "…" --challenge`):** antes de delegar al proveedor elegido, un proveedor distinto disponible (ejecutado en solo lectura con `HEFESTO_ROLE=challenge`) señala ambigüedades, lagunas y riesgos de la instrucción frente a los criterios de la tarea. La crítica redactada se guarda en `.hefesto/logs/challenges/<run_id>-<proveedor>.md`, se imprime en `stderr`, se devuelve en el JSON de salida (`challenge: {provider, path, summary}`) y se añade a la instrucción del trabajador bajo `## Crítica previa (otro proveedor)` antes de construir su paquete. **Nunca cuenta como intento** de la tarea; si no hay otro proveedor disponible (`challenge: null`) o la crítica falla, avisa y delega igual con la instrucción original.
- **Avisos de escritorio (`[notify]`, `notify.py`):** con `notify.enabled = true` (por defecto `false`), Hefesto lanza avisos de escritorio (globo `NotifyIcon` mediante PowerShell en Windows, `osascript` en macOS, `notify-send` en Linux; siempre con `shell=False` y sin bloquear ni lanzar excepción si falla) cuando ocurre un bloqueo por circuit breaker (`blocked`), un `verify` sin confiar en `done` (`trust`), una fase completada lista para `hefesto finish` (`finish`) o una pausa por agotamiento de cuota con código `3` (`quota`). `hefesto notify test` comprueba el envío.
- **Piloto automático con el planificador del sistema (`hefesto autopilot`):** `hefesto autopilot schedule` toma la hora del próximo reinicio de cuota (`earliest_cooldown`) y programa **una única ejecución** de `hefesto loop` en la hora local correspondiente usando `schtasks` (`/SC ONCE` con `/TN hefesto-autopilot-<hash>` y `/F`) en Windows o `crontab` (línea marcada con `# hefesto-autopilot <nombre>` que ejecuta `hefesto -C <proyecto> loop` seguido de `hefesto -C <proyecto> autopilot cancel` para retirarse sola tras correr una vez, con rutas citadas con `shlex.quote`) en Linux/macOS, sin dejar ningún daemon residente y ejecutando siempre el planificador como lista de argumentos (`shell=False`). Se niega con código `2` (`EXIT_BLOCKED`) si el proyecto o alguna tarea están bloqueados o si el `verify` de la siguiente tarea no está confiado; si no hay cooldown activo, no programa nada. `hefesto autopilot status` muestra la programación vigente y `hefesto autopilot cancel` la retira del planificador del sistema y de `.hefesto/logs/autopilot.json`.

| Sección y clave | Por defecto | Descripción |
|---|---|---|
| `memory.max_chars` | `2000` | Tope total de caracteres de las entradas de `DECISIONS.md` y `LEARNINGS.md` inyectadas en el paquete. |
| `packet.rules_roots` | `[]` | Lista de carpetas con reglas por stack (p. ej. `["~/rules"]` con subcarpetas `common`, `angular`, `react`…). |
| `packet.rules_max_chars` | `4000` | Presupuesto máximo de caracteres para la sección `## Reglas del stack` en el paquete. |
| `routing.history` | `false` | Reordena los modelos cualificados de la cadena del rol por `(pass@k, pass@1)` según `logs/runs.jsonl`. |
| `routing.min_samples` | `5` | Mínimo de tareas distintas por `model_id` y rol para reordenar por historial. |
| `notify.enabled` | `false` | Activa los avisos de escritorio en eventos `blocked`, `trust`, `finish` y `quota`. |

## Desarrollo

```bash
python tools/dev.py lint test smoke      # o, con make en entornos POSIX: make lint test smoke
python tools/dev.py demo                 # demostración completa con fakes: ciclo serie + paralelo con conflicto
make smoke-live                          # opcional: llamadas reales mínimas; gasta cuota y pide confirmación
```

Los tests usan CLIs falsas (`tests/fakes/`) y repositorios temporales: no llaman a ningún proveedor real ni gastan cuota.
- `python tools/dev.py demo`: ejecuta en un entorno temporal aislado la demostración completa con fakes exigida para la entrega de v2.1. Primero comprueba el ciclo serie completo (start → runner → reviewer → verify → done → evidence verify), y después una fase de 3 tareas en paralelo donde una de ellas sufre un conflicto intencionado de rebase, verificando que los runners trabajan solapados, la rama en conflicto se aísla sin corromper la fase y la cadena de evidencias se mantiene íntegra.

## Publicar Hefesto

La integración de cada fase en la rama principal configurada del proyecto la realiza `hefesto finish` (tras verificar tests, precondiciones y solicitar confirmación).

Sin embargo, **publicar (push, tag, release) es siempre una acción humana**: el framework nunca ejecuta `git push` de forma autónoma.

Para publicar una nueva versión una vez integradas las fases:

1. Asegúrate de estar en la rama principal (por ejemplo `main`):
   ```bash
   git checkout main
   ```
2. Sube la rama principal al repositorio remoto:
   ```bash
   git push origin main
   ```
3. Crea y sube la etiqueta anotada de la versión:
   ```bash
   git tag -a v2.1.0 -m "Hefesto v2.1.0"
   git push origin v2.1.0
   ```
4. Publica la release en GitHub utilizando únicamente la sección correspondiente a dicha versión en `CHANGELOG.md` (no pases el changelog entero):
   ```bash
   # Extrae solo la sección de la versión a un fichero temporal de notas
   gh release create v2.1.0 --title "Hefesto v2.1.0" --notes-file <fichero-con-seccion-v2.1.0>
   ```

*(Solo si aún no tienes repositorio remoto)*:
```bash
gh repo create <usuario>/Hefesto --private --source . --push
```
