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

## 2026-10-16 — Fase 11: skills en el paquete de tarea; «Aprendizaje» pasa a Fase 12

Pedido por el usuario en el chat (2026-10-16): Hefesto debe elegir las skills que aplican a cada tarea y pasárselas al trabajador, sin que el Core lo haga a mano. La especificación ya lo exigía (sección 5.3: el paquete lleva «reglas del rol y las skills que aplican») y no se construyó. Reglas: selección determinista (sin LLM) entre skills ya instaladas (framework, promovidas, proyecto y carpetas del usuario); el texto de la skill va dentro del paquete para que sirva a cualquier proveedor; buscar en skills.sh sigue siendo solo una propuesta (`skill suggest --online`) y descargar o promover sigue siendo decisión humana (sección 11.1). La fase «Aprendizaje» decidida el 2026-10-15 se renumera a Fase 12 sin otros cambios.


## 2026-10-16 — La rama principal pasa a ser `main`

`build/hito-1` era la rama de integración de los hitos iniciales y ya coincide con `main` (ef7f673), que es la publicada en GitHub. Desde la Fase 11 se integra directamente en `main` (`execution.main_branch` en `.hefesto/hefesto.toml`).

## 2026-10-16 — Fase 12: instalación automática de skills; «Aprendizaje» pasa a Fase 13

Decidido por el usuario en el chat (2026-10-16), eligiendo expresamente «totalmente automática» frente a «solo fuentes fiables» y «preguntar antes, como Vercel», y confirmándolo de nuevo tras avisarle de que quita la confirmación humana: Hefesto busca en skills.sh, descarga y promueve sin preguntar la mejor skill para cada tarea cuando las instaladas no bastan. Esto sustituye, solo para este circuito, la regla de la sección 11.1 de que `promote` es siempre humano. Salvaguardas que se mantienen: filtros de calidad de la propia skill de Vercel (instalaciones y estrellas mínimas; un dato desconocido descarta), el informe de riesgos de `fetch` descarta cualquier skill con un riesgo detectado, nunca se ejecuta `npx skills add` ni código de la skill, un trabajador delegado (`HEFESTO_ROLE`) nunca instala, y todo queda registrado en `skills/auto.jsonl`. Se desactiva con `skills_auto.enabled = false`. La fase «Aprendizaje» pasa a ser la Fase 13.

## 2026-10-16 — Fase 13: dependencias compartidas en los worktrees; «Aprendizaje» pasa a Fase 14

Encontrado al hacer `finish` de la Fase 2 de SENTINEL: la suite se ejecuta en un worktree temporal sin las carpetas ignoradas de la copia principal (`node_modules`) y `ng build` falla sin que el código tenga nada mal. Elegido por el usuario en el chat frente a un atajo puntual: `[execution] worktree_links` enlaza esas carpetas (junction en Windows, enlace simbólico en el resto) en el worktree de `finish`, en el de la fase y en los de tareas en paralelo, solo si están ignoradas por git. Probado antes de diseñar: `git worktree remove`, con o sin `--force`, entra por una junction y borra el contenido real; `shutil.rmtree` no. Por eso todo borrado de un worktree pasa por `gitops.remove_worktree`, que quita antes los enlaces, y un test impide volver a llamar a `git worktree remove` fuera de `gitops.py`. La fase «Aprendizaje» pasa a ser la Fase 14.

## 2026-10-17 — Fases 14 a 18: aprendizaje, catálogo de skills, verificación visual, Obsidian y Odysseus

Pedido por el usuario en el chat (2026-10-17) tras analizar ECC (affaan-m/ECC, MIT), web-design-guidelines (vercel-labs, MIT), getdesign.md / awesome-design-md (VoltAgent, MIT), playwright-cli (Microsoft, Apache-2.0), taste-skill (Leonxlnx, MIT), impeccable (pbakaus, Apache-2.0), Obsidian y Odysseus (AGPL-3.0). Integrado todo de una vez, en cinco fases para que cada `finish` sea pequeño.

- **Regla del usuario: las skills no pertenecen a ningún proveedor.** El orquestador decide qué agente o subagente usa cada skill o herramienta, en qué modo y para qué (`catalog/skills.toml`: roles, modos, `kind` de tarea, puertas interactivas). Dónde esté instalada una skill en disco (`~/.codex/skills`…) no decide quién la usa.
- **Fase 14 «Aprendizaje»**: mantiene lo decidido el 2026-10-15 y toma de ECC la confianza y el alcance proyecto/global de las lecciones (continuous-learning-v2), pass@k para el enrutado (eval-harness) y las reglas por lenguaje en el paquete. El adaptador `hermes` de aquella ADR queda absorbido por el adaptador `local` ya existente y su uso como último recurso (TASK-1804).
- **Fase 15 «Catálogo de skills»**: fuentes con commit fijado y traídas con `fetch` + informe de riesgos; nunca `npx skills add` ni instaladores de terceros; AgentShield opcional (binario instalado y revisado, nunca npx) antes de promover. No se instala ECC entero: sus hooks y su orquestador competirían con Hefesto.
- **Fase 16 «Diseño y verificación visual»**: DESIGN.md/PRODUCT.md como contexto fijo; `impeccable detect` (determinista) y las capturas de `playwright-cli` como verify, porque revisar no es verificar; telemetría de impeccable siempre desactivada (`DO_NOT_TRACK=1`); impeccable se usa como indica su flujo (pedido expreso del usuario, referencia: webreactiva.com/blog/impeccable): `init` una vez por proyecto con el humano (PRODUCT.md con anti-referencias; sin él nadie recibe impeccable), `document` para DESIGN.md en proyectos existentes, y por tarea visual diagnóstico (`critique`/`audit`) → comandos sugeridos, uno por delegación con checkpoint → `polish` siempre al final; nunca `bolder` sin diagnóstico, `animate` al final, `craft` (deprecado en v4) nunca; `init` y `shape` solo para el Core con un humano; las excepciones del detector solo las añade el Core, con motivo; el detector no prueba que la página funcione, por eso va junto a las capturas.
- **Fase 17 «Bóveda Obsidian»**: Obsidian solo lee; state.json, trust.json y los marcadores del ROADMAP siguen siendo la única fuente. La única entrada desde Obsidian es la propiedad `revision` de las candidatas de memoria, que un humano aplica con `memory review --from-vault` (TTY).
- **Fase 18 «Odysseus»**: solo como servicio externo opcional por su API HTTP (`/api/research/*`, token `ody_` en una variable de entorno), nunca copiando código: es AGPL-3.0 y Hefesto está publicado. Odysseus no expone un `/v1/chat/completions` propio (comprobado en su código): el adaptador local apunta al servidor de modelos que lanza.

## 2026-10-17 — Desbloqueo de TASK-1408

Contrato defectuoso corregido por el Core dentro de la tarea; se separa en TASK-1410 (autorizado por el usuario en el chat 2026-10-17)
