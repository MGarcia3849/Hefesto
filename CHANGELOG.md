# Changelog

Todas las novedades notables de Hefesto se documentan en este archivo.

El formato se basa en [Keep a Changelog](https://keepachangelog.com/es-ES/1.1.0/) y este proyecto se adhiere a [Semantic Versioning](https://semver.org/lang/es/).

## Skills en el paquete de tarea

- **Descubrimiento y selección determinista (`hefesto/skill_select.py`):** Descubrimiento de skills instaladas en carpetas del proyecto (`.claude/skills`, `.agents/skills`), promovidas (`~/hefesto/skills/promoted`), framework (`skills/`) y carpetas del usuario configurables (`skills_selection.user_roots`, por defecto `["~/.claude/skills", "~/.codex/skills"]`), deduplicando por nombre en ese orden de preferencia (`proyecto` > `promovida` > `framework` > `usuario`).
- **Puntuación por palabras clave y configuración `[skills_selection]`:** Selección sin LLM mediante coincidencia de palabra entera entre palabras clave de la tarea (título, `accept`, extensiones de `files` y `role`) y los metadatos de cada skill, donde cada palabra clave suma una sola vez en el primer campo donde casa (`name` +3, si no `tags` +2, si no `description` +1; no se acumulan), gobernada por `enabled`, `max_skills`, `min_score`, `max_chars_per_skill`, `user_roots` y `exclude` (excluyendo siempre las skills de protocolo).
- **Campo `skills` en el ROADMAP (`hefesto/roadmap.py`):** Soporte para el campo opcional `- skills: a, b` en las tareas del ROADMAP (`Task.skills`), incluyendo siempre en primer lugar las skills fijadas (también con `enabled = false` o aunque superen el tope total `max_skills`, salvo las listadas en `exclude`; las automáticas solo ocupan los huecos libres que dejan las fijadas hasta `max_skills`, p. ej. con `max_skills = 3` y 2 fijadas entra como mucho 1 automática).
- **Inyección saneada en el paquete del trabajador (`hefesto/packet.py`):** Inclusión de la sección `## Skills que aplican (síguelas)` con nombre, origen, motivos y el cuerpo de `SKILL.md` saneado, con secretos redactados y recortado a `max_chars_per_skill`, con prioridad de recorte tras la memoria y antes que los ficheros de la tarea.
- **Sugerencia de skills y visibilidad en enrutado (`skill suggest` y `route`):** Nuevo subcomando `hefesto skill suggest TASK` para inspeccionar las skills locales elegidas con su puntuación y motivo, opción `--online` que propone candidatas en `skills.sh` con nivel de confianza y comando `hefesto skill fetch` (sin descargar ni promover automáticamente), y visualización de las skills del paquete en `hefesto route TASK --dry-run`.
- **Protocolo y documentación actualizados:** Actualización de `prompts/protocol/plan.md`, `prompts/protocol/next.md` y `README.md` con las pautas de uso de `skills:` y `hefesto skill suggest`.

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

### Hito 4 — Ecosistema, paralelismo y validación final
- **Ecosistema de skills y packs:** Catálogo curado de packs (`pack list|enable|disable|update`), búsqueda de skills locales y públicas (`skill find`), descarga segura a `_proposals/` con informe automatizado de riesgos (`skill fetch`), promoción interactiva (`promote`), y pack `context` con integración de Graphify para análisis estructural de código sin consumo de LLM.
- **Aislamiento con worktrees fuera del proyecto:** Creación de worktrees en `~/.hefesto/worktrees/` fuera del árbol del repositorio, evitando interferencias con la copia principal y asegurando que herramientas como `agy` resuelvan limpiamente la raíz de trabajo.
- **Integración y finalización atómica (`finish`):** Validación completa de precondiciones, conflictos y cadena de evidencias antes de realizar modificaciones en la copia principal; si falla alguna comprobación, el espacio de trabajo permanece inalterado.
- **Mantenimiento y reversión (`clean` y `discard`):** Comando `hefesto clean` para eliminar worktrees y ramas `hefesto/*` huérfanas sin cambios no guardados; comando `hefesto discard` para revertir tareas de forma atómica (código y evidencia) o descartar fases enteras dejando constancia justificada en `DECISIONS.md`.
- **Evidencias numeradas y encadenadas:** Evidencias con número de secuencia (`seq`), hash del commit resultante, hash criptográfico de la evidencia previa (`prev_sha256`) y seguimiento de cabeza local (`evidence_head`) para auditoría e integridad criptográfica estricta.
- **Scheduler paralelo con concurrencia adaptativa por modelo:** Comando `hefesto parallel` que ejecuta lotes de tareas independientes en worktrees disjuntos respetando límites globales (`max_parallel_tasks`) y por modelo (`max_parallel_per_model`); adaptación dinámica de concurrencia al 50% ante `rate_limit` por modelo con recuperación tras éxitos; integración serial en la rama de la fase marcando conflictos como `[!]` sin abortar el resto del lote. Simulación previa con `hefesto route --dry-run`.
- **Memoria técnica global entre proyectos:** Notas locales o propuestas globales (`hefesto remember`), circuito de revisión humana obligatoria (`hefesto memory review`), búsqueda textual (`hefesto memory search`), saneamiento de duplicados (`hefesto memory prune`), e inyección contextual de notas relevantes en `resume` y paquetes de tarea.
- **Supervisión de límites y actividad (`limits`, `jobs`, `watch` y `statusline`):** Comando `hefesto limits` para lectura offline de límites de 5 h y semanal de Codex y Claude Code; comando `hefesto jobs` con visualización tabular limpia de agentes activos y última línea de actividad legible; monitor en directo `hefesto watch`; y puente `hefesto statusline` para la línea de estado de Claude Code.
- **Diagnóstico y robustez:** `hefesto doctor` ampliado con comprobación de CPU y memoria RAM disponible para cálculo de concurrencia recomendada; guarda de contratos en `review` adaptada para permitir enmiendas legítimas al rol Core; comando `fail` idempotente tras revisión negativa sin duplicar conteo de intentos.
- **Suite de demostración y validación final:** Comando `python tools/dev.py demo` que valida en un entorno temporal aislado tanto el ciclo serie completo como una fase paralela de 3 tareas con conflicto de rebase intencionado; suite de 427 tests automatizados en verde.

### Desviaciones conocidas / Notas
- **CLI gemini para cuentas individuales:** Google ha retirado la CLI directa de Gemini para cuentas personales; se debe utilizar el adaptador `agy` (Antigravity CLI).
- **Límites de uso en Claude y Gemini:** Las CLIs no proporcionan API para consultar la cuota restante (`unknown` en preflight y `hefesto limits`).
- **PowerShell `--add-to-path`:** La rama de PowerShell de `install.sh` en Windows interactúa con el registro del entorno del usuario pero no está cubierta por tests en CI para proteger el sistema host.
- **Pack `memory` (basic-memory):** Versión externa sin fijar; opera de forma predeterminada mediante el flujo nativo de archivos auditables en Markdown.
