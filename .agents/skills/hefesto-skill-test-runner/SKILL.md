---
name: hefesto-skill-test-runner
description: Detecta el stack del proyecto, ejecuta el test más acotado posible y devuelve solo el resultado, el número de fallos y las líneas clave de cada stacktrace. Úsala para comprobar una tarea sin llenar el contexto con salida de tests.
---

# test-runner

**Entrada:** la tarea en curso (sus `files` y su `verify`) o un fichero de test concreto.
**Salida:** 3–15 líneas: `PASS|FAIL`, número de tests y fallos, y por cada fallo `fichero:línea — mensaje`.

## Detección del stack

| Señal | Comando acotado |
|---|---|
| `pubspec.yaml` | `flutter test <test/fichero_test.dart>` (o `dart test` si no hay Flutter) |
| `package.json` con `vitest` / `jest` | `npx vitest run <fichero>` / `npx jest <fichero>` |
| `pyproject.toml`, `pytest.ini` o `tests/` en Python | `python -m pytest -q <fichero>` (o `python -m unittest <módulo>`) |
| `Cargo.toml` | `cargo test <filtro>` |
| `go.mod` | `go test ./<paquete> -run <Test>` |

Si la tarea tiene `verify`, ese es el comando de referencia: ejecútalo tal cual.

## Procedimiento

1. Ejecuta el comando más acotado que cubra la tarea, nunca la suite completa salvo que lo pida el `verify`.
2. Guarda la salida completa en `.hefesto/logs/tests/<tarea>-<hora>.log`.
3. Devuelve solo el resumen. De cada stacktrace, la primera línea del proyecto (no de librerías) y el mensaje de la aserción.
4. Si el pack `tokens` está activo, la salida que ves puede estar comprimida; la completa sigue en el log.

## Criterios de aceptación

- El resumen cabe en 15 líneas y nombra cada test fallido.
- El log completo existe y contiene la salida sin recortar.
- No modifica ficheros del proyecto.
