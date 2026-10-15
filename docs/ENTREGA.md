# Entrega final — Hefesto v2.1.0

Documento de entrega final según la sección 14 de `HEFESTO_V2_1.md` y la especificación de la Fase 10 (Validación final).
Fecha de validación: 2026-10-15 en Windows 11.

---

## 1. Árbol de ficheros del repositorio

Estructura real del proyecto a nivel de directorios y módulos principales:

```
Hefesto/
├── .agents/
│   └── skills/                  # Skills expuestas a Codex CLI y Gemini CLI ($hefesto-*, /hefesto:*)
├── .claude/
│   ├── agents/                  # Definiciones de subagentes para Claude Code (runner, reviewer, forge, visual)
│   ├── settings.json            # Configuración de permisos y hooks de Claude Code
│   └── skills/                  # Skills expuestas a Claude Code (/hefesto-*)
├── .codex/
│   ├── config.toml              # Sandbox workspace-write y configuración para Codex CLI
│   └── hooks.json               # Hooks de ciclo de vida (SessionStart, PreToolUse)
├── .gemini/
│   ├── commands/hefesto/        # Comandos personalizados para Gemini CLI (plan, next, retro)
│   └── settings.json            # Configuración y hooks (SessionStart, BeforeTool)
├── .hefesto/
│   ├── DECISIONS.md             # Registro de decisiones de diseño (ADR)
│   ├── LEARNINGS.md             # Registro continuo de lecciones aprendidas
│   ├── ROADMAP.md               # Tareas y fases con verify deterministas
│   └── state.json               # Estado atómico del ciclo de vida y lock de concurrencia
├── bin/
│   ├── hefesto                  # Lanzador Bash para Linux, macOS y Git Bash en Windows
│   └── hefesto.cmd              # Lanzador cmd/PowerShell para Windows nativo
├── catalog/                     # Catálogo de packs curados externos (context, usage, memory, etc.)
├── docs/
│   └── ENTREGA.md               # Documento de entrega final y auditoría de validación
├── hefesto/                     # Núcleo del framework (100% Python estándar ≥ 3.11)
│   ├── __init__.py
│   ├── acceptance.py            # Aceptación de tareas, veredicto de revisión, done y finish
│   ├── activity.py              # Monitorización y registro de agentes activos
│   ├── adapters/                # Adaptadores para CLIs de suscripción
│   │   ├── __init__.py
│   │   ├── base.py              # Clase base y clasificación de errores
│   │   ├── claude.py            # Adaptador Claude Code (claude -p)
│   │   ├── codex.py             # Adaptador Codex CLI (codex exec -)
│   │   ├── gemini.py            # Adaptador Gemini CLI y agy (Antigravity)
│   │   └── local.py             # Adaptador para modelos locales compatibles con OpenAI
│   ├── admin.py                 # Gestión de proveedores y configuración de modelo local
│   ├── cli.py                   # Definición de la CLI hefesto y argumentos
│   ├── config.py                # Carga en capas y validación de hefesto.toml / settings.json
│   ├── distribution.py          # bootstrap, adopt, self-update y migrate
│   ├── doctor.py                # Diagnóstico de entorno, adaptadores, recursos (CPU/RAM) y estado
│   ├── evidence.py              # Cadena criptográfica SHA-256 de evidencias
│   ├── gitops.py                # Operaciones de Git (ramas, checkpoints, rebase, worktrees)
│   ├── guard.py                 # Barreras de seguridad, rutas protegidas y detección de secretos
│   ├── limits.py                # Lectura de ventanas de cuota de 5 h y semanal, statusline
│   ├── loop.py                  # Ciclo autónomo headless con relevo automático y parada limpia
│   ├── maintenance.py           # Comandos clean y discard para mantenimiento de ramas/worktrees
│   ├── memory.py                # Memoria técnica global entre proyectos y flujo de revisión
│   ├── packet.py                # Ensamblado acotado de paquetes de contexto para delegación
│   ├── packs.py                 # Instalación, activación y actualización de packs externos
│   ├── preflight.py             # Verificación preventiva de cuota y disponibilidad antes de delegar
│   ├── progress.py              # Registro continuo de notas (note), resume y handoff
│   ├── project.py               # Descubrimiento de raíz de proyecto e inicialización (init)
│   ├── providers.py             # Gestión de allowlist de proveedores y normalización de modelos
│   ├── quota.py                 # Cooldowns por cuota o rate limit y registro de uso
│   ├── roadmap.py               # Carga, tipado y validación de ROADMAP.md
│   ├── router.py                # Enrutador por rol, cadena de fallback y preflight
│   ├── scheduler.py             # Planificador paralelo con concurrencia adaptativa por modelo
│   ├── skills.py                # Búsqueda, descarga a _proposals, informe de riesgos y promoción
│   ├── state.py                 # Lectura y persistencia atómica de state.json
│   ├── status.py                # Renderizado de hefesto status
│   ├── sync.py                  # Generación idempotente de skills, subagentes, comandos y hooks
│   ├── trust.py                 # Aprobación interactiva o auto_safe de comandos verify
│   ├── util.py                  # Utilidades compartidas del sistema de archivos, procesos y fechas
│   └── workflow.py              # Transiciones de ciclo de tarea (start, next, fail, unblock)
├── prompts/                     # Plantillas maestras de prompts
│   ├── protocol/                # Fuente única de plan.md, next.md y retro.md
│   └── roles/                   # Prompts base para roles (core, runner, forge, visual, reviewer)
├── skills/                      # Skills nativas versionadas en el framework
│   ├── _proposals/              # Propuestas de skills pendientes de revisión y promoción
│   ├── frontend-taste/
│   ├── git-checkpointer/
│   ├── motion-system/
│   ├── retro/
│   ├── skill-creator/
│   ├── test-runner/
│   └── ui-anatomy/
├── templates/                   # Plantillas para inicialización de proyectos
│   └── project/
├── tests/                       # Suite de tests con fakes (sin llamadas reales ni coste de API)
│   ├── fakes/                   # CLIs falsas (claude, codex, gemini, agy, graphify, npx)
│   ├── helpers.py               # Utilidades de test y casos base
│   └── test_*.py                # 427 tests automatizados unitarios y de integración
├── tools/
│   └── dev.py                   # Tareas de desarrollo offline (lint, test, smoke, demo)
├── AGENTS.md                    # Protocolo maestro de orquestación
├── BOOTSTRAP.md                 # Guía de bootstrap para arranque rápido en cualquier proyecto
├── CHANGELOG.md                 # Registro histórico de cambios del proyecto
├── CLAUDE.md                    # Entrada de protocolo para Claude Code
├── DECISIONS.md                 # Decisiones arquitectónicas y auditoría inicial
├── GEMINI.md                    # Entrada de protocolo para Gemini CLI / agy
├── HEFESTO_V2_1.md              # Especificación completa de diseño de Hefesto v2.1
├── Makefile                     # Acceso rápido a targets de dev (para entornos POSIX)
├── README.md                    # Documentación principal de usuario y arquitectura
├── VERSION                      # Versión actual del framework (2.1.0)
├── hefesto.toml                 # Configuración predeterminada de Hefesto
└── install.sh                   # Script de instalación idempotente para cualquier plataforma
```

---

## 2. Lista de comandos (`hefesto --help`)

Salida real de `hefesto --help`:

```
usage: hefesto [-h] [--version] [-C PROJECT] COMANDO ...

Orquestación multiagente determinista.

positional arguments:
  COMANDO
    init                inicializa Hefesto en un proyecto (idempotente)
    bootstrap           detecta el modo de trabajo (crear, continuar, mejorar)
                        y arranca
    adopt               adopta un proyecto existente sin modificar su código
    self-update         actualiza la copia instalada de Hefesto al último tag
                        o al indicado
    migrate             aplica migraciones de configuración y plantillas si la
                        versión cambió
    doctor              comprueba dependencias, adaptadores, allowlist, lock y
                        estado
    config              valida o muestra la configuración efectiva
    status              estado, tarea, intentos, rama, rol activo y siguiente
                        acción
    hook                puente para los hooks de Claude Code (lee JSON por
                        stdin)
    next                siguiente tarea pendiente respetando dependencias y
                        bloqueos
    start               inicia una tarea (con lock y aislamiento git)
    fail                registra un intento fallido y devuelve el rol del
                        siguiente
    unblock             desbloquea una tarea tras el circuit breaker (solo
                        humanos, TTY)
    trust               aprueba los comandos verify del ROADMAP (solo humanos,
                        TTY)
    review              registra el veredicto del Reviewer (regla de
                        independencia)
    done                verify confiado y en verde + revisión pass →
                        checkpoint, [x] y evidencia
    finish              integra la rama de la fase en la principal (rebase,
                        tests, confirmación)
    evidence            muestra la evidencia de una tarea o comprueba la
                        cadena (`verify`)
    clean               elimina worktrees y ramas hefesto huérfanos sin
                        cambios
    discard             revierte una tarea o descarta una fase dejando
                        constancia en DECISIONS
    run                 delega en el proveedor que toque según rol, allowlist
                        y cuota
    packet              genera y muestra el paquete de contexto de una tarea
    route               ruta, roles y proveedores previstos para una tarea,
                        sin llamadas
    parallel            ejecuta tareas en paralelo respetando concurrencia y
                        límites
    providers           estado de proveedores, allowlist del proyecto y
                        cooldowns
    local               activa o desactiva el modelo local (API compatible con
                        OpenAI)
    sync                regenera skills, subagentes, comandos y hooks de
                        Claude, Codex y Gemini
    note                registra un paso de progreso de la tarea en curso
    resume              resumen para que cualquier orquestador continúe donde
                        lo dejó el anterior
    handoff             relevo limpio a otro orquestador
    loop                ciclo headless con topes, STOP y relevo automático
    stop                crea .hefesto/STOP para detener el loop
    prompt              imprime un paso del protocolo (para orquestadores sin
                        comandos propios)
    skill               busca y gestiona skills locales y del registro público
    promote             promueve una propuesta de skill a promovidas
    pack                catálogo y gestión de packs externos curados
    limits              uso real de ventanas de 5 h y semanal de cada
                        proveedor
    statusline          comando de línea de estado para Claude Code (lee JSON
                        por stdin)
    jobs                muestra los agentes activos y la última línea de su
                        log
    watch               monitor de límites y actividad de agentes en directo
    remember            guarda una nota en la memoria local o global
    memory              gestión de la memoria global entre proyectos

options:
  -h, --help            show this help message and exit
  --version             show program's version number and exit
  -C, --project PROJECT
                        directorio del proyecto (por defecto, el actual)
```

---

## 3. Resultados reales de validación

En Windows 11 nativo, la herramienta `make` no existe de serie (ADR-001); todas las tareas de desarrollo se invocan de forma equivalente y portable mediante `python tools/dev.py <comando>`. Asimismo, `shellcheck` no está instalado en el entorno, por lo que el análisis de scripts shell se omite con aviso sin bloquear la suite.

### 3.1 Lint (`python tools/dev.py lint`)

```
lint: shellcheck no está instalado; se omite el análisis de install.sh
lint: OK (72 ficheros Python)
exit 0
```

### 3.2 Tests (`python tools/dev.py test`)

```
Ran 427 tests in 408.303s
OK (skipped=2)
exit 0
```

### 3.3 Diagnóstico del entorno (`hefesto doctor`)

```
[ok] python          3.13.2 (C:\Python313\python.exe)
[··] plataforma      Windows 11 — Windows nativo (soporte añadido por decisión del usuario, ver DECISIONS.md)
[ok] recursos        16 CPU, 18.5 GB RAM disponible (recomendado hasta 16 agentes en paralelo; max_parallel_tasks=6)
[ok] git             git version 2.51.0.windows.1  (C:\Program Files\Git\mingw64\bin\git.EXE)
[ok] git worktree    disponible
[ok] bash (verify)   C:\Program Files\Git\bin\bash.exe
[ok] claude          2.1.300 (Claude Code)  (C:\Users\mggcp\AppData\Roaming\npm\claude.CMD)
[ok] codex           codex-cli 0.162.1  (C:\Users\mggcp\AppData\Roaming\npm\codex.CMD)
[ok] gemini          0.50.0  (C:\Users\mggcp\AppData\Roaming\npm\gemini.CMD)
[ok] agy             1.3.5  (C:\Users\mggcp\AppData\Local\agy\bin\agy.EXE)
[··] auth claude     yes
[··] auth codex      yes
[··] auth gemini     yes
[··] límites claude  5 h: unknown, semanal: unknown
[··] límites codex   5 h: 95%, semanal: 15%
[··] límites gemini  5 h: unknown, semanal: unknown
[!!] node            v22.14.0  (C:\Program Files\nodejs\node.EXE) — anterior a 22.20 (se recomienda Node >= 22.20)
[ok] npx             C:\Program Files\nodejs\npx.CMD
[··] skills (npm)    versión fijada 1.7.2
[--] shellcheck      no encontrado — opcional: lint de install.sh
[ok] graphify        C:\Users\mggcp\AppData\Roaming\Python\Python313\Scripts\graphify.EXE (versión fijada 0.9.13)
[ok] API keys        ninguna en el entorno (uso por suscripción)
[··] proyecto        C:\Users\mggcp\Documents\GitHub\Hefesto
[ok] config          válida (default, global, project)
[··] allowlist       claude, codex, gemini
[ok] estado          IN_PROGRESS tarea=TASK-1004 intentos=0
[ok] lock            libre
[··] packs           ninguno activo
exit 0
```

---

## 4. Demostración de una tarea completa en serie (Smoke)

Salida de la prueba completa de humo (`python tools/dev.py smoke`) ejecutada con fakes en directorio temporal:
`init` → `start` → `run runner` → `run reviewer` → `review` → `done` → `evidence verify` → `status`:

```
$ hefesto init C:\Users\mggcp\AppData\Local\Temp\hefesto-smoke-8s82tkm0\proyecto --shared --providers gemini,codex  → 0

  _   _       __           _
 | | | | ___ / _| ___  ___| |_ ___
 | |_| |/ _ \ |_ / _ \/ __| __/ _ \
 |  _  |  __/  _|  __/\__ \ || (_) |
 |_| |_|\___|_|  \___||___/\__\___/   v2.1.0-dev

git init
creado .claude/settings.json
creado .hefesto/DECISIONS.md
creado .hefesto/LEARNINGS.md
creado .hefesto/logs/.gitkeep
creado .hefesto/ROADMAP.md
creado AGENTS.md
creado CLAUDE.md
creado GEMINI.md
creado .hefesto/state.json
.gitignore: .hefesto/state.json, .hefesto/.lock/, .hefesto/trust.json, .hefesto/logs/, .hefesto/STOP, .hefesto/worktree/, .hefesto/worktrees/, .hefesto/progress.jsonl, graphify-out/
proveedores autorizados: codex, gemini
sync: 30 ficheros gestionados; 30 con cambios.
  create    .agents/skills/hefesto-next/SKILL.md
  create    .agents/skills/hefesto-plan/SKILL.md
  create    .agents/skills/hefesto-retro/SKILL.md
  create    .agents/skills/hefesto-skill-frontend-taste/SKILL.md
  create    .agents/skills/hefesto-skill-git-checkpointer/SKILL.md
  create    .agents/skills/hefesto-skill-motion-system/SKILL.md
  create    .agents/skills/hefesto-skill-retro/SKILL.md
  create    .agents/skills/hefesto-skill-skill-creator/SKILL.md
  create    .agents/skills/hefesto-skill-test-runner/SKILL.md
  create    .agents/skills/hefesto-skill-ui-anatomy/SKILL.md
  create    .claude/agents/hefesto-forge.md
  create    .claude/agents/hefesto-reviewer.md
  create    .claude/agents/hefesto-runner.md
  create    .claude/agents/hefesto-visual.md
  create    .claude/skills/hefesto-next/SKILL.md
  create    .claude/skills/hefesto-plan/SKILL.md
  create    .claude/skills/hefesto-retro/SKILL.md
  create    .claude/skills/hefesto-skill-frontend-taste/SKILL.md
  create    .claude/skills/hefesto-skill-git-checkpointer/SKILL.md
  create    .claude/skills/hefesto-skill-motion-system/SKILL.md
  create    .claude/skills/hefesto-skill-retro/SKILL.md
  create    .claude/skills/hefesto-skill-skill-creator/SKILL.md
  create    .claude/skills/hefesto-skill-test-runner/SKILL.md
  create    .claude/skills/hefesto-skill-ui-anatomy/SKILL.md
  create    .codex/config.toml
  create    .codex/hooks.json
  create    .gemini/commands/hefesto/next.toml
  create    .gemini/commands/hefesto/plan.toml
  create    .gemini/commands/hefesto/retro.toml
  create    .gemini/settings.json

== doctor ==
[ok] python          3.13.2 (C:\Python313\python.exe)
[··] plataforma      Windows 11 — Windows nativo (soporte añadido por decisión del usuario, ver DECISIONS.md)
[ok] recursos        16 CPU, 18.7 GB RAM disponible (recomendado hasta 16 agentes en paralelo; max_parallel_tasks=6)
[ok] git             git version 2.51.0.windows.1  (C:\Program Files\Git\mingw64\bin\git.EXE)
[ok] git worktree    disponible
[ok] bash (verify)   C:\Program Files\Git\bin\bash.exe
[ok] claude          claude fake 0.0.0  (C:\Users\mggcp\AppData\Local\Temp\hefesto-smoke-8s82tkm0\bin\claude.CMD)
[ok] codex           codex fake 0.0.0  (C:\Users\mggcp\AppData\Local\Temp\hefesto-smoke-8s82tkm0\bin\codex.CMD)
[ok] gemini          gemini fake 0.0.0  (C:\Users\mggcp\AppData\Local\Temp\hefesto-smoke-8s82tkm0\bin\gemini.CMD)
[--] agy             no encontrado
[··] auth claude     yes
[··] auth codex      yes
[··] auth gemini     yes
[··] límites claude  5 h: unknown, semanal: unknown
[··] límites codex   5 h: 95%, semanal: 15%
[··] límites gemini  5 h: unknown, semanal: unknown
[--] node            no encontrado — opcional: skills externas
[--] npx             no encontrado — opcional: `hefesto skill find`
[··] skills (npm)    versión fijada 1.7.2
[--] shellcheck      no encontrado — opcional: lint de install.sh
[--] graphify        no encontrado — opcional: pack context
[ok] API keys        ninguna en el entorno (uso por suscripción)
[··] proyecto        C:\Users\mggcp\AppData\Local\Temp\hefesto-smoke-8s82tkm0\proyecto
[ok] config          válida (default)
[··] allowlist       claude, codex, gemini
[ok] estado          IDLE tarea=- intentos=0
[ok] lock            libre
[··] packs           ninguno activo

$ hefesto -C C:\Users\mggcp\AppData\Local\Temp\hefesto-smoke-8s82tkm0\proyecto start TASK-001  → 0
TASK-001 en curso (run 36714e2de41a, rol runner).
  rama: hefesto/fase-1-humo  aislamiento: branch  directorio: C:\Users\mggcp\AppData\Local\Temp\hefesto-smoke-8s82tkm0\proyecto
  siguiente: hefesto run runner "<instrucción>" --task TASK-001

$ hefesto -C C:\Users\mggcp\AppData\Local\Temp\hefesto-smoke-8s82tkm0\proyecto run runner Crea src/hola.py  → 0
{
  "run_id": "b60a48be43a0",
  "hops": [
    {
      "entry": "gemini",
      "model_id": "gemini:default",
      "outcome": "respondió"
    }
  ],
  "packet": ".hefesto/logs/packets/TASK-001-runner-8d49d8ce5e.md",
  "success": true,
  "provider": "gemini",
  "model": "default",
  "summary": "creado src/hola.py",
  "error_type": null,
  "usage": {
    "input_tokens": 90,
    "output_tokens": 20
  },
  "log_path": ".hefesto/logs/runs/b60a48be43a0-gemini.log",
  "duration": 0.114
}

$ hefesto -C C:\Users\mggcp\AppData\Local\Temp\hefesto-smoke-8s82tkm0\proyecto run reviewer Revisa el diff  → 0
{
  "run_id": "7f94d330dff8",
  "hops": [
    {
      "entry": "claude:sonnet",
      "model_id": "claude:sonnet",
      "outcome": "respondió"
    }
  ],
  "packet": ".hefesto/logs/packets/TASK-001-reviewer-22b42d7cde.md",
  "success": true,
  "provider": "claude",
  "model": "sonnet",
  "summary": "VEREDICTO: pass\nsin observaciones",
  "error_type": null,
  "usage": {
    "input_tokens": 120,
    "output_tokens": 40
  },
  "log_path": ".hefesto/logs/runs/7f94d330dff8-claude.log",
  "duration": 0.073
}

$ hefesto -C C:\Users\mggcp\AppData\Local\Temp\hefesto-smoke-8s82tkm0\proyecto review TASK-001 --verdict pass --run-id 7f94d330dff8  → 0
Revisión pass registrada para TASK-001 por claude:sonnet.

$ hefesto -C C:\Users\mggcp\AppData\Local\Temp\hefesto-smoke-8s82tkm0\proyecto done TASK-001  → 0
TASK-001 completada: commit 1ed7d26c78, evidencia TASK-001.json.

$ hefesto -C C:\Users\mggcp\AppData\Local\Temp\hefesto-smoke-8s82tkm0\proyecto evidence verify  → 0
Cadena de evidencias íntegra (1 evidencias).

$ hefesto -C C:\Users\mggcp\AppData\Local\Temp\hefesto-smoke-8s82tkm0\proyecto status  → 0
hefesto: COMPLETED
  tarea:     -  intentos: 0
  rol:       -
  rama:      hefesto/fase-1-humo
  roadmap:   1/1 hechas, 0 en curso, 0 bloqueadas, 0 pendientes
  próxima:   todas las tareas están completadas
  siguiente: hefesto finish

smoke: OK
exit 0
```

---

## 5. Demostración de fase paralela con conflicto (Demo)

Salida de `python tools/dev.py demo`: ejecuta primero el ciclo serie y a continuación una fase en paralelo de 3 tareas con conflicto forzado de rebase en TASK-003:

```
  "success": true,
  "provider": "claude",
  "model": "sonnet",
  "summary": "VEREDICTO: pass\nsin observaciones",
  "error_type": null,
  "usage": {
    "input_tokens": 120,
    "output_tokens": 40
  },
  "log_path": ".hefesto/logs/runs/f855983aaf28-claude.log",
  "duration": 0.078
}

$ hefesto -C C:\Users\mggcp\AppData\Local\Temp\hefesto-demo-0zf1mhtt\serie review TASK-001 --verdict pass --run-id f855983aaf28  → 0
Revisión pass registrada para TASK-001 por claude:sonnet.

$ hefesto -C C:\Users\mggcp\AppData\Local\Temp\hefesto-demo-0zf1mhtt\serie done TASK-001  → 0
TASK-001 completada: commit 706fc76cca, evidencia TASK-001.json.

$ hefesto -C C:\Users\mggcp\AppData\Local\Temp\hefesto-demo-0zf1mhtt\serie evidence verify  → 0
Cadena de evidencias íntegra (1 evidencias).

$ hefesto -C C:\Users\mggcp\AppData\Local\Temp\hefesto-demo-0zf1mhtt\serie status  → 0
hefesto: COMPLETED
  tarea:     -  intentos: 0
  rol:       -
  rama:      hefesto/fase-1-humo
  roadmap:   1/1 hechas, 0 en curso, 0 bloqueadas, 0 pendientes
  próxima:   todas las tareas están completadas
  siguiente: hefesto finish

smoke: OK
demo: ciclo serie OK
$ hefesto parallel
TASK-002: integrated (intentos: 1) - integrada con éxito
TASK-001: integrated (intentos: 1) - integrada con éxito
TASK-003: conflict (intentos: 1) - conflicto de rebase con la rama de la fase
demo: TASK-001 integrated
demo: TASK-002 integrated
demo: TASK-003 conflict
demo: runners solapados: sí
demo: rama de TASK-003 conservada
demo: evidencias: OK
demo: OK
exit 0
```

---

## 6. Proveedores instalados, autenticados y autorizados

Estado verificado de las CLIs externas en el sistema:

```
proveedor  instalado        auth  autorizado  estado  hasta  llamadas(loop)
claude     sí (claude.CMD)  yes   sí          ok      -      0             
codex      sí (codex.CMD)   yes   sí          ok      -      0             
gemini     sí (agy.EXE)     yes   sí          ok      -      0             
```

---

## 7. Desviaciones, aspectos no verificados y ADRs

Relación de puntos que no pudieron ser verificados directamente en la documentación o en las CLIs, junto con las decisiones de arquitectura adoptadas en `DECISIONS.md` y `.hefesto/DECISIONS.md`:

1. **Soporte de Windows nativo (ADR-001):**
   La especificación original limitaba el soporte a Linux, macOS y WSL. En la máquina de desarrollo las CLIs principales (`codex`, `gemini`/`agy`, `node`, `graphify`) están instaladas en Windows nativo. Se implementó soporte completo para Windows (lanzador `hefesto.cmd`, detección de procesos con `ctypes`/`OpenProcess`, y ejecución de `verify` con el Bash de Git for Windows).

2. **Retirada de la CLI `gemini` para cuentas personales (ADR-002, LEARNINGS 2026-10-14 TASK-702):**
   Google retiró la CLI directa de Gemini para cuentas individuales (`IneligibleTierError`). El soporte para Gemini se presta a través del binario `agy` (Antigravity CLI), soportado transparentemente por `hefesto` con `adapters.gemini.binary = "agy"`. En modo headless `-p`, `agy` no admite ejecución de órdenes shell arbitrarias y requiere instrucciones acotadas de edición de archivos.

3. **Límites de cuota restante no expuestos (ADR-002, ADR-010):**
   Ni Claude ni Gemini exponen mediante sus CLIs el porcentaje o valor de cuota restante de forma legible por scripts. En `hefesto limits` y en el preflight preventivo de cuota, sus valores se reportan rigurosamente como `unknown`. Solo Codex proporciona datos analizables a través de sus registros de sesión local.

4. **Instalación de PATH en PowerShell sin tests automatizados en CI (LEARNINGS 2026-10-14 TASK-809):**
   La rama de PowerShell de `install.sh --add-to-path` en Windows interactúa directamente con el registro de usuario (`HKCU\Environment`). Para evitar alteraciones accidentales del entorno del desarrollador, los tests de regresión usan `HEFESTO_PATH_DRY_RUN=1`. La llamada real en producción funciona, pero la API estándar expande variables (`%VAR%`) a cadenas literales `REG_SZ`.

5. **Pack `memory` (`basic-memory`) sin versión npm fijada:**
   El pack `memory` está preparado para integrarse con `basic-memory`, pero al no contar con una versión formal fijada en npm durante la validación, el framework opera de forma predeterminada mediante el flujo nativo de archivos auditables en Markdown (`.hefesto/LEARNINGS.md` y `~/.hefesto/memory/`), garantizando que ninguna lección se almacene en memoria sin revisión interactiva humana.

6. **Sandbox `workspace-write` de Codex en Windows (ADR-015, LEARNINGS 2026-10-14 TASK-710):**
   El sandbox `workspace-write` de Codex en Windows restringe estrictamente la escritura fuera del worktree actual de la tarea. Debido a esto, `hefesto note` no puede escribir en `.hefesto/progress.jsonl` del repositorio raíz cuando Codex actúa como worker.

7. **Patrones de detección de cuota (ADR-010):**
   Los patrones de detección de errores de `quota` y `rate_limit` en los adaptadores se basan en firmas conocidas de error reportadas por las comunidades de cada CLI, sin haber inducido agotamiento forzado deliberado de cuotas en las cuentas de usuario durante las pruebas automáticas.

---

## 8. Siguiente paso: Fase 11 «Aprendizaje» (v2.2)

Tal como se acordó y documentó en la última decisión de `.hefesto/DECISIONS.md` (2026-10-15):

Tras la publicación y congelación de la versión 2.1.0, el trabajo continuará en la **Fase 11 «Aprendizaje»**, incorporando un harness de aprendizaje técnico continuo inspirado en Hermes Agent (Nous Research), adaptado a la filosofía estricta de Hefesto (solo suscripciones, archivos Markdown transparentes y auditables, y revisión humana obligatoria):

1. **Extractor determinista de lecciones:** análisis automático de las evidencias generadas (`evidence/TASK-*.json`) para extraer candidatos a lecciones técnicas en `~/.hefesto/memory/_pendientes/`.
2. **Propuestas asistidas de skills:** detección de patrones de código repetidos en dos o más tareas para redactar borradores automáticos en `skills/_proposals/` listos para `hefesto promote`.
3. **Enrutado por histórico de éxito:** selección inteligente del mejor modelo según el rol y tipo de tarea, con explicación detallada en `hefesto route --dry-run`.
4. **Revisión adversarial de instrucciones:** validación previa de los prompts de delegación del orquestador antes de pasarlos a los runners.
5. **Piloto automático programado:** ejecución diferida de `hefesto loop` alineada con las horas de reinicio de cuota de los proveedores mediante el programador de tareas del sistema operativo, sin daemons residentes.
6. **Adaptador hermes opcional:** conector opcional y desactivado por defecto para modelos locales vía Ollama, sin costes de API.

---

## 9. Instrucciones de publicación

La publicación y el despliegue de versiones son acciones **exclusivamente humanas** que nunca ejecuta Hefesto de forma autónoma. En este repositorio:

- Todo el trabajo validado y completado de las distintas fases reside en la rama `build/hito-1`, donde `hefesto finish` fue integrando sucesivamente cada fase. A la rama `main` **no** se integra mediante `hefesto finish`.
- El repositorio remoto `origin` ya existe configurado (`https://github.com/MGarcia3849/Hefesto.git`), por lo que no se debe ejecutar `gh repo create`.

Pasos exactos que debe ejecutar el usuario:

1. Cambiar a la rama `main`:
   ```bash
   git checkout main
   ```
2. Integrar el trabajo completado de `build/hito-1` avanzando de forma lineal (*fast-forward*):
   ```bash
   git merge --ff-only build/hito-1
   ```
3. Subir la rama `main` actualizada a `origin`:
   ```bash
   git push origin main
   ```
4. Crear la etiqueta anotada para la versión v2.1.0:
   ```bash
   git tag -a v2.1.0 -m "Hefesto v2.1.0"
   ```
5. Subir la etiqueta a `origin`:
   ```bash
   git push origin v2.1.0
   ```
6. Publicar la release en GitHub extrayendo únicamente la sección `v2.1.0` de `CHANGELOG.md` a un archivo temporal (no pasar el changelog completo):
   - En Linux/macOS/Git Bash:
     ```bash
     awk '/^## v2.1.0/{f=1; print; next} /^## /{f=0} f' CHANGELOG.md > /tmp/release-notes-v2.1.0.md
     gh release create v2.1.0 --title "Hefesto v2.1.0" --notes-file /tmp/release-notes-v2.1.0.md
     rm /tmp/release-notes-v2.1.0.md
     ```
   - O copiando manualmente la sección de la versión `v2.1.0` de `CHANGELOG.md` a un archivo temporal (p. ej. `notes-2.1.0.md`):
     ```bash
     gh release create v2.1.0 --title "Hefesto v2.1.0" --notes-file notes-2.1.0.md
     ```
