---
name: hefesto-skill-ui-anatomy
description: Diagramas isométricos de anatomía de interfaces en SVG autocontenido, con capas, etiquetas con guías e interactividad mínima en CSS/JS inline. Úsala solo cuando una tarea pida explícitamente explicar la estructura de una UI.
---

# ui-anatomy

**Entrada:** una pantalla o componente y las capas a explicar (p. ej. fondo, contenido, navegación, overlays).
**Salida:** un único fichero `.svg` (o `.html` con el SVG inline) sin dependencias externas.

## Construcción

1. **Proyección isométrica:** cada capa es un plano transformado con `matrix(0.866, 0.5, -0.866, 0.5, x, y)`. Las capas se separan en vertical 40–60 px, de abajo (fondo) arriba (overlays).
2. **Capas:** un `<g id="capa-…">` por capa, con relleno de baja opacidad y borde de 1 px. Usa los colores del proyecto si existen tokens; si no, un neutro y un acento.
3. **Etiquetas:** texto fuera de las capas, unido con una guía (`<line>` o `<path>` de 1 px con un punto en el extremo). Las etiquetas nunca se solapan entre sí ni con las capas.
4. **Interactividad mínima:** al pasar el ratón por una etiqueta se resalta su capa (CSS `:hover` o unas pocas líneas de JS inline). Sin librerías.
5. **Accesibilidad:** `<title>` y `<desc>` en el SVG; `role="img"`; contraste AA en el texto.

## Criterios de aceptación

- Se abre directamente en un navegador sin red.
- Cada capa tiene etiqueta, y ninguna etiqueta se solapa.
- Pesa menos de 100 KB.

Si el pack `anatomy` está activo, esta skill es solo el overlay: carga la skill externa y aplica estas reglas encima.
