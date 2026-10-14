# Lecciones

<!-- 1–3 líneas por lección. Al superar ~150 entradas, consolidar. -->
- 2026-10-14 TASK-702 [gemini]: la CLI `gemini` está retirada para cuentas individuales (IneligibleTierError: migrar a Antigravity); usar `adapters.gemini.binary = "agy"`.
- 2026-10-14 TASK-702 [agy]: en `-p` headless, agy deniega los comandos de shell y devuelve `status: SUCCESS` con `response` vacía y `denied_actions`; hay que pedirle solo edición de ficheros. Tarda 6–9 min por tarea M.
- 2026-10-14 TASK-702 [adapters]: errores no reconocidos (IneligibleTierError, 503 UNAVAILABLE) se tratan como fallo del trabajo y no hacen fallback; deben clasificarse como auth/rate_limit.
- 2026-10-14 TASK-701 [guard]: el hook bloquea `2>&1` junto a una ruta de control (falso positivo: confunde la redirección de stderr con escritura).
- 2026-10-14 TASK-701 [proceso]: Codex como revisor encuentra defectos reales en contratos (2 rechazos útiles de 3); el 3.er intento quedó al borde del circuit breaker.
- 2026-10-14 TASK-710 [seguridad]: una lista negra de argumentos para comandos «seguros» no resiste revisión (8 evasiones: saltos de línea, comillas escapadas, expansión de llaves de Bash, ruff --fix-only, go -exec/-toolexec/-vettool, -C "" y HEFESTO_ROLE=""); hace falta lista blanca de herramientas y opciones y rechazar caracteres de expansión.
- 2026-10-14 TASK-710 [codex]: como trabajador, su sandbox workspace-write (cwd = worktree) no puede escribir en `.hefesto/` del proyecto principal: `hefesto note` falla desde Codex.
- 2026-10-14 TASK-710 [proceso]: el escalado runner→runner→forge funcionó; con autores gemini+codex, el revisor se enrutó solo a claude:sonnet (TASK-702 en uso).
- 2026-10-14 TASK-703 [cuota]: Codex agotó su cuota en una revisión; Hefesto lo clasificó (quota), aplicó cooldown con la hora del mensaje y la revisión pasó a claude:sonnet sin gastar intento. Validado en real.
- 2026-10-14 TASK-711 [config]: con Codex sin cuota, el rol Forge se quedaba sin opción útil (Claude era revisor necesario); añadir gemini a roles.forge.use evitó esperar ~30 min. Las reglas ask de prefijo de Claude se esquivan con opciones globales: cubrir también `hefesto -C:*`/`--project:*`.
- 2026-10-14 TASK-713 [seguridad]: tercera vez el mismo patrón (710, 711, 713): cada lista negra o enmascarado se evade (sustituciones en comillas dobles, comillas escapadas, almohadilla que shlex trata como comentario). Solo funcionan las listas blancas estrictas. Candidata a skill: «barreras con lista blanca».
- 2026-10-14 TASK-713 [proceso]: circuit breaker real; el humano desbloqueó con una nota acotada y el 4.º intento se centró solo en el defecto. Pendiente (no bloqueante): REDIRECTION_STRIP del guard quita redirecciones a /dev/null sin comprobar lo que sigue.
- 2026-10-14 TASK-714 [diseño]: finish reejecutaba el verify de las tareas de contrato, que comprueba que los tests FALLAN; tras implementar siempre falla. Solo se detectó al integrar la primera fase real.
