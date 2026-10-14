@AGENTS.md

## Notas para Claude Code

- El hook `SessionStart` inyecta `hefesto status`; si no aparece, ejecútalo tú.
- El hook `PreToolUse` (`hefesto hook pre-tool-use`) bloquea comandos peligrosos y rutas protegidas.
- Delega con `hefesto run ROL "…"` para que quede registrado quién hizo cada cosa.
