# Hermes Agent integrado con Hefesto

Hefesto puede usar Hermes Agent como motor subordinado para tareas generales. Hefesto conserva el estado, las aprobaciones y la verificación; Telegram sigue teniendo un solo bot y Hefesto un solo orquestador.

## Instalación en Windows

Instala Hermes Agent con el instalador oficial de PowerShell y el canal estable:

```powershell
irm https://raw.githubusercontent.com/NousResearch/hermes-agent/main/scripts/install.ps1 | iex
```

En esta máquina se instaló Hermes Agent **v0.23.2** con ese instalador (`powershell`, canal `stable`). El instalador coloca el ejecutable en `%LOCALAPPDATA%\hermes\bin\hermes.exe` y añade `%LOCALAPPDATA%\hermes\bin` al `PATH`. Abre una terminal nueva para que Windows cargue el PATH actualizado y comprueba `hermes --version`.

## Inicio de sesión y modelo

Para iniciar sesión con la suscripción de ChatGPT, ejecuta:

```powershell
hermes auth add openai-codex
```

Completa el inicio de sesión mediante el código de dispositivo que indica Hermes. Después, revisa y elige un modelo disponible:

```powershell
hermes model
```

Según la documentación de Hermes, el acceso a Claude por suscripción solo funciona con **Claude Max**. Sin Max, Claude requiere una API de pago; Hefesto no activa ni proporciona esa API.

## Perfiles subordinados de Hefesto

Desde un proyecto reconocido por Hefesto, ejecuta:

```powershell
hefesto hermes setup
hefesto hermes status
```

`setup` crea los perfiles `hefesto-lectura` y `hefesto-accion` mediante `hermes profile create --clone --no-alias`. Ambos clonan la configuración de `default` y comparten sus inicios de sesión. Hefesto fija `approvals.single_query_mode` y `approvals.deny` por perfil con `hermes -p PERFIL config set`. `status` muestra si Hermes y los perfiles están disponibles. Para enviar una consulta al motor subordinado:

```powershell
hefesto hermes ask "Resume los documentos recientes de este proyecto"
```

Los documentos generados se guardan en **Hefesto Oficina**, la carpeta de trabajo de salida gestionada por Hefesto.

## Telegram: acceso directo y aprobación

Las consultas de lectura y las acciones locales de bajo impacto pueden ejecutarse directamente: redactar un documento de Word en Oficina, leer y resumir correo o buscar información y archivos. Las acciones con efectos externos o destructivos crean una operación pendiente que requiere `/approve ID`: enviar correo, borrar archivos, mover elementos fuera de Hefesto Oficina, instalar software o ejecutar comandos. La operación muestra su alcance antes de pedir aprobación. Las acciones sensibles, como publicar o desplegar cuando corresponda, también pueden pedir el PIN de Hefesto.

Para abrir un documento de Oficina, responde con `/open_file NOMBRE`. El bot de Telegram que recibe estas órdenes es el de Hefesto. No se activa el gateway de Telegram de Hermes: habría dos bots compitiendo por el canal y se perdería el único flujo de aprobación de Hefesto.

## Configuración del correo

Configura el correo dentro de Hermes, no en Hefesto. Para Gmail, Outlook o Microsoft 365 puedes usar la skill `himalaya` por IMAP/SMTP; algunos proveedores exigen una contraseña de aplicación. También puedes configurar la skill `google-workspace` cuando encaje con tu cuenta. Sigue el flujo de autenticación que documente cada skill y almacena sus credenciales en la configuración segura de Hermes. Hefesto no guarda contraseñas ni las copia a sus archivos.

## Uso del ordenador

El control de escritorio de Hermes requiere una instalación interactiva adicional. En una terminal interactiva ejecuta:

```powershell
hermes computer-use install --upgrade
```

El instalador solicita elevación UAC. No se ejecuta en segundo plano desde Hefesto.

## Límites

Los perfiles de lectura y acción reducen el riesgo al configurar approvals distintas, pero no aíslan Hermes del sistema ni sustituyen los permisos de Windows. Hermes puede equivocarse, interpretar mal una petición o producir un documento incorrecto; revisa los resultados y el alcance de cada aprobación. La suscripción y las capacidades disponibles dependen de la cuenta y del proveedor. La integración no convierte las consultas en verificación determinista ni elimina los límites y confirmaciones propios de Hefesto.
