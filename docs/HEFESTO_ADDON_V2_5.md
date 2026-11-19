# Add-on v2.5

Este documento reúne cuatro módulos incorporados en las Fases 23–26. Los comandos se muestran con las opciones registradas en `hefesto/cli.py`; `<id>` identifica el registro local que crea cada flujo.

## Módulos

### Aprobación de movimiento (`hefesto approval`)

La aprobación se limita a una propuesta concreta: proyecto, página, tipo (`app` o `web`), efecto (`animation`, `video`, `motion-graphic` o `3d`) y huella. Los gates del ciclo comprueban los cambios en diseño, delegación, checkpoint, integración y despliegue. El movimiento ya existente se conserva.

```text
hefesto approval propose --page P --kind K --effect E "descripción"
hefesto approval approve <id>
hefesto approval revoke <id>
hefesto approval list
```

`propose` no concede permiso. `approve` requiere una terminal interactiva y confirmación explícita; no ofrece `--yes`. Se puede revocar una aprobación en cualquier momento; los gates dejan de aceptarla. Una TTY y la ausencia de variables de agente son condiciones necesarias, pero no constituyen autenticación robusta por sí solas: el registro protegido, su huella y los gates aportan las demás barreras.

### Réplica analítica (`hefesto replica`)

Compara una referencia web pública autorizada con páginas del proyecto y genera inventarios y una propuesta documental.

```text
hefesto replica authorize <id> <url>
hefesto replica capture <id> [--route RUTA]
hefesto replica inventory <id> --page RUTA=FICHERO [--page RUTA=FICHERO ...]
hefesto replica propose <id>
hefesto replica accept <id>
```

Solo analiza sitios públicos dentro del alcance autorizado. No clona sitios, copia recursos o código, ni aplica cambios al proyecto. La autorización y aceptación requieren confirmación humana interactiva.

### Seguridad (`hefesto security`)

El escáner lee los ficheros del proyecto localmente y produce un informe reproducible. Los gates de cierre y despliegue bloquean únicamente hallazgos `critical` o `high`, confirmados y dentro de alcance.

```text
hefesto security scan [--profile PERFIL] [--json]
hefesto security except <id> --person PERSONA --reason MOTIVO --expires AAAA-MM-DD
hefesto security exceptions
```

El escaneo es pasivo: no abre la red ni ejecuta el código analizado. Es un conjunto limitado de reglas y heurísticas, no una auditoría de seguridad ni una certificación de cumplimiento. El estado jurídico o normativo del informe se expresa como `NO_VERIFICADO`. El perfil `centuria-policial` usa únicamente datos sintéticos; no debe interpretarse como un sistema policial real.

### Experiencia visual (`hefesto visual-experience`)

Inspecciona el estado visual y prepara propuestas estáticas; no implementa cambios.

```text
hefesto visual-experience inspect [--kind KIND] [--json]
hefesto visual-experience propose --page PAGINA --kind KIND --effect EFECTO [--step FOTOGRAMA ...] "descripción"
hefesto visual-experience apply <id> [--base BASE]
hefesto visual-experience verify [--base BASE] [--kind KIND]
```

`inspect` y `propose` escriben documentos en `.hefesto/visual-experience/`. `apply` prepara instrucciones únicamente con una aprobación puntual; `verify` consulta el gate de movimiento existente. La estimación de coste no mide rendimiento real ni sustituye una auditoría de accesibilidad.

## Pruebas

Desde la raíz del repositorio:

```bash
python -m unittest tests.test_addon_approval tests.test_addon_replica tests.test_addon_security tests.test_addon_visual
python -m unittest tests.test_distribution
python tools/dev.py lint
python tools/dev.py smoke
```

Las pruebas de réplica usan fixtures y servicios falsos locales; no requieren autorización para navegar a una web real.

## Migración y rollback

`hefesto migrate` crea primero `.hefesto/backups/<fecha>/` y copia los ficheros existentes `settings.json`, `ROADMAP.md`, `DECISIONS.md` y `LEARNINGS.md`. Añade `visual.motion = "static"` y los valores ausentes de los perfiles de seguridad sin sustituir preferencias ya guardadas. La operación es idempotente: si el proyecto ya está al día, no crea otra copia ni vuelve a migrar.

Para restaurar manualmente, detén Hefesto en ese proyecto, elige la carpeta de respaldo creada por la migración y copia de vuelta únicamente los ficheros que quieras revertir. Por ejemplo, restaura `settings.json` a `.hefesto/settings.json`; restaura los documentos restantes a sus rutas originales solo si también necesitas revertirlos. Conserva la carpeta de backup hasta comprobar el resultado. La restauración no es automática y no revierte otros cambios hechos después de la migración.

## Límites de entrega

El add-on no ejecuta `git push`, no integra ramas automáticamente y no despliega automáticamente. La integración y cualquier entrega permanecen sujetas a los pasos explícitos y autorizaciones humanas del flujo Hefesto.
