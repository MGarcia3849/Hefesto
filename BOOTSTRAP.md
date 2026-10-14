# Arranque de Hefesto en cualquier proyecto (BOOTSTRAP)

Este documento contiene las instrucciones exactas para un agente (Claude Code, Codex CLI o Gemini CLI/agy) que acaba de recibir la indicación:
*«Instala Hefesto desde <URL> y úsalo en este proyecto»*.

Sigue rigurosamente estos pasos en orden antes de escribir código.

---

## 1. Única autorización del usuario

Pregunta al usuario, en **UNA sola interacción** al comienzo, la confirmación de estos cuatro puntos:
1. **URL del repositorio:** la URL de Hefesto (por defecto `https://github.com/MGarcia3849/Hefesto`).
2. **Versión / Tag:** la versión a instalar (por defecto la última release etiquetada, por ejemplo `v2.1.0`; **nunca `main` sin fijar**).
3. **Proveedores externos:** si autoriza enviar código a Gemini y Codex (por defecto `gemini,codex`).
4. **PATH del sistema:** autorización para añadir `~/.local/bin` al PATH del usuario si `hefesto` no se encuentra tras la instalación.

> [!IMPORTANT]
> Esta es la **única autorización** de todo el proceso de instalación y arranque. Si el repositorio es privado, usa la sesión activa de `gh` (GitHub CLI) o de `git` del usuario en el sistema. Hefesto **nunca** almacena ni solicita tokens ni credenciales.

---

## 2. Instalación o actualización de Hefesto (`~/hefesto`)

Comprueba si el directorio `~/hefesto` existe en el equipo del usuario:

### Si `~/hefesto` NO existe:
Clona el tag seleccionado y ejecuta el instalador con `--add-to-path` (cubierto por la autorización inicial del punto 1):
```bash
git clone --branch <tag> <URL> ~/hefesto
bash ~/hefesto/install.sh --add-to-path
```
*(En Windows, ejecuta `install.sh` desde Git Bash. El instalador es idempotente y crea tanto `hefesto` como `hefesto.cmd` en `~/.local/bin` o en la ruta indicada por `$HEFESTO_BIN_DIR`).*

Con `--add-to-path`, el instalador añade automáticamente el directorio del lanzador al PATH del usuario (en Windows vía PowerShell sobre el PATH de usuario sin duplicar; en Linux, macOS y WSL añadiendo la línea a `~/.profile`). Este cambio queda cubierto por la autorización inicial del punto 1. Abre una terminal nueva para que surta efecto.

#### Alternativa manual para la sesión actual o si se instaló sin `--add-to-path`:
Si se necesita usar `hefesto` en la sesión actual sin abrir una terminal nueva, o si se ejecutó el instalador sin `--add-to-path`:
- **Git Bash (sesión actual):**
  ```bash
  export PATH="$HOME/.local/bin:$PATH"
  ```
- **Persistente en Windows (PowerShell manual como alternativa), sin duplicar la entrada:**
  ```powershell
  $d="$HOME\.local\bin"; $p=[Environment]::GetEnvironmentVariable("Path","User"); if (($p -split ";") -notcontains $d) { [Environment]::SetEnvironmentVariable("Path", $p.TrimEnd(";") + ";" + $d, "User") }
  ```
- **Linux, macOS y WSL:** añade `~/.local/bin` al perfil de la shell (`~/.profile`, `~/.bashrc` o `~/.zshrc`) si no está.

### Si `~/hefesto` YA existe:

Si `~/hefesto` tiene cambios locales sin commitear (`git -C ~/hefesto status --porcelain` no está vacío), **no actualices** y avisa al usuario.

Para un agente sin terminal interactiva (cubierto por la única autorización inicial), actualiza con:
```bash
git -C ~/hefesto fetch --tags
git -C ~/hefesto checkout <tag>
bash ~/hefesto/install.sh --add-to-path
```
Y ejecuta `hefesto migrate` y `hefesto sync` en el proyecto si hace falta.

`hefesto self-update` queda como opción para el usuario en su propia terminal interactiva (muestra el changelog y pide confirmación).

---

## 3. Diagnóstico del entorno (`hefesto doctor`)

Ejecuta el diagnóstico de Hefesto:
```bash
hefesto doctor
```
Verifica que las dependencias mínimas (Python ≥ 3.11, git con worktree, Bash) y las CLIs estén listas. Resuelve lo que falte antes de avanzar.

---

## 4. Arranque en el proyecto (`hefesto bootstrap`)

En la raíz del proyecto donde se desea trabajar, ejecuta:
```bash
hefesto bootstrap --yes --providers <lista>
```
*(Por ejemplo: `hefesto bootstrap --yes --providers gemini,codex`).*

El comando `hefesto bootstrap` analiza de forma determinista el directorio y decide automáticamente la acción adecuada:
* **Crear** (directorio vacío o que solo contiene documentación básica como README o LICENSE): ejecuta `init` (en modo local) y guía al usuario para planificar con `/hefesto-plan` a partir de sus requisitos.
* **Continuar** (ya existe `.hefesto/` en el proyecto): ejecuta `hefesto resume` para retomar el trabajo exactamente en la siguiente acción pendiente (`next_action`), sin repetir delegaciones previas.
* **Mejorar** (existe código pero no `.hefesto/`): ejecuta `hefesto adopt`, analizando el stack técnico (tests, lint, build) y generando un ROADMAP de mejoras en `.hefesto/ROADMAP.md` y un registro en `DECISIONS.md`, **sin modificar ni un solo fichero de código existente**.

### Modo local por defecto (sin ensuciar el proyecto)
Por defecto, la instalación se realiza en **modo local** (`install_mode: local`):
- Todo vive dentro de `.hefesto/` (estado, ROADMAP, DECISIONS, LEARNINGS, evidencias y logs).
- No crea `AGENTS.md` ni `CLAUDE.md` en la raíz ni altera `.gitignore`.
- Registra las rutas generadas en `.git/info/exclude`.
- Como resultado, `git status` permanece completamente limpio y nada de Hefesto se sube al repositorio remoto de GitHub.
*(Si en un equipo se desea versionar el ROADMAP y las evidencias en Git, se puede usar `hefesto init --shared`).*

---

## 5. Operaciones con confirmación y verificaciones automáticas

Para trabajar de forma autónoma sin fricción pero con seguridad garantizada según el código:

### Verificaciones automáticas (`auto_safe`)
Los comandos de `verify` estándar (tests, lint, build como `pytest`, `python -m unittest`, `npm test`, `flutter test`, `cargo test`, `go test`, `ruff`, `mypy`, `test -f`, `grep -q`) se aprueban solos: están clasificados como seguros por `trust.is_safe_verify` y `hefesto done` los aprueba automáticamente sin requerir intervención del usuario.

### Confirmaciones y permisos
- **`hefesto bootstrap --yes`**: ejecuta `init` o `adopt` sin preguntar, porque es la autorización inicial.
- **Fuera de bootstrap en Claude Code**: `hefesto init`, `hefesto adopt`, `hefesto local`, `hefesto providers add/remove/reset` y cualquier comando con `-C`/`--project` piden confirmación mediante la regla de permisos `ask`.
- **`hefesto trust`, `hefesto unblock` y `hefesto finish`**: piden terminal salvo con `--yes`, que funciona sin terminal, se rechaza en procesos headless (`HEFESTO_ROLE`) y con `-C`, y en Claude Code pasa por la regla `ask` (un clic del usuario).
- **Control de Git**: nunca hay `git push` automático; la publicación o sincronización remota queda siempre bajo control humano explícito.

---

## 6. Aprobación previa antes de tocar código

Una vez ejecutado `hefesto bootstrap`:
1. Muestra al usuario el plan generado (en modo crear o mejorar) o el estado recuperado (en modo continuar).
2. **Espera la aprobación explícita del usuario** antes de iniciar tareas (`hefesto start`) o modificar cualquier archivo del código.
