---
name: hefesto-skill-git-checkpointer
description: Reglas de commit de Hefesto. Úsala cuando vayas a guardar el trabajo de una tarea o dudes de qué puede entrar en un commit; el commit real lo hace siempre `hefesto done`.
---

# git-checkpointer

**Entrada:** una tarea en curso con su `verify` en verde y revisión `pass`.
**Salida:** un commit `hefesto(TASK-NNN): descripción [verified]` en la rama `hefesto/<fase>` y otro con la evidencia.

## Reglas

1. No hagas commits a mano: `hefesto done TASK` los hace con todas las comprobaciones.
2. Commits solo en ramas `hefesto/*`. Nunca `git push` ni integración en la rama principal: eso es `hefesto finish`, con confirmación humana.
3. Solo entran los ficheros de `files` de la tarea (y los nuevos bajo esas rutas). Nunca `git add -A` ni `git add .`.
4. Hefesto bloquea el commit si hay rutas fuera de alcance, secretos (por nombre o por contenido: claves, tokens, cadenas de conexión) o binarios grandes. Si se bloquea, corrige la causa; no la esquives.
5. Ante una interrupción, nunca `reset --hard`, `checkout -- .` ni borres worktrees con cambios: deja una nota (`hefesto note`) y avisa al usuario.

## Criterios de aceptación

- `git log -1` muestra `hefesto(TASK-NNN): … [verified]` seguido del commit de evidencia.
- `git status` no muestra ficheros de la tarea pendientes.
- `hefesto evidence verify` sale con 0.
