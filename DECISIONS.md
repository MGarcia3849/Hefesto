# Decisiones de construcción de Hefesto

Registro de las desviaciones respecto a `HEFESTO_V2_1.md` y de lo verificado en la Fase 0 (2026-10-14).

## ADR-001 — Soporte de Windows nativo además de WSL

- **Contexto:** la especificación limita el soporte a Linux, macOS y WSL. En la máquina del usuario, `codex`, `gemini`, `agy`, `node` y `graphify` solo están instalados y autenticados en Windows; en WSL (Ubuntu) solo están `python3` 3.12, `git` y `claude`.
- **Decisión (del usuario):** Hefesto funciona en Windows nativo y en Linux/macOS/WSL.
- **Consecuencias:**
  - Python se invoca como `python` si `python3` no es un Python ≥ 3.11 real (en Windows, `python3` suele ser el acceso directo de la Microsoft Store).
  - Lanzadores: `bin/hefesto` (sh) y `bin/hefesto.cmd` (cmd/PowerShell). `install.sh` se ejecuta con Git Bash y crea ambos en `~/.local/bin`.
  - `verify` se ejecuta con el `bash` de Git for Windows (nunca con `C:\Windows\System32\bash.exe`, que es el lanzador de WSL).
  - La comprobación de PID vivo del lock usa la API de Windows (`OpenProcess`) vía `ctypes` (librería estándar).
  - Las CLIs de npm son `.cmd`; se resuelven con `shutil.which` y el prompt siempre se pasa por stdin, nunca como argumento.
  - Sin `make` en Windows: `tools/dev.py lint|test|smoke` equivale a los objetivos del `Makefile`.

## ADR-002 — Resultado de la auditoría de CLIs (Fase 0)

| CLI | Versión | No interactivo | Salida JSON | Aislamiento |
|---|---|---|---|---|
| Claude Code | 2.1.296 | `claude -p` | `--output-format json` | `--permission-mode`, `--allowedTools`, hooks |
| Codex CLI | 0.155.1 | `codex exec` (prompt por stdin con `-`) | `--json` (eventos JSONL) | `-s read-only \| workspace-write` |
| Gemini CLI | 0.50.0 | `gemini -p` (añade stdin) | `-o json` | `--approval-mode default\|auto_edit\|plan`, `-s` |
| Antigravity (`agy`) | 1.2.11 | `agy -p` | `--output-format json` | `--mode accept-edits\|plan`, `--sandbox` |

- Autenticación comprobable sin gastar llamadas: Codex (`codex login status`). Para Claude y Gemini solo se comprueba la presencia de credenciales locales (`unknown` si no hay).
- Ninguna CLI expone de forma legible por scripts la cuota *restante*; el nivel 2 del preflight queda en `unknown` hasta el pack `usage`.
- Modelos visibles: `agy models` lista `gemini-3.8-flash-{low,medium,high}`; `codex debug models` lista `gpt-6-*`, `gpt-5.6-*`. Siguiendo la regla 3, ningún nombre se fija en el código; la config por defecto usa `gemini:default` y `codex:default` y el usuario añade los suyos.
- Paquete npm `skills`: versión actual 1.7.2, exige Node ≥ 22.20 (instalado 22.14: funciona con aviso).
- `graphify` 0.9.13: `extract --code-only`, `update`, `query`, `path` y `explain` verificados.
- Sin `shellcheck`, `uv` ni `pipx` instalados: el lint de shell se omite con aviso.

## ADR-003 — Los TOML del usuario nunca se reescriben

`settings.json` (proyecto y global) guarda lo que Hefesto escribe; las sobrescrituras de configuración van bajo su clave `config`. Orden efectivo: TOML por defecto → `~/hefesto/hefesto.toml` → `~/hefesto/settings.json` → `.hefesto/hefesto.toml` → `.hefesto/settings.json`.

## ADR-004 — Comprobación de TTY sin variable de entorno de escape

`trust`, `unblock`, `promote` y similares exigen `stdin` y `stdout` en terminal. No existe ninguna variable de entorno que lo simule (un agente podría usarla); los tests sustituyen la función en proceso.

## ADR-005 — Dos commits por tarea: código y evidencia

La evidencia debe contener el hash del commit resultante, y un commit no puede contener su propio hash. `done` hace `hefesto(TASK-NNN): … [verified]` (código + ROADMAP `[x]`) y después `hefesto(TASK-NNN): evidence`. `last_checkpoint` apunta al primero. Para revertir una tarea se revierten ambos.

## ADR-006 — Cadena de evidencias: `seq` + hash de la anterior + cabeza local

Cada evidencia lleva `seq`, `prev_sha256` (SHA-256 de la anterior con saltos de línea normalizados, estable ante `autocrlf`) y `prev_task_id`. `state.json` (local) guarda la cabeza (`evidence_head`), de modo que `evidence verify` detecta también el borrado de la última evidencia.

## ADR-007 — Ficheros de control en modo worktree

`.hefesto/` (ROADMAP, evidencias, DECISIONS, LEARNINGS) vive siempre en la copia principal, que es la única fuente de verdad. En modo `worktree`, el checkpoint copia esos ficheros al worktree antes de commitear. En `finish`, si la copia principal tiene esos ficheros modificados y su contenido coincide con el de la rama, se devuelven a su versión de HEAD antes de integrar (su contenido entra con la propia integración). Si no coinciden, `finish` se detiene.

## ADR-008 — Ramas: `hefesto/<fase>` y, en `branch_mode = task`, `hefesto/<fase>-task-nnn`

Git no permite `hefesto/fase-1` y `hefesto/fase-1/TASK-001` a la vez (conflicto fichero/directorio en `refs/heads`). Las ramas por tarea usan un guion. Una fase nueva parte de la rama abierta de la fase anterior si sigue sin integrar (para que sus dependencias estén presentes), o de la rama principal. `finish` rebasa con `--onto <principal> <base de la fase>`, de modo que solo se reaplican los commits de esa fase.

## ADR-009 — Adaptadores: prompt por stdin; `agy` recibe el paquete por fichero

El paquete puede superar el límite de 32 767 caracteres de la línea de órdenes de Windows, así que se pasa por stdin (`claude -p`, `codex exec -`, `gemini -p` añade stdin). Para `agy` no se ha podido verificar que lea stdin en `-p`: recibe una instrucción corta que apunta al paquete guardado en `.hefesto/logs/packets/` (con `--add-dir`). Sin flags que salten permisos: Claude `--permission-mode acceptEdits|plan`, Codex `-s workspace-write|read-only`, Gemini `--approval-mode auto_edit|plan`, agy `--mode accept-edits|plan`.

## ADR-010 — Patrones de cuota y rate limit sin verificar contra salidas reales

Los patrones de `hefesto/adapters/*.py` salen de mensajes conocidos de cada CLI (p. ej. Claude «usage limit reached|<epoch>», Codex «You've hit your usage limit», Gemini «RESOURCE_EXHAUSTED» / «Quota exceeded»). No se han provocado errores reales de cuota para confirmarlos. Si una CLI cambia sus mensajes, el error se clasifica como fallo genérico, que sí cuenta como intento: hay que revisar los patrones al ver un caso real (`.hefesto/logs/runs/`).

## ADR-011 — `auth_error` persiste hasta `providers reset`

Un proveedor con error de autenticación se salta hasta que el usuario inicia sesión y ejecuta `hefesto providers reset NOMBRE`. Los cooldowns de cuota, en cambio, vencen solos.

## ADR-012 — Escalado y códigos de salida

- `fail` sale con 0 y devuelve el rol del siguiente intento; con 2 si salta el circuit breaker. `done` con `verify` en rojo sale con 1 (intentos restantes) o 2 (breaker).
- El tercer intento pasa a `forge` solo si la tarea era de `runner`. Las tareas `core`, `visual` y `forge` conservan su rol.
- `review --verdict fail` cuenta como intento (es un fallo real del trabajo).

## ADR-013 — Alcance del Hito 1

`clean` y `discard` (sección 10) quedan para el Hito 4, junto con el resto de la gestión de worktrees del paralelismo. `note`, `resume`, `handoff`, `loop`, `sync`, `prompt` y las skills son del Hito 2. Las plantillas solo mencionan comandos que ya existen.

## ADR-014 — Materialización del protocolo (Fase 5), verificada en las CLIs instaladas

- **Fuente única:** `prompts/protocol/{plan,next,retro}.md` (frontmatter `name`/`description` + cuerpo) y `prompts/<rol>.md`.
- **Claude Code:** skills en `.claude/skills/hefesto-*/SKILL.md` y subagentes en `.claude/agents/hefesto-<rol>.md` (`model` = primer `claude:*` de la cadena del rol, o `inherit`; el Reviewer solo con `Read, Grep, Glob`).
- **Codex 0.155:** lee `AGENTS.md`; skills de repositorio en `.agents/skills/` (se invocan con `$nombre`); hooks estables en `.codex/hooks.json` con el mismo esquema que Claude (`SessionStart`, `PreToolUse`; herramientas `Bash` y `apply_patch`; código 2 = bloquear); configuración de proyecto en `.codex/config.toml` (`workspace-write`, `on-request`, sin red). Verificado por las cadenas del binario y `codex features list` (`hooks: stable`), no por documentación publicada.
- **Gemini 0.50:** lee `GEMINI.md` (importa `@./AGENTS.md`); comandos en `.gemini/commands/hefesto/*.toml` (`/hefesto:next`); descubre skills en `.gemini/skills/` **y** `.agents/skills/`, así que no se duplican; hooks `SessionStart` y `BeforeTool` en `.gemini/settings.json`, en el formato que produce `gemini hooks migrate --from-claude`. Herramientas: `run_shell_command`, `read_file`, `write_file`, `replace`, `glob`…, que `guard.py` traduce.
- **Skills del framework:** `sync` las copia como `hefesto-skill-<nombre>` en `.claude/skills/` y `.agents/skills/` (copia y no symlink, para que funcionen en Windows sin modo desarrollador y para detectar ediciones a mano). Por eso `init --copy` desaparece: siempre es copia.
- **Conflictos:** `.hefesto/generated.json` guarda el hash de cada fichero generado. Un fichero editado a mano, o uno que existía antes con otro contenido (p. ej. tu `.gemini/settings.json`), se reporta como conflicto (código 2) y no se toca.

## ADR-015 — Loop y orquestadores headless (Fase 6)

- Cada iteración pasa al orquestador el paso `next` + un apartado «modo autónomo» (una tarea, sin `trust`/`unblock`/`finish`). El proceso hijo recibe `HEFESTO_ORCHESTRATOR`, `HEFESTO_LOOP_ID` y `HEFESTO_ROLE=orchestrator`, de modo que `note` sabe quién escribe.
- Permisos del orquestador sin saltarse nada: Claude `--permission-mode acceptEdits --allowedTools "Bash(hefesto:*) …"` y `--max-turns`; Gemini `--approval-mode auto_edit --allowed-tools run_shell_command(hefesto) …` (el flag está marcado como obsoleto en favor del motor de políticas, pero funciona en 0.50); Codex `exec -s workspace-write`.
- **Sin verificar con llamadas reales:** que el sandbox `workspace-write` de Codex en Windows permita a `hefesto done` escribir en `.git` (commit). Si no lo permite, Codex puede orquestar todo menos el `done`, que quedará indicado en `next_action` para el siguiente orquestador o para el usuario.
- `--max-turns` solo lo aplica Claude; en Codex y Gemini el tope efectivo es `orchestrator_timeout_seconds` y el límite de iteraciones del loop (`max_tasks × (max_attempts + 3)`). Dos iteraciones seguidas sin ningún cambio de estado detienen el loop para no quemar cuota.
- La sonda de cuota es una llamada en modo solo lectura («Responde únicamente con la palabra: ok»), cacheada `probe_max_age_minutes` en `~/.hefesto/providers.json`.

## ADR-016 — El Hito 2 no se ha construido «con Hefesto»

La especificación propone escribir las fases del Hito 2 en adelante como ROADMAP del propio repositorio y ejecutarlas con su ciclo, repartiendo el coste entre las tres suscripciones. Se ha construido directamente en esta sesión porque: (1) el núcleo aún no se ha probado con llamadas reales y (2) habría gastado cuota de las tres cuentas sin la aprobación explícita del usuario. Queda como opción para los hitos 3 y 4, empezando por `make smoke-live`.
