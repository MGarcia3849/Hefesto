# Integración de Hefesto con Telegram

Telegram es un canal humano para consultar Hefesto y solicitar operaciones con alcance visible. No sustituye la CLI ni el estado de Hefesto: los cambios siguen el ciclo, las verificaciones y las barreras habituales.

## Preparar el bot y proteger el acceso

1. En Telegram, inicia una conversación con `@BotFather`, crea un bot y guarda el token que entrega. Obtén también tu ID numérico de usuario mediante un bot de consulta de IDs fiable. En grupos necesitarás el ID numérico del chat.
2. Guarda el token fuera del repositorio. En PowerShell ejecuta `setx HEFESTO_TELEGRAM_TOKEN "<token>"` y abre una terminal nueva; alternativamente, usa un gestor de secretos que lo exponga al proceso. No pegues el token en archivos versionados, chats, capturas ni registros. `docs/telegram.env.example` muestra el nombre de la variable sin incluir credenciales.
3. Crea `~/hefesto/telegram.toml` (en Windows, bajo tu directorio de usuario, por ejemplo `C:\Users\<usuario>\hefesto\telegram.toml`):

   ```toml
   allowed_users = [123456789]
   allowed_chats = [-1001234567890]
   rate_per_minute = 20
   ```

   `allowed_users` es obligatorio y contiene IDs numéricos de personas autorizadas. En privado solo se exige ese ID; en grupos y supergrupos también se exige que el chat esté en `allowed_chats`. El límite por defecto es 20 mensajes por usuario en una ventana de 60 segundos; `rate_per_minute` debe ser un entero positivo. No añadas aquí el token.

   La integración puede buscar repositorios de GitHub bajo el propietario configurado en los ajustes de Hefesto (`github_owner`); no es una clave de `telegram.toml`. Configúralo desde la terminal con `hefesto projects owner TU_USUARIO` (sin argumento muestra el actual). Para listar repositorios hacen falta además `gh` autenticado y ese propietario configurado. Si no está disponible, registra el proyecto localmente.

4. Registra proyectos y, si vas a clonar repositorios nuevos, configura su carpeta destino. Ejecuta desde un proyecto reconocido por Hefesto:

   ```powershell
   hefesto projects add manjair C:\proyectos\manjair
   hefesto projects list
   hefesto projects remove manjair
   hefesto projects root C:\proyectos
   ```

   `add` acepta `--name` y `--read-only`; sin `--read-only` se registra con capacidades de lectura y escritura. `root` establece la raíz donde Hefesto preparará clones. `remove` quita el registro, no borra la carpeta del proyecto. La integración de Telegram no configura los proyectos: estos comandos son de la CLI normal.

5. Define un PIN en una terminal interactiva con `hefesto telegram pin`. Se solicita dos veces y debe tener al menos cuatro dígitos. Guárdalo como credencial. `/push` y `/deploy` requieren ese PIN además de aprobar la operación en Telegram.

## Grupos y temas

Autoriza el ID del grupo en `allowed_chats` y crea un tema por proyecto. En cada tema, envía `/bind ID` para vincular el proyecto registrado; usa `/unbind` para desvincularlo. Reserva General para órdenes globales y consultas. En grupos, Hefesto no acepta órdenes de proyecto fuera de un tema vinculado. En privado puedes usar `/project ID` para seleccionar el proyecto.

## Comandos

Los comandos disponibles son:

| Comando | Función |
|---|---|
| `/start`, `/help` | Muestra ayuda. |
| `/status` | Consulta estado global o del proyecto activo. |
| `/projects` | Lista proyectos registrados. |
| `/project ID` | Selecciona proyecto en privado o General. |
| `/bind ID`, `/unbind` | Vincula o desvincula el tema actual de un proyecto. |
| `/open [studio|vscode]` | Abre el proyecto activo; sin opción abre VS Code. |
| `/close` | Desactiva la asociación de proyecto del chat o tema. |
| `/plan TEXTO` | Solicita un plan para aprobación. |
| `/run` | Solicita ejecutar una iteración del loop para aprobación. |
| `/trust` | Solicita aprobar los `verify` pendientes para aprobación. |
| `/finish` | Solicita integrar la fase para aprobación. |
| `/push` | Solicita publicar la rama y abrir un PR para aprobación con PIN. |
| `/deploy` | Solicita desplegar para aprobación con PIN. |
| `/approve ID [PIN]` | Aprueba y ejecuta la operación pendiente del mismo usuario, chat y tema. Para push/deploy incluye el PIN. |
| `/reject ID` | Rechaza una operación pendiente. |
| `/diff` | Consulta el resumen de cambios del proyecto activo. |
| `/tasks` | Consulta tareas y estado del ROADMAP activo. |
| `/stop` | Solicita parar el loop activo antes de su siguiente iteración. |

El texto natural reconoce peticiones de proyecto y nombres aproximados sin convertir mensajes en comandos de shell. Ejemplos admitidos:

- `abre el proyecto manjair en android studio y haz estos cambios`
- `abre el proyecto solgrif solution en vsc y arréglalo`

Al detectar una petición de trabajo, Hefesto muestra una operación pendiente con su alcance y el comando `/approve`. Un proyecto ambiguo o desconocido requiere que lo elijas; no se aplica silenciosamente al proyecto activo. También se puede solicitar clonado desde un mensaje con una URL de GitHub; el clonado queda pendiente de aprobación.

## Qué se ejecuta y qué requiere aprobación

Las consultas (`/start`, `/help`, `/status`, `/projects`, `/diff`, `/tasks`) y la selección o apertura de proyecto (`/project`, `/bind`, `/unbind`, `/open`, `/close`) se atienden directamente. Crear una operación no ejecuta el cambio: `/plan`, `/run`, `/trust`, `/finish`, `/push`, `/deploy`, el trabajo pedido en lenguaje natural y el clonado generan una propuesta pendiente. Para ejecutarla hay que responder `/approve ID`; push y deploy requieren `/approve ID PIN`. `/reject ID` la descarta.

Las operaciones caducan a los **300 segundos**, están ligadas al usuario, chat, tema y alcance que se mostraron, y solo se pueden aprobar y ejecutar una vez. Si caducan, recházalas o solicita una nueva operación. La aprobación no elimina las verificaciones ni requisitos de Hefesto.

## Arranque, parada y recuperación en Windows

Abre Windows Terminal nueva para que cargue la variable definida con `setx`, ve a una carpeta de proyecto Hefesto y ejecuta:

```powershell
hefesto telegram check
hefesto telegram run
```

`check` valida token y allowlist. `run` mantiene el long polling en primer plano; deja esa ventana abierta. Para parar, pulsa `Ctrl+C` o ejecuta `hefesto telegram stop` desde otra terminal. La parada por comando crea una solicitud cooperativa; el gateway la observa y sale.

Tras un cierre inesperado, vuelve a iniciar `hefesto telegram run`. Al arrancar, Hefesto detecta operaciones que quedaron en estado `running`, las marca fallidas y lo registra; no las vuelve a ejecutar automáticamente. Solicita otra operación si aún la necesitas. Los errores de comunicación se reintentan con espera exponencial hasta cinco fallos consecutivos por defecto; tras el máximo, el proceso termina y puedes revisar el mensaje de error y arrancarlo de nuevo. Los registros redactan el token.

## Arranque automático al iniciar sesión (Windows)

Desde una terminal humana, dentro de un proyecto Hefesto, ejecuta:

```powershell
hefesto telegram check
hefesto telegram autostart enable
hefesto telegram autostart start
hefesto telegram autostart status
```

`enable` exige una terminal interactiva sin agente y vuelve a comprobar token y allowlist antes de registrar
la tarea **Hefesto Telegram** en el Programador de tareas del usuario actual. No requiere administrador.
`start` solicita arrancarla ahora; sin él se iniciará en el siguiente inicio de sesión. Para evitar dos gateways
haciendo polling del mismo bot, detén antes cualquier `hefesto telegram run` que tengas abierto en una terminal.

La tarea se ejecuta oculta en tu sesión interactiva, con permisos normales, sin límite de tiempo, también con
batería y con una sola instancia de la tarea. Al fallar se reinicia tras un minuto, hasta 999 veces. Puedes cerrar
VS Code y la terminal: el gateway sigue atendiendo Telegram y puede abrir los editores instalados en ese
escritorio. El ordenador debe estar encendido, conectado y con la sesión iniciada; no atiende mientras está
suspendido ni antes de iniciar sesión. Cerrar sesión termina los procesos de esa sesión.

Se importa un XML UTF-16 mediante `schtasks /Create /XML`, sin shell. El lanzador usa el comando `hefesto`
del PATH (o `bin/hefesto.cmd` del framework como alternativa) y conserva como directorio de trabajo el proyecto
desde el que activaste la tarea. Mantén disponibles ese proyecto, la instalación de Hefesto y su Python;
si los mueves, vuelve a ejecutar `enable`. Windows Script Host debe estar permitido por tu equipo.
Las rutas con comillas, expansión de variables o caracteres de control se rechazan; el comando `hefesto`
tampoco puede contener metacaracteres de cmd (`&`, `|`, `<`, `>`, `^`, paréntesis).

El token continúa llegando exclusivamente de `HEFESTO_TELEGRAM_TOKEN` en el entorno del usuario; no se copia
al XML ni a los lanzadores. Usa la variable persistente del usuario descrita en la configuración inicial;
tras cambiarla, cierra e inicia sesión para que Windows actualice el entorno de las tareas. La salida del gateway
se redacta **antes** de escribir en `util.state_home()/telegram-gateway.log` (por defecto
`%USERPROFILE%\.hefesto\telegram-gateway.log`; `HEFESTO_STATE_DIR` cambia ese directorio).
Allí también se guardan `telegram-autostart.xml`, `telegram-autostart.vbs` y `telegram-autostart.py`, sin token.
`status` muestra la respuesta de Windows con el estado y la última ejecución, junto a las últimas 20 líneas
redactadas del log. Si la consulta falla lo indica: no interpreta todos los errores como tarea ausente.

Para quitar el arranque automático y solicitar la parada del gateway:

```powershell
hefesto telegram autostart disable
hefesto telegram stop
```

`disable` elimina la tarea; una instancia ya activa puede continuar hasta recibir `stop`. Los lanzadores y el
log se conservan para diagnóstico. La parada cooperativa normal no provoca reinicio por fallo; puedes usar
`autostart start` para volver a arrancar mientras la tarea siga registrada. En Linux y otros sistemas,
`autostart` informa que aún no está implementado y devuelve un código distinto de cero; el arranque manual
con `hefesto telegram run` sigue disponible.

## Límites, estado y decisiones

- **Completo:** allowlist numérica de usuarios y chats, límite de frecuencia, long polling, consultas, selección de proyecto, enlace de temas, apertura de editores, operaciones aprobables con alcance, caducidad y un solo uso, PIN para push/deploy y recuperación de operaciones interrumpidas.
- **Pendiente:** las notas de voz no están disponibles porque aún no hay transcripción integrada; envía texto. No hay ejecución de Android Studio ni VS Code en un teléfono o en la nube: `/open` necesita el escritorio Windows donde corre Hefesto y el editor instalado. Telegram no expone una consola remota ni reemplaza la CLI.
- **Codex:** ChatGPT Plus se usa solo mediante la CLI oficial de Codex con sesión iniciada (`codex login`); Telegram no accede directamente a la cuenta ni a la API de pago.
- **Hermes Agent:** Desde 2026-11-19 se integra como motor subordinado de Hefesto para tareas generales. no se instala Hermes como segundo bot ni como segundo orquestador; Telegram conserva el bot de Hefesto y las operaciones siguen el flujo de aprobación de Hefesto. Consulta [la guía de Hermes](HERMES.md).
- **Decisiones del usuario (2026-11-19):** un tema por proyecto y General para órdenes globales; permitir aprobar `trust`, `finish`, `push` y `deploy` desde Telegram (push/deploy protegidos además por PIN); las consultas y la apertura se ejecutan directamente.

La decisión de arquitectura completa está en [ADR sobre Hermes Agent](../.hefesto/DECISIONS.md).
