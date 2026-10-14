---
name: hefesto-skill-frontend-taste
description: Criterio visual para interfaces web y Flutter, con tokens, escala de espaciado fija, pareja tipográfica sans + mono, paleta contenida, jerarquía clara y estados vacío/carga/error. Úsala en tareas del rol Visual y al revisar UI.
---

# frontend-taste

**Entrada:** una pantalla o componente a crear o revisar.
**Salida:** código que usa solo tokens, y una checklist de revisión marcada.

## Sistema

- **Tokens:** colores, espaciados, radios, sombras, duraciones y tipografías se definen una vez (CSS custom properties, `ThemeData`/`ThemeExtension` en Flutter). Prohibidos los valores sueltos en componentes.
- **Espaciado:** escala fija 4 · 8 · 12 · 16 · 24 · 32 · 48 · 64. Nada fuera de la escala.
- **Tipografía:** una sans para interfaz y una mono para datos y código. Escala de 5–6 tamaños como máximo; el peso marca la jerarquía antes que el tamaño.
- **Color:** un neutro con 8–10 pasos, un acento y los semánticos (éxito, aviso, error). Modo oscuro con sus propios valores, no con el color invertido.
- **Jerarquía:** una acción primaria por vista; las secundarias, visualmente subordinadas.

## Estados obligatorios

Cada vista con datos tiene **vacío** (qué es y qué hacer), **carga** (skeleton con la forma final, no un spinner a pantalla completa) y **error** (qué pasó y cómo reintentar).

## Accesibilidad

- Contraste AA: 4.5:1 en texto y 3:1 en componentes y texto grande.
- Foco visible y distinto del hover; orden de tabulación lógico.
- Objetivos táctiles ≥ 44×44 px (48 dp en Flutter). Etiquetas accesibles en iconos sin texto.

## Criterios de aceptación (checklist de revisión)

- [ ] Sin valores fuera de los tokens.
- [ ] Estados vacío, carga y error implementados.
- [ ] Contraste AA y foco visible comprobados.
- [ ] Funciona a 360 px de ancho sin scroll horizontal.
- [ ] Modo oscuro revisado.
