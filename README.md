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
| `run ROL "…" [--task] [--dry-run]` | Delegación con paquete de contexto, cadena de fallback y cooldowns. |
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
| `skill [find\|fetch\|update\|suggest\|auto\|TÉRMINO]` | Busca y gestiona skills locales y del registro público con informe de riesgos; sugiere (`skill suggest TASK`) o instala automáticamente (`skill auto TASK`) skills para una tarea. |
| `promote NAME [--yes]` | Promueve una propuesta de skill a promovidas. |
| `pack list\|enable\|disable\|update` | Catálogo y gestión de packs externos curados. |
| `limits [--json]` | Uso real de ventanas de 5 h y semanal de cada proveedor. |
| `statusline` | Comando de línea de estado para Claude Code (lee JSON por stdin). |
| `jobs [--json]` | Muestra los agentes activos y la última línea de su log de forma legible. |
| `watch [--once] [--interval N]` | Monitor de límites y actividad de agentes en directo. |
| `remember "…" [--scope …]` | Guarda una nota en la memoria local o propuesta global. |
| `memory review\|search\|prune` | Gestión y revisión humana de la memoria global entre proyectos. |

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
