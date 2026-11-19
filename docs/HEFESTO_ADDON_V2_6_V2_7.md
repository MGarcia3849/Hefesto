# Add-ons v2.6 y v2.7

Esta guía resume las especificaciones de tarea, el benchmark offline, las mejoras de fiabilidad y la revisión local previa al despliegue. Los resultados son señales operativas para revisar; no sustituyen pruebas del producto ni una evaluación profesional.

## Especificación opcional del ROADMAP

Una tarea puede incluir estos campos en su bloque:

```markdown
- scope: API de perfiles y validación de entradas
- out_of_scope: rediseño de la interfaz
- risks: migración incompatible si cambia el esquema
- requires_approval: deploy, external_data
```

`scope`, `out_of_scope` y `risks` son texto. `requires_approval` es una lista separada por comas; sus valores aceptados son `install`, `deploy`, `animation`, `external_data` y `architecture`. Expresan requisitos declarados para la tarea: **no conceden permisos** ni reemplazan las autorizaciones y barreras propias de Hefesto.

`hefesto doctor` incluye el chequeo `instrucciones`, que informa si las instrucciones de las distintas CLI son coherentes. Un aviso señala una discrepancia que debe revisarse; el chequeo no autentica instrucciones externas.

## Benchmark local

Ejecuta los doce escenarios offline:

```console
hefesto bench
hefesto bench --scenario easy-bug --scenario crash-resume --report bench.json
hefesto bench --report actual.json --compare anterior.json
```

Los escenarios usan **proveedores simulados** y tienen **coste comercial cero**; no llaman a proveedores comerciales ni consumen su cuota. `--report` guarda el resultado JSON y `--compare` lo compara con un informe previo para señalar regresiones. La comparación advierte «muestra pequeña» si alguno de los informes tiene menos de cinco ejecuciones.

El benchmark mide cómo se comporta el flujo y sus barreras ante escenarios reproducibles: estados de tarea, verificación, falsas finalizaciones, reintentos, bloqueos y duración, según el escenario. No mide calidad general del código, rendimiento de proveedores reales, coste de producción, ni demuestra que una aplicación funcione o sea segura fuera de esos casos simulados. Una muestra pequeña puede producir diferencias ruidosas.

## Fiabilidad del harness

- Los registros `delegations.jsonl` incluyen `error_class` para clasificar el error de cada delegación.
- `limits.transient_retries` tiene valor predeterminado `0`; `limits.retry_backoff_seconds` controla la pausa entre reintentos cuando se habilitan. Sin configurar reintentos no hay reintentos transitorios automáticos.
- `hefesto done TASK` puede repetirse tras un corte: la finalización detecta el checkpoint ya realizado y no vuelve a aplicar sus efectos.
- `hefesto resume` identifica delegaciones que quedaron interrumpidas para continuar el ciclo desde su estado registrado; no asume que una llamada interrumpida haya terminado correctamente.

## Revisión previa al despliegue

Comando disponible:

```console
hefesto security predeploy
hefesto security predeploy --profile web-general --report predeploy.md
```

Los controles A–E cubren `costs` (costes), `accessibility` (accesibilidad), `data-access` (acceso a datos), `communications` (comunicaciones) y `abuse` (abuso). Cada control y hallazgo puede tener estado `confirmed`, `suspected`, `not_tested`, `not_applicable` o `passed`. Un control no ejecutado queda `not_tested`; no se presenta como aprobado. Los veredictos son `PASS`, `REVIEW_REQUIRED` y `BLOCKED`: indicios o controles sin probar requieren revisión; hallazgos confirmados graves pueden bloquear.

La integración en `deploy` se configura con `security.predeploy`: `warn` por defecto, `block` para detener ante `BLOCKED`, u `off` para omitir el análisis. Por ejemplo:

```toml
[security]
predeploy = "warn"
```

La revisión busca patrones locales y ejecuta comprobaciones disponibles; depende del contenido y las herramientas presentes. `suspected` significa indicio, no hecho confirmado. `not_applicable` significa que el control no corresponde al proyecto según las señales detectadas, no una garantía universal. Este informe **no certifica** cumplimiento de RGPD, LOPDGDD, WCAG ni ENS, ni constituye asesoramiento jurídico o una auditoría exhaustiva. Revisa manualmente el informe y valida el producto y su contexto antes de desplegar.
