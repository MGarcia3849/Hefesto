# Decisiones (ADR)

<!-- Una entrada por decisión: fecha, contexto, decisión y consecuencias. Hefesto añade aquí los desbloqueos y descartes. -->

## 2026-10-14 — Atribución errónea del revisor en la evidencia de TASK-703

La evidencia `TASK-703.json` registra `reviewer: codex:default`, pero la revisión aprobada la hizo `claude:sonnet` (run `80777a8f0f7b`): Codex agotó su cuota en ese mismo run y el enrutador pasó a Claude. `hefesto review --run-id` tomó el primer registro del run (el fallido). Las evidencias no se reescriben (cadena de hashes); se corrige el fallo en TASK-712 y queda constancia aquí.

## 2026-10-14 — Desbloqueo de TASK-713

Autorizado por el usuario en el chat (2026-10-14). La parte de adaptadores es correcta; los tres intentos fallaron por evasiones del hook. Nuevo intento centrado en que el lexer de is_plain_hefesto_invocation no trate la almohadilla como comentario (commenters vacío o rechazar la almohadilla), con tests, conservando el resto del intento 3.

## 2026-10-14 — Desbloqueo de TASK-803

Autorizado por el usuario en el chat (2026-10-14). Lo construido es correcto y está cubierto por 51 tests; los tres intentos fallaron por defectos de seguridad cada vez más profundos. Nuevo intento acotado a: nombres de fichero con lista blanca estricta (sin dos puntos, sin punto o espacio final, sin colisiones por mayúsculas o normalización), saneado de caracteres de control en todo lo que se imprime de la skill, y topes de tamaño por fichero, tamaño total y número de ficheros.

## 2026-10-14 — Corrección del verify de TASK-817 (error del orquestador)

El verify de TASK-817 incluía la suite completa, que contiene los tests de contrato de TASK-806 (aún pendiente), así que `done` falló por un motivo ajeno a la tarea y consumió un intento. Se sustituye por los tests propios de la tarea y el lint; la suite completa la exige TASK-806. Lección: el verify de una tarea no puede depender de tareas posteriores.
