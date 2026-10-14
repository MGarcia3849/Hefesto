---
name: hefesto-skill-retro
description: Formato de LEARNINGS y de los ADR de Hefesto, consolidación de lecciones y criterio para proponer una skill. Úsala al cerrar una tarea con /hefesto-retro o al revisar la memoria del proyecto.
---

# retro

**Entrada:** una tarea completada o bloqueada, con su evidencia y sus notas de progreso.
**Salida:** 0–1 lección en `.hefesto/LEARNINGS.md`, 0–1 ADR en `.hefesto/DECISIONS.md`, y opcionalmente una propuesta de skill.

## LEARNINGS

- Una línea por lección (máximo tres), en presente y verificable: `- AAAA-MM-DD TASK-NNN [stack]: lección.`
- Solo lo que cambiaría cómo se hace la próxima tarea. Nada de resúmenes de lo hecho.
- Al superar ~150 entradas: agrupa por stack o módulo, fusiona duplicadas y elimina las que ya están en el código o en un ADR. La consolidación es una tarea propia del ROADMAP, no se hace de pasada.

## ADR

Solo si se eligió entre alternativas:

```
## AAAA-MM-DD — Título
**Contexto:** …
**Decisión:** …
**Consecuencias:** …
```

## Cuándo proponer una skill

Cuando la misma lección (o el mismo tipo de error) aparece dos o más veces. Usa `hefesto-skill-skill-creator`; nunca actives la skill tú.

## Criterios de aceptación

- Cada lección cabe en tres líneas y nombra su tarea.
- No hay secretos, datos personales ni trabajo de terceros.
