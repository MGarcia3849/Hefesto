---
name: hefesto-skill-skill-creator
description: Investiga una API, librería o patrón repetido y redacta una skill nueva en skills/_proposals/ con su justificación. Úsala cuando la retro detecte un patrón que se repite dos o más veces. Nunca activa la skill.
---

# skill-creator

**Entrada:** un patrón repetido (al menos dos entradas en LEARNINGS) o una API/paquete que el proyecto usa a menudo.
**Salida:** `skills/_proposals/<nombre>/SKILL.md` + `skills/_proposals/<nombre>/JUSTIFICACION.md`.

## Procedimiento

1. Comprueba que no existe ya una skill parecida (`hefesto skill <término>` cuando esté disponible, o busca en `skills/` y `.claude/skills/`).
2. Investiga en la documentación oficial; cita las URLs y la versión consultada. No inventes APIs.
3. Escribe `SKILL.md` con frontmatter (`name`, `description` precisa de **cuándo** usarla), entrada, salida, procedimiento y criterios de aceptación verificables.
4. Escribe `JUSTIFICACION.md`: qué lecciones o tareas la motivan, qué ahorra y qué riesgos tiene.
5. Sin scripts ejecutables, sin instrucciones que salten permisos y sin referencias a secretos.
6. Deja una nota: `hefesto note "propuesta de skill <nombre> en _proposals"`. **Nunca** la actives ni la copies a `.claude/skills/`: la incorpora el usuario con `hefesto promote`.

## Criterios de aceptación

- Ambos ficheros existen en `_proposals/` y nada cambia fuera de esa carpeta.
- La descripción permite decidir en una línea cuándo cargar la skill.
