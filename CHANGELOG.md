# Changelog

Todas las novedades notables de Hefesto se documentan en este archivo.

El formato se basa en [Keep a Changelog](https://keepachangelog.com/es-ES/1.1.0/) y este proyecto se adhiere a [Semantic Versioning](https://semver.org/lang/es/).

## v2.1.0 — 2026-10-14

### Hito 1 — Núcleo en serie
- **Núcleo determinista:** Framework multiagente que aprovecha suscripciones existentes de Claude, ChatGPT (Codex) y Gemini/agy sin costes de API.
- **Máquina de estados atómica:** Control de ciclo de vida en `state.json` con escrituras seguras, lock contra concurrencia accidental y circuit breaker determinista al tercer intento fallido.
- **ROADMAP estructurado:** Tareas tipadas por fase con `verify`, `files`, `role` y `size`, aplicando el principio de contrato primero (`tests/`).
- **Sistema de confianza (`trust`):** Validación y control estricto de comandos `verify` antes de su ejecución.
- **Aislamiento en Git y guardián:** Rama por fase (`hefesto/<fase>`), soporte para `isolation: worktree`, guardián de seguridad (`guard.py`) que bloquea secretos y modificaciones no autorizadas, checkpoints limpios y comando `finish` con confirmación humana.
- **Cadena de evidencias encadenadas:** Hashes criptográficos SHA-256 enlazados en `evidence/` para auditoría y reproducibilidad completa del trabajo realizado.
- **Adaptadores multi-proveedor:** Adaptadores para Claude Code, Codex CLI y Gemini CLI, con gestión de paquetes de contexto acotados (`packet`), enrutado fallback y control de cooldowns por proveedor.

### Hito 2 — Multi-orquestador y autonomía
- **Orquestador intercambiable:** Soporte completo para que Claude Code, Codex CLI o Gemini CLI conduzcan el ciclo (`/hefesto-next`, `$hefesto-next`, `/hefesto:next`).
- **Sincronización unificada (`sync`):** Generación automática de skills, subagentes, comandos de herramientas y hooks para cada CLI desde una única fuente de verdad (`prompts/protocol/`), preservando ediciones manuales.
- **Ciclo autónomo (`loop`):** Ejecución desatendida con límites de tareas (`--max-tasks`), turnos (`--max-turns`), parada limpia mediante archivo `STOP` (`hefesto stop`) y relevo automático de orquestador (`--orchestrator auto`).
- **Relevo por cuota y persistencia:** Comando `handoff` y recuperación con `hefesto resume`, registro continuo con `hefesto note`, y preflight de cuota (`providers check`) sin consumir intentos de tarea al agotarse un proveedor.
- **Skills del framework:** Skills nativas integradas: `git-checkpointer`, `test-runner`, `skill-creator`, `retro`, `frontend-taste`, `motion-system` y `ui-anatomy`.

### Hito 3 — Distribución y ergonomía
- **Distribución e instalación universal:** Script `install.sh` idempotente para Linux, macOS, WSL y Windows nativo (Git Bash), con soporte para ejecutables en `~/.local/bin` (`hefesto` y `hefesto.cmd`).
- **Arranque inteligente (`hefesto bootstrap`):** Detección determinista entre crear proyecto (`init`), continuar tarea (`resume`) o mejorar código existente (`adopt`).
- **Adopción de proyectos existentes (`hefesto adopt`):** Detección de stack técnico (Python, Node, Flutter, Rust, Go) y propuesta de roadmap sin alterar el código existente.
- **Actualización y migración (`self-update` y `migrate`):** Actualización interactiva a tags oficiales con visualización previa de CHANGELOG (`hefesto self-update`) y migración automática de proyectos con respaldo en `.hefesto/backups/` (`hefesto migrate`).
- **Modo local (`install_mode: local`):** Instalación predeterminada que ubica todo dentro de `.hefesto/` y `.git/info/exclude`, manteniendo `git status` limpio y sin subir configuración de Hefesto a GitHub.
- **Una sola autorización inicial:** Flujo `bootstrap --yes`, preconfiguración de permisos para Claude Code y barreras de seguridad deterministas que eliminan confirmaciones redundantes.
- **Verificación segura automática (`auto_safe`):** Aprobación autónoma de comandos estándar de test, lint y build (`is_safe_verify`), reservando la aprobación humana interactiva únicamente para comandos potencialmente destructivos o con acceso a red.
- **Revisor independiente:** Enrutamiento de `hefesto run reviewer` que omite automáticamente a los proveedores autores de la tarea en curso.
- **Adaptador `agy` endurecido:** Adaptador reforzado para el entorno Google Antigravity CLI.

### Conocido / pendiente
- **Hito 4 (Ecosistema y paralelismo):**
  - Catálogo y gestión de skills externas: `skill find`, `fetch` y `promote`.
  - Packs curados externos: pack `context` (Graphify para navegación de código sin LLM), pack `usage` (ccusage para métricas locales de tokens) y pack `memory` (Basic Memory para memoria técnica permanente entre proyectos).
  - Planificador concurrente (`max_parallel_tasks`) con worktrees aislados por tarea y concurrencia adaptativa por proveedor.
  - Limpieza y mantenimiento de estados huérfanos con `clean` y `discard`.
- **CLI gemini para cuentas individuales:** Google ha retirado la CLI directa de Gemini para cuentas personales; se debe utilizar el adaptador `agy` (Antigravity CLI).
