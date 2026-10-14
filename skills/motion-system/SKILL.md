---
name: hefesto-skill-motion-system
description: Sistema de animación con resortes y duraciones tokenizadas, hover sutil, coreografía de entrada y salida, y respeto a prefers-reduced-motion. Incluye ejemplos web (CSS) y Flutter. Úsala cuando una tarea añada o revise animaciones.
---

# motion-system

**Entrada:** una interacción o transición a animar.
**Salida:** animaciones que usan solo los tokens de movimiento y que tienen alternativa reducida.

## Tokens

| Token | Duración | Uso |
|---|---|---|
| `motion-fast` | 120 ms | hover, pulsación, cambios de color |
| `motion-base` | 200 ms | aparición de elementos pequeños, toggles |
| `motion-slow` | 320 ms | paneles, diálogos, cambios de página |

Curvas: `ease-out` para entrar, `ease-in` para salir. Resorte suave (amortiguación ~0.8) para arrastres y elementos que siguen al dedo.

## Reglas

- Anima `transform` y `opacity`, nunca `width`, `height` ni `top`.
- Hover sutil: como mucho `translateY(-1px)` o un cambio de sombra; nunca escalados grandes.
- Coreografía: escalona las entradas de las listas 30–50 ms por elemento, con un máximo de 6 escalones. Las salidas van más rápidas que las entradas.
- `prefers-reduced-motion: reduce` → sin desplazamientos; solo fundidos de opacidad de ≤ 120 ms.

## Web

```css
:root { --motion-fast: 120ms; --motion-base: 200ms; --ease-out: cubic-bezier(.2,.8,.2,1); }
.card { transition: transform var(--motion-fast) var(--ease-out), box-shadow var(--motion-fast) var(--ease-out); }
.card:hover { transform: translateY(-1px); }
@media (prefers-reduced-motion: reduce) { .card { transition: opacity 120ms linear; } .card:hover { transform: none; } }
```

## Flutter

```dart
class Motion { static const fast = Duration(milliseconds: 120); static const base = Duration(milliseconds: 200); }
final reduce = MediaQuery.of(context).disableAnimations;
AnimatedOpacity(duration: reduce ? Duration.zero : Motion.base, curve: Curves.easeOut, opacity: visible ? 1 : 0, child: child);
```

## Criterios de aceptación

- Ninguna duración ni curva fuera de los tokens.
- Con movimiento reducido no hay desplazamientos.
- Ninguna animación bloquea la interacción más de `motion-slow`.
