# ROADMAP

<!--
Hefesto construyéndose a sí mismo (sección 13 de HEFESTO_V2_1.md).
Hitos 1 y 2 se construyeron directamente (ADR-016); desde aquí, con el propio ciclo de Hefesto.
-->

## Fase 7 — Distribución
- [ ] TASK-701: Contratos de distribución y de enrutado del revisor
  - verify: `python -m py_compile tests/test_distribution.py tests/test_reviewer_routing.py && python -m unittest tests.test_distribution tests.test_reviewer_routing 2>&1 | grep -q "FAILED"`
  - files: tests/test_distribution.py, tests/test_reviewer_routing.py
  - role: core
  - depends_on: none
  - size: S
  - parallel: false
  - contract: tests/test_distribution.py, tests/test_reviewer_routing.py
  - accept: los tests compilan y fallan porque aún no existen hefesto/distribution.py ni los comandos bootstrap, adopt, self-update y migrate
- [ ] TASK-702: El revisor nunca se enruta a un proveedor que escribió el cambio
  - verify: `python -m unittest tests.test_reviewer_routing && python tools/dev.py lint`
  - files: hefesto/router.py
  - role: runner
  - depends_on: TASK-701
  - size: S
  - parallel: false
  - accept: `hefesto run reviewer` salta los proveedores autores de la tarea en curso salvo que no quede otro; el salto aparece en los hops
- [ ] TASK-703: Detección de modo y de stack, comandos bootstrap y adopt
  - verify: `python -m unittest tests.test_distribution.ModeTests tests.test_distribution.StackTests tests.test_distribution.BootstrapTests tests.test_distribution.AdoptTests && python tools/dev.py lint`
  - files: hefesto/distribution.py, hefesto/cli.py
  - role: runner
  - depends_on: TASK-701
  - size: M
  - parallel: false
  - accept: crear/continuar/mejorar se detectan de forma determinista; adopt no modifica ningún fichero de código del proyecto
- [ ] TASK-704: self-update, migrate e instalación desde un repositorio git
  - verify: `python -m unittest tests.test_distribution && python tools/dev.py lint`
  - files: hefesto/distribution.py, hefesto/cli.py, hefesto/doctor.py
  - role: runner
  - depends_on: TASK-703
  - size: M
  - parallel: false
  - accept: self-update sin confirmación no cambia de versión; migrate hace copia de seguridad; install.sh repetido no rompe nada
- [ ] TASK-705: BOOTSTRAP.md, CHANGELOG.md e instrucciones de publicación
  - verify: `test -f BOOTSTRAP.md && grep -q "hefesto bootstrap" BOOTSTRAP.md && grep -q "v2.1.0" CHANGELOG.md && grep -q "BOOTSTRAP.md" AGENTS.md && grep -q "gh repo create" README.md`
  - files: BOOTSTRAP.md, CHANGELOG.md, README.md, AGENTS.md
  - role: runner
  - depends_on: TASK-704
  - size: S
  - parallel: false
  - accept: un agente que solo recibe la URL del repositorio sabe instalar Hefesto y decidir entre crear, continuar o mejorar
