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

## 2026-10-15 — Corrección del contrato de TASK-904 durante TASK-905 (error del orquestador)

Dos defectos del contrato, no de la implementación: (1) el worker falso intentaba escribir en `src/` (un directorio) y el verify `test -f src/` nunca podía pasar; ahora escribe un fichero dentro y el verify es `test -d src`. (2) «nada se escribe en la copia principal» es falso con aislamiento `branch`, donde la copia principal ES el directorio de la fase; la garantía real (cada agente trabaja en su worktree) ya la comprueba el `cwd` de las llamadas, y la aserción solo se aplica con aislamiento `worktree`. Las ramas de tarea son `<rama de fase>--TASK-XXX`, no `hefesto/<fase>/TASK-XXX`, porque git no admite una ref anidada bajo una rama existente.

## 2026-10-15 — Descarte de TASK-905

Descarte de la tarea en curso TASK-905.

## 2026-10-15 — TASK-907 sin dependencia de TASK-904

La guarda de `review` (contrato de una dependencia modificado → revisión negativa automática) no distingue el rol: también saltó en TASK-907, una tarea `core` cuyo único fin es enmendar ese contrato, lo que el protocolo sí permite al Core. TASK-904 ya está hecha, así que la dependencia no aporta orden; se elimina. Mejora pendiente (Fase 10): que la guarda exima a las tareas de rol `core`.

## 2026-10-15 — Desbloqueo de TASK-906

Autorizado por el usuario en el chat (2026-10-15). Lo construido es correcto y está cubierto por 32 tests; las dos primeras revisiones encontraron fallos de seguridad ya corregidos. Nuevo intento acotado a 4 puntos de robustez: relevant() no rompe resume/packet ante enlaces (aviso y se ignora); _check_path no rechaza un HEFESTO_HOME enlazado legítimo (el confinamiento ya usa la ruta resuelta); resume pasa las notas por guard.redact; una candidata inválida se salta con aviso sin abortar review.

## 2026-10-15 — Fase 11 (v2.2): harness de aprendizaje tipo Hermes

Decidido por el usuario en el chat. Tras publicar la v2.1, Fase 11 «Aprendizaje» con las ideas de Hermes Agent (Nous Research) implementadas en Hefesto y con sus reglas (solo suscripciones, ficheros auditables, nada entra en memoria sin revisión humana): (1) extractor determinista de lecciones desde evidencias → `_pendientes/`; (2) borradores de skills desde patrones repetidos → `skills/_proposals/` + `promote`; (3) enrutado por historial de éxito por modelo × tipo de tarea, explicado en `route --dry-run`; (4) revisión adversarial de las instrucciones antes de delegar; (5) piloto automático programando `hefesto loop` con el planificador del SO al reinicio de cuotas, sin daemon; (6) avisos cuando algo necesita aprobación. Última tarea: adaptador `hermes` opcional, desactivado por defecto, solo con modelos locales (Ollama), sin claves de API. Se descarta Hermes con API de pago, su memoria de captura automática, Docker y servidores. Contratos primero (Core, en tarea propia) y tareas S/M.
