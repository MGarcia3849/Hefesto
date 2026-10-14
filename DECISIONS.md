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
