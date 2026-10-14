# Hefesto

Framework de orquestación multiagente para trabajar con tus suscripciones de **Claude**, **ChatGPT (Codex)** y **Google (Gemini)**, sin usar la API de pago. El estado, los intentos, la verificación y los commits los gestiona la CLI `hefesto`, con lógica determinista y cubierta por tests. Ningún prompt es fuente de verdad.

> Estado: **Hito 3 — distribución y arranque en cualquier proyecto** (`v2.1.0`). La especificación completa está en [`HEFESTO_V2_1.md`](HEFESTO_V2_1.md) y las desviaciones, en [`DECISIONS.md`](DECISIONS.md).

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

## Comandos

| Comando | Qué hace |
|---|---|
| `init [dir] [--copy] [--shared] [--providers …]` | Inicializa sin sobrescribir nada; modo local por defecto; ejecuta `doctor`. |
| `bootstrap [dir] [--yes] [--providers LISTA]` | Detecta modo (crear, continuar o mejorar) y arranca en modo local. |
| `adopt [dir] [--providers LISTA]` | Adopta un proyecto existente sin modificar código; propone ROADMAP. |
| `self-update [--to TAG]` | Actualiza a un tag oficial mostrando el changelog con confirmación. |
| `migrate` | Migra proyectos antiguos aplicando copias de seguridad automáticas. |
| `doctor` | Dependencias, CLIs, autenticación (sin gastar llamadas), API keys en el entorno, allowlist, lock y estado. |
| `config validate` / `config show` | Valida las capas y muestra la configuración efectiva con el origen de cada valor. |
| `status` / `next` | Estado compacto y siguiente tarea disponible. |
| `start TASK` / `fail TASK "motivo"` | Inicia una tarea (con lock y aislamiento git) o registra un intento fallido; al 3.º salta el circuit breaker. |
| `trust` / `unblock TASK "nota"` | Solo humanos y con terminal: aprobar `verify` y desbloquear tareas. |
| `run ROL "…" [--task] [--dry-run]` | Delegación con paquete de contexto, cadena de fallback y cooldowns. |
| `packet TASK` / `route TASK --dry-run` | Paquete que recibiría el agente y la ruta prevista, sin llamadas. |
| `review TASK --verdict pass\|fail` | Veredicto con regla de independencia (proveedor distinto, o modelo superior). |
| `done TASK` / `finish [FASE] [--pr]` | Aceptación y commit; integración de la fase en la rama principal. |
| `evidence TASK` / `evidence verify` | Evidencia de una tarea; integridad de la cadena de hashes. |
| `providers status\|add\|remove\|reset` | Proveedores, allowlist del proyecto y cooldowns. |
| `local enable\|disable` | Modelo local compatible con OpenAI (desactivado por defecto). |
| `providers check [--probe]` | Preflight de cuota de los proveedores (`unknown` si la CLI no expone su uso). |
| `sync [--check]` / `prompt plan\|next\|retro` | Materializa el protocolo para las tres CLIs / imprime un paso. |
| `note "…"` / `resume [--json]` / `handoff` | Progreso continuo y relevo entre orquestadores. |
| `loop [--orchestrator …] [--max-tasks N] [--max-turns M] [--wait]` / `stop` | Ciclo headless con topes, relevo automático y parada limpia. |

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
make lint test smoke             # o, sin make: python tools/dev.py lint|test|smoke
make smoke-live                  # opcional: llamadas reales mínimas; gasta cuota y pide confirmación
```

Los tests usan CLIs falsas (`tests/fakes/`) y repositorios temporales: no llaman a ningún proveedor real.

## Publicar Hefesto

La publicación de Hefesto y la creación de releases es una acción **humana** (el framework nunca ejecuta `git push` de forma autónoma). Comandos exactos para publicar la versión:

```bash
git checkout main
hefesto finish
git push origin main
git tag -a v2.1.0 -m "Hefesto v2.1.0"
git push origin v2.1.0
gh release create v2.1.0 --notes-file CHANGELOG.md
```

Y, para quien parta de cero y cree el repositorio remoto en GitHub por primera vez:
```bash
gh repo create <usuario>/Hefesto --private --source . --push
```
