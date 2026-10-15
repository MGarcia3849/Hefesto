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
| `packet TASK [--role] [--model]` / `route [TASK] --dry-run` | Paquete que recibiría el agente y ruta prevista (o lote paralelo sin tarea). |
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
| `skill [find\|fetch\|update\|TÉRMINO]` | Busca y gestiona skills locales y del registro público con informe de riesgos. |
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
