"""CLI `hefesto`: único contrato entre los orquestadores y el estado del proyecto."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from hefesto import __version__, config, doctor, project
from hefesto.util import EXIT_BLOCKED, EXIT_INPUT, EXIT_OK, HefestoError


def _project(args: argparse.Namespace) -> Path:
    return project.find_project(Path(args.project) if getattr(args, "project", None) else None)


# ---------------------------------------------------------------------------
# Fase 1: init, doctor, config


def cmd_init(args: argparse.Namespace) -> int:
    target = Path(args.dir or ".")
    for line in project.init(target, copy=args.copy, providers=args.providers):
        print(line)
    print("\n== doctor ==")
    checks = doctor.run_checks(target.resolve())
    print(doctor.format_checks(checks))
    return EXIT_OK


def cmd_doctor(args: argparse.Namespace) -> int:
    try:
        proj: Path | None = _project(args)
    except HefestoError:
        proj = None
    checks = doctor.run_checks(proj)
    print(doctor.format_checks(checks))
    return doctor.exit_code(checks)


def cmd_config_validate(args: argparse.Namespace) -> int:
    try:
        proj: Path | None = _project(args)
    except HefestoError:
        proj = None
    cfg = config.load(proj, validate_layers=False)
    errors = config.validate(cfg)
    if errors:
        print("Configuración inválida:")
        for err in errors:
            print(f"  - {err}")
        return EXIT_INPUT
    print("Configuración válida. Capas: " + ", ".join(f"{layer.name} ({layer.path})" for layer in cfg.layers))
    return EXIT_OK


def cmd_config_show(args: argparse.Namespace) -> int:
    try:
        proj: Path | None = _project(args)
    except HefestoError:
        proj = None
    cfg = config.load(proj)
    if args.json:
        print(json.dumps(cfg.data, indent=2, ensure_ascii=False))
        return EXIT_OK
    for keys, value in config.flatten(cfg.data):
        print(f"{'.'.join(keys)} = {json.dumps(value, ensure_ascii=False)}    # {cfg.origin(*keys)}")
    return EXIT_OK


def cmd_status(args: argparse.Namespace) -> int:
    from hefesto import status

    print(status.render(_project(args)))
    return EXIT_OK


def cmd_hook(args: argparse.Namespace) -> int:
    from hefesto import guard

    raw = sys.stdin.read()
    try:
        payload = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError:
        print("hefesto hook: entrada JSON inválida; se bloquea por seguridad.", file=sys.stderr)
        return 2
    try:
        proj: Path | None = project.find_project(Path(payload.get("cwd") or ".")) if isinstance(payload, dict) else None
    except HefestoError:
        proj = None
    protected = config.load(proj).get("guard", "protected_paths", default=list(guard.DEFAULT_PROTECTED))
    reason = guard.check_tool_use(payload if isinstance(payload, dict) else {}, protected)
    if reason:
        print(reason, file=sys.stderr)
        return 2  # Claude Code: código 2 = bloquear la herramienta y mostrar el motivo.
    return EXIT_OK


# ---------------------------------------------------------------------------
# Fase 2: ciclo de tareas


def cmd_next(args: argparse.Namespace) -> int:
    from hefesto import workflow

    task, reason = workflow.do_next(_project(args))
    if task is None:
        print(f"Sin tarea disponible: {reason}.")
        return EXIT_OK
    prefix = "En curso" if reason == "en curso" else "Siguiente"
    print(f"{prefix}: {task.id} — {task.title}\n  role: {task.role}  size: {task.size}\n  files: "
          f"{', '.join(task.files)}\n  verify: {task.verify}")
    return EXIT_OK


def cmd_start(args: argparse.Namespace) -> int:
    from hefesto import workflow

    data = workflow.start(_project(args), args.task)
    print(f"{args.task} en curso (run {data['run_id']}, rol {data['active_role']}).")
    if data.get("branch"):
        print(f"  rama: {data['branch']}  aislamiento: {data.get('isolation')}  directorio: {data.get('workdir')}")
    print(f"  siguiente: {data['next_action']}")
    return EXIT_OK


def cmd_fail(args: argparse.Namespace) -> int:
    from hefesto import workflow

    code, message = workflow.fail(_project(args), args.task, args.reason)
    print(message if code else f"Intento registrado. Rol del siguiente intento: {message}")
    return code


def cmd_unblock(args: argparse.Namespace) -> int:
    from hefesto import workflow

    print(workflow.unblock(_project(args), args.task, args.note))
    return EXIT_OK


def cmd_trust(args: argparse.Namespace) -> int:
    from hefesto import roadmap, trust

    proj = _project(args)
    _count, messages = trust.approve_interactive(proj, roadmap.load_valid(proj))
    print("\n".join(messages))
    return EXIT_OK


# ---------------------------------------------------------------------------
# Fase 3: aceptación


def _print_result(code: int, message: str) -> int:
    print(message, file=sys.stdout if code == EXIT_OK else sys.stderr)
    return code


def cmd_review(args: argparse.Namespace) -> int:
    from hefesto import acceptance

    return _print_result(*acceptance.review(_project(args), args.task, args.verdict, args.notes or "", args.run_id,
                                            args.reviewer, args.author or []))


def cmd_done(args: argparse.Namespace) -> int:
    from hefesto import acceptance

    return _print_result(*acceptance.done(_project(args), args.task))


def cmd_finish(args: argparse.Namespace) -> int:
    from hefesto import acceptance

    return _print_result(*acceptance.finish(_project(args), args.phase, args.pr))


def cmd_evidence(args: argparse.Namespace) -> int:
    from hefesto import evidence, state

    proj = _project(args)
    if args.target == "verify":
        errors = evidence.verify_chain(proj, state.load(proj).get("evidence_head"))
        if errors:
            return _print_result(EXIT_BLOCKED, "Cadena de evidencias rota:\n  - " + "\n  - ".join(errors))
        count = len(evidence.load_all(proj))
        print(f"Cadena de evidencias íntegra ({count} evidencias).")
        return EXIT_OK
    print(json.dumps(evidence.show(proj, args.target), indent=2, ensure_ascii=False))
    return EXIT_OK


# ---------------------------------------------------------------------------
# Fase 4: adaptadores y enrutado


def cmd_run(args: argparse.Namespace) -> int:
    from hefesto import router

    outcome = router.run(_project(args), args.role, args.prompt, args.task, args.dry_run)
    if args.dry_run:
        for hop in outcome.hops:
            print(f"{hop.entry:<16} {hop.model_id:<22} {hop.outcome}")
        if outcome.code:
            print(f"Ningún proveedor disponible para `{args.role}`.")
        return outcome.code
    print(json.dumps(outcome.to_dict(), ensure_ascii=False, indent=2))
    return outcome.code


def cmd_packet(args: argparse.Namespace) -> int:
    from hefesto import packet, providers, roadmap, state

    proj = _project(args)
    cfg = config.load(proj)
    rm = roadmap.load_valid(proj)
    task = rm.get(args.task)
    role = args.role or task.role
    data = state.load(proj)
    workdir = Path(data["workdir"]) if data.get("current_task_id") == task.id and data.get("workdir") else proj
    entry = args.model or (cfg.role_chain(role) or ["claude"])[0]
    pkt = packet.build(proj, cfg, role, args.instruction or "(sin instrucción adicional)", task, rm, data,
                       providers.model_id(cfg, entry), workdir)
    path = packet.save(proj, pkt, task.id, role)
    print(pkt.text)
    print(f"\n[paquete: ~{pkt.tokens} tokens de {pkt.max_tokens}; guardado en {path.relative_to(proj).as_posix()}]",
          file=sys.stderr)
    return EXIT_OK


def cmd_route(args: argparse.Namespace) -> int:
    from hefesto import roadmap, router

    proj = _project(args)
    cfg = config.load(proj)
    task = roadmap.load_valid(proj).get(args.task)
    if task.size == "L":
        print(f"{task.id} es L: el Core debe dividirla en tareas M/S antes de ejecutar nada.")
        steps = [("core", "dividir la tarea")]
    elif task.size == "S":
        steps = [(task.role, "un solo intento de implementación"), ("reviewer", "revisión ligera")]
    else:
        steps = [(task.role, "implementación"), ("reviewer", "revisión"), ("forge", "si fallan dos intentos")]
    print(f"Ruta de {task.id} (size {task.size}) — sin llamadas a proveedores:")
    for role, why in steps:
        hops = router.plan_route(cfg, role)
        chosen = next((h for h in hops if h.outcome == "elegido"), None)
        skipped = [f"{h.entry} ({h.outcome})" for h in hops if h.outcome != "elegido"]
        print(f"  {role:<9} {why:<34} → {chosen.model_id if chosen else 'NINGUNO disponible'}"
              + (f"   saltados: {'; '.join(skipped)}" if skipped else ""))
    return EXIT_OK


def cmd_providers(args: argparse.Namespace) -> int:
    from hefesto import admin, quota

    proj = _project(args)
    if args.action == "status":
        print(admin.status_table(proj))
    elif args.action == "reset":
        if not args.name:
            raise HefestoError("Indica el proveedor: `hefesto providers reset NOMBRE`.", EXIT_INPUT)
        quota.reset(args.name)
        print(f"{args.name}: estado reiniciado (sin cooldown ni error de auth).")
    else:
        if not args.name:
            raise HefestoError(f"Indica el proveedor: `hefesto providers {args.action} NOMBRE`.", EXIT_INPUT)
        print(admin.add_or_remove(proj, args.action, args.name))
    return EXIT_OK


def cmd_local(args: argparse.Namespace) -> int:
    from hefesto import admin

    proj = _project(args)
    if args.action == "enable":
        if not args.base_url or not args.model:
            raise HefestoError("`local enable` requiere --base-url y --model.", EXIT_INPUT)
        print(admin.local_enable(proj, args.base_url, args.model, args.role, args.first, args.global_scope))
    else:
        print(admin.local_disable(proj, args.global_scope))
    return EXIT_OK


# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="hefesto", description="Orquestación multiagente determinista.")
    parser.add_argument("--version", action="version", version=f"hefesto {__version__}")
    parser.add_argument("-C", "--project", help="directorio del proyecto (por defecto, el actual)")
    sub = parser.add_subparsers(dest="command", metavar="COMANDO")

    p = sub.add_parser("init", help="inicializa Hefesto en un proyecto (idempotente)")
    p.add_argument("dir", nargs="?", help="directorio del proyecto")
    p.add_argument("--copy", action="store_true", help="copia las skills en vez de enlazarlas")
    p.add_argument("--providers", help="proveedores externos autorizados, p. ej. gemini,codex ('' = ninguno)")
    p.set_defaults(func=cmd_init)

    p = sub.add_parser("doctor", help="comprueba dependencias, adaptadores, allowlist, lock y estado")
    p.set_defaults(func=cmd_doctor)

    p = sub.add_parser("config", help="valida o muestra la configuración efectiva")
    csub = p.add_subparsers(dest="config_cmd", metavar="ACCIÓN", required=True)
    c = csub.add_parser("validate", help="valida las capas de configuración")
    c.set_defaults(func=cmd_config_validate)
    c = csub.add_parser("show", help="configuración efectiva y la capa de origen de cada valor")
    c.add_argument("--json", action="store_true")
    c.set_defaults(func=cmd_config_show)

    p = sub.add_parser("status", help="estado, tarea, intentos, rama, rol activo y siguiente acción")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("hook", help="puente para los hooks de Claude Code (lee JSON por stdin)")
    p.add_argument("event", choices=["pre-tool-use"])
    p.set_defaults(func=cmd_hook)

    p = sub.add_parser("next", help="siguiente tarea pendiente respetando dependencias y bloqueos")
    p.set_defaults(func=cmd_next)

    p = sub.add_parser("start", help="inicia una tarea (con lock y aislamiento git)")
    p.add_argument("task")
    p.set_defaults(func=cmd_start)

    p = sub.add_parser("fail", help="registra un intento fallido y devuelve el rol del siguiente")
    p.add_argument("task")
    p.add_argument("reason")
    p.set_defaults(func=cmd_fail)

    p = sub.add_parser("unblock", help="desbloquea una tarea tras el circuit breaker (solo humanos, TTY)")
    p.add_argument("task")
    p.add_argument("note")
    p.set_defaults(func=cmd_unblock)

    p = sub.add_parser("trust", help="aprueba los comandos verify del ROADMAP (solo humanos, TTY)")
    p.set_defaults(func=cmd_trust)

    p = sub.add_parser("review", help="registra el veredicto del Reviewer (regla de independencia)")
    p.add_argument("task")
    p.add_argument("--verdict", choices=["pass", "fail"], required=True)
    p.add_argument("--notes", default="")
    p.add_argument("--run-id", help="run_id de `hefesto run reviewer`")
    p.add_argument("--reviewer", help="proveedor:modelo si la revisión no pasó por `hefesto run`")
    p.add_argument("--author", action="append", help="proveedor:modelo que escribió el cambio (repetible)")
    p.set_defaults(func=cmd_review)

    p = sub.add_parser("done", help="verify confiado y en verde + revisión pass → checkpoint, [x] y evidencia")
    p.add_argument("task")
    p.set_defaults(func=cmd_done)

    p = sub.add_parser("finish", help="integra la rama de la fase en la principal (rebase, tests, confirmación)")
    p.add_argument("phase", nargs="?", help="fase (por defecto, la primera abierta)")
    p.add_argument("--pr", action="store_true", help="sube la rama y abre un PR en borrador (github.pr = true)")
    p.set_defaults(func=cmd_finish)

    p = sub.add_parser("evidence", help="muestra la evidencia de una tarea o comprueba la cadena (`verify`)")
    p.add_argument("target", help="TASK-NNN o `verify`")
    p.set_defaults(func=cmd_evidence)

    p = sub.add_parser("run", help="delega en el proveedor que toque según rol, allowlist y cuota")
    p.add_argument("role", choices=list(config.ROLES))
    p.add_argument("prompt", help="instrucción del orquestador (viaja dentro del paquete de la tarea)")
    p.add_argument("--task", help="tarea (por defecto, la que está en curso)")
    p.add_argument("--dry-run", action="store_true", help="dice a qué proveedor iría, sin llamar a nada")
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("packet", help="genera y muestra el paquete de contexto de una tarea")
    p.add_argument("task")
    p.add_argument("--role", choices=list(config.ROLES))
    p.add_argument("--model", help="proveedor[:modelo] para el límite de tamaño (por defecto, el primero del rol)")
    p.add_argument("--instruction", help="instrucción del orquestador a incluir")
    p.set_defaults(func=cmd_packet)

    p = sub.add_parser("route", help="ruta, roles y proveedores previstos para una tarea, sin llamadas")
    p.add_argument("task")
    p.add_argument("--dry-run", action="store_true", required=True, help="obligatorio: route nunca llama a nada")
    p.set_defaults(func=cmd_route)

    p = sub.add_parser("providers", help="estado de proveedores, allowlist del proyecto y cooldowns")
    p.add_argument("action", choices=["status", "add", "remove", "reset"])
    p.add_argument("name", nargs="?", choices=list(config.PROVIDERS))
    p.set_defaults(func=cmd_providers)

    p = sub.add_parser("local", help="activa o desactiva el modelo local (API compatible con OpenAI)")
    p.add_argument("action", choices=["enable", "disable"])
    p.add_argument("--base-url")
    p.add_argument("--model")
    p.add_argument("--role", choices=list(config.ROLES), help="añade `local` a la cadena de este rol")
    p.add_argument("--first", action="store_true", help="al principio de la cadena (por defecto, al final)")
    p.add_argument("--global", dest="global_scope", action="store_true", help="en ~/hefesto/settings.json")
    p.set_defaults(func=cmd_local)

    return parser


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8")
            sys.stderr.reconfigure(encoding="utf-8")
        except (OSError, ValueError):
            pass
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return EXIT_OK
    try:
        return int(args.func(args) or 0)
    except HefestoError as exc:
        print(f"hefesto: {exc}", file=sys.stderr)
        return exc.code
    except KeyboardInterrupt:
        print("hefesto: interrumpido", file=sys.stderr)
        return EXIT_INPUT
