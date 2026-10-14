@./AGENTS.md

## Notas para Gemini CLI

- Tu primera acción es `hefesto status` (lo inyecta el hook `SessionStart`); si hay tarea en curso, `hefesto resume`.
- El ciclo de una tarea es `/hefesto:next`.
- Delega siempre con `hefesto run ROL "…"`; no hay subagentes nativos en este protocolo.
