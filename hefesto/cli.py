"""CLI `hefesto`: único contrato entre los orquestadores y el estado del proyecto."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from hefesto import __version__, config, doctor, project, util
from hefesto.util import EXIT_BLOCKED, EXIT_INPUT, EXIT_OK, HefestoError


def _project(args: argparse.Namespace) -> Path:
    return project.find_project(Path(args.project) if getattr(args, "project", None) else None)


def _reject_project_option(args: argparse.Namespace, command: str) -> None:
    if getattr(args, "project", None) is not None:
        raise HefestoError(f"`hefesto {command}` no permite la opción global `-C`/`--project`.", EXIT_INPUT)


# ---------------------------------------------------------------------------
# Fase 1: init, doctor, config


def cmd_init(args: argparse.Namespace) -> int:
    from hefesto import distribution

    _reject_project_option(args, "init")
    target = Path(args.dir or ".")
    distribution.validate_safe_project_paths(target)
    for line in project.init(target, providers=args.providers, shared=getattr(args, "shared", False)):
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

    print(status.render(_project(args), protocol=getattr(args, "protocol", False)))
    return EXIT_OK


def cmd_hook(args: argparse.Namespace) -> int:
    try:
        from hefesto import guard

        raw = sys.stdin.read()
        try:
            payload = json.loads(raw) if raw.strip() else {}
        except json.JSONDecodeError:
            print("hefesto hook: entrada JSON inválida; se bloquea por seguridad.", file=sys.stderr)
            return 2
        try:
            proj: Path | None = (
                project.find_project(Path(payload.get("cwd") or "."))
                if isinstance(payload, dict)
                else None
            )
        except Exception:
            proj = None

        fail_closed = False
        try:
            cfg = config.load(proj)
            protected = cfg.get("guard", "protected_paths", default=list(guard.DEFAULT_PROTECTED))
        except Exception:
            protected = list(guard.DEFAULT_PROTECTED)
            fail_closed = True

        reason = guard.check_tool_use(
            payload if isinstance(payload, dict) else {},
            protected,
            project=proj,
            fail_closed=fail_closed,
        )
        if reason:
            print(reason, file=sys.stderr)
            return 2  # Claude Code: código 2 = bloquear la herramienta y mostrar el motivo.
        return EXIT_OK
    except Exception as exc:
        print(f"hefesto hook: error inesperado ({exc}); se bloquea por seguridad.", file=sys.stderr)
        return 2


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
    if code:
        print(message)
    elif "ya se contó" in message:
        print(message)
    else:
        print(f"Intento registrado. Rol del siguiente intento: {message}")
    return code


def _check_yes_barriers(args: argparse.Namespace) -> None:
    if "HEFESTO_ROLE" in os.environ:
        raise HefestoError("`--yes` no está permitido cuando `HEFESTO_ROLE` está definido.", EXIT_BLOCKED)
    if getattr(args, "project", None) is not None:
        raise HefestoError("`--yes` no está permitido con la opción global `-C`/`--project`.", EXIT_INPUT)


def cmd_unblock(args: argparse.Namespace) -> int:
    from hefesto import workflow

    if getattr(args, "yes", False):
        _check_yes_barriers(args)
    print(workflow.unblock(_project(args), args.task, args.note, yes=getattr(args, "yes", False)))
    return EXIT_OK


def cmd_trust(args: argparse.Namespace) -> int:
    from hefesto import roadmap, trust

    if getattr(args, "yes", False):
        _check_yes_barriers(args)
    proj = _project(args)
    _count, messages = trust.approve_interactive(proj, roadmap.load_valid(proj), yes=getattr(args, "yes", False))
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

    if getattr(args, "yes", False):
        _check_yes_barriers(args)
    return _print_result(*acceptance.finish(_project(args), args.phase, args.pr, yes=getattr(args, "yes", False)))


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


def cmd_clean(args: argparse.Namespace) -> int:
    from hefesto import maintenance, util

    if getattr(args, "yes", False):
        _check_yes_barriers(args)
    else:
        util.require_tty("hefesto clean")
    proj = _project(args)
    if not getattr(args, "yes", False):
        plan = maintenance.plan_clean(proj, include_unmerged=getattr(args, "include_unmerged", False))
        print(plan)
        if not util.confirm("¿Proceder con la limpieza?", default=False):
            return EXIT_OK
    report = maintenance.clean(proj, include_unmerged=getattr(args, "include_unmerged", False))
    print(report)
    return EXIT_OK


def cmd_discard(args: argparse.Namespace) -> int:
    from hefesto import maintenance, util

    if getattr(args, "yes", False):
        _check_yes_barriers(args)
    else:
        util.require_tty("hefesto discard")
    proj = _project(args)
    if not getattr(args, "yes", False):
        preview = maintenance.preview_discard(proj, args.target)
        print(preview)
        if not util.confirm(f"¿Descartar {args.target}?", default=False):
            return EXIT_OK
    msg = maintenance.discard(proj, args.target)
    print(msg)
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
    from hefesto import config, providers, roadmap, router, scheduler, skill_select, skills

    proj = _project(args)
    cfg = config.load(proj)
    task_id = getattr(args, "task", None)
    if task_id:
        task = roadmap.load_valid(proj).get(task_id)
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
        choices = skill_select.select(proj, cfg, task.role, task)
        names = ", ".join(skills.sanitize_text(c.name) for c in choices) if choices else "ninguna"
        print(f"skills del paquete: {names}")
        return EXIT_OK

    rm = roadmap.load_valid(proj)
    capacity = int(cfg.get("limits", "max_parallel_tasks", default=4))
    batch = scheduler.select_batch(rm, running=[], capacity=capacity)
    conc = scheduler.Concurrency.from_config(cfg)
    busy: dict[str, int] = {}
    print(f"Lote paralelo previsto (capacidad {capacity}):")
    for task in batch:
        entry = scheduler.pick_entry(cfg, task.role, busy, conc)
        if entry:
            mid = providers.model_id(cfg, entry)
            busy[mid] = busy.get(mid, 0) + 1
            print(f"  {task.id:<10} ({task.role}) → {entry} ({mid})")
        else:
            print(f"  {task.id:<10} ({task.role}) → NINGUNO disponible")
    return EXIT_OK


def cmd_providers(args: argparse.Namespace) -> int:
    from hefesto import admin, preflight, quota

    if args.action in ("add", "remove", "reset"):
        _reject_project_option(args, f"providers {args.action}")
    proj = _project(args)
    if args.action == "check":
        print(preflight.render(preflight.check_all(proj, args.probe)))
    elif args.action == "status":
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

    _reject_project_option(args, f"local {args.action}")
    proj = _project(args)
    if args.action == "enable":
        if not args.base_url or not args.model:
            raise HefestoError("`local enable` requiere --base-url y --model.", EXIT_INPUT)
        print(admin.local_enable(proj, args.base_url, args.model, args.role, args.first, args.global_scope))
    else:
        print(admin.local_disable(proj, args.global_scope))
    return EXIT_OK


# ---------------------------------------------------------------------------
# Fase 5: orquestadores


def cmd_sync(args: argparse.Namespace) -> int:
    from hefesto import sync

    changes = sync.run(_project(args), check=args.check)
    print(sync.summary(changes))
    pending = [c for c in changes if c.action != "unchanged"]
    if any(c.action == "conflict" for c in changes):
        return EXIT_BLOCKED
    return EXIT_INPUT if args.check and pending else EXIT_OK


def cmd_prompt(args: argparse.Namespace) -> int:
    from hefesto import sync

    print(sync.render_prompt(args.step))
    return EXIT_OK


# ---------------------------------------------------------------------------
# Fase 6: autonomía


def cmd_note(args: argparse.Namespace) -> int:
    from hefesto import progress

    record = progress.note(_project(args), args.text)
    print(f"Nota registrada ({record['orchestrator']}, tarea {record['task_id'] or '-'}).")
    return EXIT_OK


def cmd_resume(args: argparse.Namespace) -> int:
    from hefesto import progress

    info = progress.resume_data(_project(args))
    print(json.dumps(info, ensure_ascii=False, indent=2) if args.json else progress.render_resume(info))
    return EXIT_OK


def cmd_handoff(args: argparse.Namespace) -> int:
    from hefesto import progress

    print(progress.handoff(_project(args), args.to, args.reason, args.source))
    return EXIT_OK


def cmd_loop(args: argparse.Namespace) -> int:
    from hefesto import loop

    result = loop.run(_project(args), args.orchestrator, args.max_tasks, args.max_turns, args.wait)
    print(f"{result.message}\n(tareas completadas: {result.completed}, iteraciones: {result.iterations})")
    return result.code


def cmd_stop(args: argparse.Namespace) -> int:
    from hefesto import loop

    path = loop.request_stop(_project(args))
    print(f"Creado {path}. El loop se detendrá antes de la siguiente iteración; borrarlo es una acción humana.")
    return EXIT_OK


def cmd_parallel(args: argparse.Namespace) -> int:
    from hefesto import scheduler

    if "HEFESTO_ROLE" in os.environ:
        print("`hefesto parallel` no puede ejecutarse dentro de un agente (`HEFESTO_ROLE`).", file=sys.stderr)
        return EXIT_BLOCKED
    proj = _project(args)
    results = scheduler.run_parallel(proj, max_parallel=getattr(args, "max_parallel", None))
    failed = False
    for r in results:
        detail_str = f" - {r.detail}" if r.detail else ""
        print(f"{r.task_id}: {r.status} (intentos: {r.attempts}){detail_str}")
        if r.status in ("failed", "conflict"):
            failed = True
    return EXIT_INPUT if failed else EXIT_OK


# ---------------------------------------------------------------------------
# Fase 7: distribución


def cmd_bootstrap(args: argparse.Namespace) -> int:
    from hefesto import distribution

    target = Path(args.dir or getattr(args, "project", None) or ".").resolve()
    return distribution.bootstrap(target, providers=args.providers, yes=getattr(args, "yes", False))


def cmd_adopt(args: argparse.Namespace) -> int:
    from hefesto import distribution

    _reject_project_option(args, "adopt")
    target = Path(args.dir or ".").resolve()
    distribution.validate_safe_project_paths(target)
    for line in distribution.adopt(target, providers=args.providers):
        print(line)
    return EXIT_OK


def cmd_self_update(args: argparse.Namespace) -> int:
    from hefesto import distribution
    from hefesto.util import FRAMEWORK_ROOT

    try:
        proj: Path | None = _project(args)
    except HefestoError:
        proj = None
    msg = distribution.self_update(FRAMEWORK_ROOT, to=args.to, project=proj)
    if msg:
        print(msg)
    return EXIT_OK


def cmd_migrate(args: argparse.Namespace) -> int:
    from hefesto import distribution

    proj = _project(args)
    msg = distribution.migrate(proj)
    if msg:
        print(msg)
    return EXIT_OK


# ---------------------------------------------------------------------------
# Fase 8: ecosistema de skills y packs


def cmd_skill(args: argparse.Namespace) -> int:
    from hefesto import skills, util
    from hefesto.util import FRAMEWORK_ROOT, hefesto_home

    tokens: list[str] = getattr(args, "args", []) or []
    if not tokens:
        raise HefestoError(
            "Indica un término o subcomando: `hefesto skill TÉRMINO` o `hefesto skill find TÉRMINO`.",
            EXIT_INPUT,
        )

    subcmd = tokens[0].lower()
    if subcmd == "suggest":
        if len(tokens) < 2:
            raise HefestoError("Indica el ID de la tarea: `hefesto skill suggest TASK-NNN`.", EXIT_INPUT)
        from hefesto import roadmap, skill_select

        proj = _project(args)
        cfg = config.load(proj)
        rm = roadmap.load_valid(proj)
        task = rm.get(tokens[1])
        choices = skill_select.select(proj, cfg, task.role, task)
        if choices:
            headers = ["NOMBRE", "ORIGEN", "PUNTOS", "MOTIVO"]
            rows = [
                [
                    skills.sanitize_text(str(c.name)),
                    skills.sanitize_text(str(c.source)),
                    skills.sanitize_text(str(c.score)),
                    skills.sanitize_text("; ".join(c.reasons)),
                ]
                for c in choices
            ]
            widths = [len(h) for h in headers]
            for row in rows:
                for i, col in enumerate(row):
                    widths[i] = max(widths[i], len(col))
            print(skills.sanitize_text("  ".join(h.ljust(widths[i]) for i, h in enumerate(headers))))
            for row in rows:
                print(skills.sanitize_text("  ".join(col.ljust(widths[i]) for i, col in enumerate(row))))
        else:
            print(skills.sanitize_text(f"Ninguna skill instalada encaja con {task.id}."))

        if getattr(args, "online", False):
            words = skill_select.keywords(task.role, task)
            term = " ".join(words[:2]).strip()
            if term:
                try:
                    online_results = skills.find(term)
                except HefestoError as exc:
                    print(skills.sanitize_text(f"Aviso: {exc}"))
                    online_results = []
                if online_results:
                    headers_on = ["NOMBRE", "INSTALACIONES", "CONFIANZA"]
                    rows_on = [
                        [
                            skills.sanitize_text(str(r.get("name", ""))),
                            skills.sanitize_text(str(r.get("installs", ""))),
                            skills.sanitize_text(str(r.get("trust", ""))),
                        ]
                        for r in online_results
                    ]
                    widths_on = [len(h) for h in headers_on]
                    for row in rows_on:
                        for i, col in enumerate(row):
                            widths_on[i] = max(widths_on[i], len(col))
                    print(skills.sanitize_text("  ".join(h.ljust(widths_on[i]) for i, h in enumerate(headers_on))))
                    for row in rows_on:
                        print(skills.sanitize_text("  ".join(col.ljust(widths_on[i]) for i, col in enumerate(row))))
                    for r in online_results:
                        name_clean = skills.sanitize_text(str(r.get("name", "")))
                        print(skills.sanitize_text(f"hefesto skill fetch {name_clean}"))
            print(skills.sanitize_text(
                "Nada se ha descargado: revisa y, si te convence, ejecuta tú fetch y luego promote."
            ))
        return EXIT_OK

    if subcmd == "find":
        if len(tokens) < 2:
            raise HefestoError("Indica el término a buscar: `hefesto skill find TÉRMINO`.", EXIT_INPUT)
        term = " ".join(tokens[1:])
        results = skills.find(term, owner=getattr(args, "owner", None))
        if not results:
            print(f"No se encontraron skills para '{term}'.")
            return EXIT_OK
        headers = ["NOMBRE", "INSTALACIONES", "ESTRELLAS", "CONFIANZA", "URL"]
        rows = [
            [
                str(r.get("name", "")),
                str(r.get("installs", "")),
                str(r.get("stars", "")),
                str(r.get("trust", "")),
                str(r.get("url", "")),
            ]
            for r in results
        ]
        widths = [len(h) for h in headers]
        for row in rows:
            for i, col in enumerate(row):
                widths[i] = max(widths[i], len(col))
        print("  ".join(h.ljust(widths[i]) for i, h in enumerate(headers)))
        for row in rows:
            print("  ".join(col.ljust(widths[i]) for i, col in enumerate(row)))
        return EXIT_OK

    if subcmd == "fetch":
        if len(tokens) < 2:
            raise HefestoError("Indica la especificación: `hefesto skill fetch OWNER/REPO@SKILL`.", EXIT_INPUT)
        spec = tokens[1]
        path = skills.fetch(spec)
        print(f"Propuesta descargada en: {path}")
        source = util.read_json(path / "SOURCE.json", default={})
        risks = source.get("risks", []) if isinstance(source, dict) else []
        print("== Informe de riesgos ==")
        if risks:
            for r in risks:
                print(f"- [{skills.sanitize_text(str(r.get('kind', '')))}] {skills.sanitize_text(str(r.get('file', '')))}: {skills.sanitize_text(str(r.get('detail', '')))}")
        else:
            print("Sin riesgos detectados.")
        return EXIT_OK

    if subcmd == "update":
        if len(tokens) < 2:
            raise HefestoError("Indica el nombre de la skill: `hefesto skill update SKILL`.", EXIT_INPUT)
        skill_name = tokens[1]
        promoted_dir = hefesto_home() / "skills" / "promoted" / skill_name
        path = skills.update(skill_name)
        print(f"Propuesta actualizada en: {path}")

        import difflib

        old_files: set[str] = set()
        if promoted_dir.is_dir():
            for p in sorted(promoted_dir.rglob("*")):
                if p.is_file() and p.name != "SOURCE.json":
                    old_files.add(p.relative_to(promoted_dir).as_posix())

        new_files: set[str] = set()
        if path.is_dir():
            for p in sorted(path.rglob("*")):
                if p.is_file() and p.name != "SOURCE.json":
                    new_files.add(p.relative_to(path).as_posix())

        all_rels = sorted(old_files | new_files)
        changed_binaries: list[str] = []

        for rel in all_rels:
            old_p = promoted_dir / rel
            new_p = path / rel
            old_bytes = old_p.read_bytes() if old_p.is_file() else None
            new_bytes = new_p.read_bytes() if new_p.is_file() else None

            if old_bytes == new_bytes:
                continue

            old_text = skills.decode_text(old_bytes) if old_bytes is not None else None
            new_text = skills.decode_text(new_bytes) if new_bytes is not None else None

            is_old_text = (old_bytes is None) or (old_text is not None)
            is_new_text = (new_bytes is None) or (new_text is not None)

            if is_old_text and is_new_text:
                old_lines = old_text.splitlines(keepends=True) if old_text is not None else []
                new_lines = new_text.splitlines(keepends=True) if new_text is not None else []
                fromfile = f"promoted/{skill_name}/{rel}" if old_bytes is not None else "/dev/null"
                tofile = f"_proposals/{path.name}/{rel}" if new_bytes is not None else "/dev/null"
                diff = list(difflib.unified_diff(
                    old_lines,
                    new_lines,
                    fromfile=fromfile,
                    tofile=tofile,
                ))
                if diff:
                    sys.stdout.write(skills.sanitize_text("".join(diff)))
            else:
                changed_binaries.append(rel)

        if changed_binaries:
            print("== Ficheros binarios modificados ==")
            for b in changed_binaries:
                print(f"- {skills.sanitize_text(b)}")

        source = util.read_json(path / "SOURCE.json", default={})
        risks = source.get("risks", []) if isinstance(source, dict) else []
        print("== Informe de riesgos ==")
        if risks:
            for r in risks:
                print(f"- [{skills.sanitize_text(str(r.get('kind', '')))}] {skills.sanitize_text(str(r.get('file', '')))}: {skills.sanitize_text(str(r.get('detail', '')))}")
        else:
            print("Sin riesgos detectados.")

        return EXIT_OK

    term = " ".join(tokens)
    roots: list[Path] = [
        FRAMEWORK_ROOT / "skills",
        hefesto_home() / "skills" / "promoted",
    ]
    try:
        proj = _project(args)
    except HefestoError:
        proj = None
    if proj is not None and (proj / ".claude" / "skills").is_dir():
        roots.append(proj / ".claude" / "skills")

    results = skills.search_local(term, roots)
    if not results:
        print(f"No se encontraron skills locales para '{term}'.")
        print(f"Puedes buscar en el registro público con:\n  hefesto skill find {term}")
        print("O redactar una nueva con la skill hefesto-skill-skill-creator.")
        return EXIT_OK

    for r in results:
        path = Path(r["path"])
        print(f"== {path} ==")
        try:
            print(skills.sanitize_text(path.read_text(encoding="utf-8")))
        except OSError:
            pass
    return EXIT_OK


def cmd_promote(args: argparse.Namespace) -> int:
    from hefesto import skills

    if getattr(args, "yes", False):
        _check_yes_barriers(args)
    path = skills.promote(args.name, yes=getattr(args, "yes", False))
    print(f"Skill promovida en: {path}")
    return EXIT_OK


def cmd_pack_list(args: argparse.Namespace) -> int:
    from hefesto import packs

    try:
        proj: Path | None = _project(args)
    except HefestoError:
        proj = None
    items = packs.list_packs(proj)
    if not items:
        print("No hay packs en el catálogo.")
        return EXIT_OK
    headers = ["NOMBRE", "TIPO", "ESTADO", "POR DEFECTO", "COSTE", "FALTA"]
    rows = []
    for p in items:
        status_s = "activo" if p["enabled"] else "inactivo"
        cost_s = "adicional" if p["extra_cost"] else "incluido"
        missing_s = ", ".join(p["missing"]) if p["missing"] else "-"
        rows.append([
            p["name"],
            p["kind"] or "",
            status_s,
            p["default"] or "",
            cost_s,
            missing_s,
        ])
    widths = [len(h) for h in headers]
    for r in rows:
        for i, val in enumerate(r):
            widths[i] = max(widths[i], len(val))
    print("  ".join(h.ljust(widths[i]) for i, h in enumerate(headers)))
    for r in rows:
        print("  ".join(val.ljust(widths[i]) for i, val in enumerate(r)))
    return EXIT_OK


def cmd_pack_enable(args: argparse.Namespace) -> int:
    from hefesto import packs

    if getattr(args, "yes", False):
        _check_yes_barriers(args)
    proj = _project(args)
    msg = packs.enable(
        proj,
        args.name,
        yes=getattr(args, "yes", False),
        accept_extra_cost=getattr(args, "accept_extra_cost", False),
    )
    if msg:
        print(msg)
    if getattr(msg, "has_conflicts", False):
        return EXIT_BLOCKED
    return EXIT_OK


def cmd_pack_disable(args: argparse.Namespace) -> int:
    from hefesto import packs

    proj = _project(args)
    msg = packs.disable(proj, args.name)
    if msg:
        print(msg)
    if getattr(msg, "has_conflicts", False):
        return EXIT_BLOCKED
    return EXIT_OK


def cmd_pack_update(args: argparse.Namespace) -> int:
    from hefesto import packs

    proj = _project(args)
    paths = packs.update(proj, args.name)
    if paths:
        for p in paths:
            print(f"Propuesta actualizada en: {p}")
    return EXIT_OK


def cmd_limits(args: argparse.Namespace) -> int:
    from hefesto import limits

    if getattr(args, "json", False):
        print(json.dumps(limits.all_limits(), indent=2, ensure_ascii=False))
        return EXIT_OK
    print(limits.render_table())
    return EXIT_OK


def cmd_statusline(args: argparse.Namespace) -> int:
    try:
        from hefesto import limits

        raw = sys.stdin.read()
        print(limits.handle_statusline(raw))
    except Exception:
        print("hefesto")
    return EXIT_OK


def _read_last_log_lines(path: Path, max_bytes: int = 8192, n_lines: int = 8) -> list[str]:
    try:
        if not path.is_file():
            return []
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            offset = max(0, size - max_bytes)
            fh.seek(offset)
            raw = fh.read()
        text = raw.decode("utf-8", errors="replace")
        lines = text.splitlines()
        if offset > 0 and len(lines) > 1:
            lines = lines[1:]
        non_empty = [ln.strip() for ln in lines if ln.strip()]
        return non_empty[-n_lines:] if non_empty else []
    except OSError:
        return []


def summarize_log_line(line: str) -> str:
    from hefesto import skills

    clean = skills.ANSI_RE.sub("", line).strip()
    if not clean:
        return ""
    if clean.startswith("{") and clean.endswith("}"):
        try:
            data = json.loads(clean)
        except Exception:
            data = None
        if isinstance(data, dict):
            su = data.get("step_update")
            if isinstance(su, dict):
                delta = su.get("text_delta")
                if isinstance(delta, str) and delta.strip():
                    return delta.strip()[:160]
                call = su.get("tool_call") or su.get("call")
                if isinstance(call, dict):
                    name = str(call.get("name") or "tool")
                    args = call.get("args") or call.get("input")
                    target = ""
                    if isinstance(args, dict):
                        target = str(args.get("path") or args.get("file_path") or args.get("command") or args.get("target") or "")
                    elif isinstance(args, str):
                        target = args
                    return f"{name} {target}".strip()[:160]
                if su.get("state"):
                    return f"[{su.get('state')}] {su.get('step_type', '')}".strip()[:160]

            delta = data.get("delta")
            if isinstance(delta, dict):
                text = delta.get("text") or delta.get("text_delta")
                if isinstance(text, str) and text.strip():
                    return text.strip()[:160]
                if delta.get("type") == "input_json_delta" or "partial_json" in delta:
                    name = delta.get("name") or data.get("name")
                    return f"(llamada a herramienta: {name})" if name else "(llamada a herramienta)"
                if delta.get("type") == "tool_use" or delta.get("name"):
                    name = delta.get("name") or ""
                    return f"(llamada a herramienta: {name})" if name else "(llamada a herramienta)"
            elif isinstance(delta, str) and delta.strip():
                return delta.strip()[:160]

            if "partial_json" in data or data.get("type") == "input_json_delta":
                name = data.get("name")
                return f"(llamada a herramienta: {name})" if name else "(llamada a herramienta)"

            if data.get("event") == "result":
                res = data.get("result")
                if isinstance(res, dict) and res.get("response"):
                    resp = str(res.get("response")).strip()
                    if not (resp.startswith("{") and resp.endswith("}")):
                        return resp[:160]

            msg = data.get("message")
            if isinstance(msg, dict):
                content = msg.get("content")
                if isinstance(content, list) and content:
                    first = content[0]
                    if isinstance(first, dict):
                        if first.get("type") == "tool_use":
                            name = str(first.get("name") or "tool")
                            inp = first.get("input")
                            target = ""
                            if isinstance(inp, dict):
                                target = str(inp.get("path") or inp.get("file_path") or inp.get("command") or inp.get("target") or "")
                            elif isinstance(inp, str):
                                target = inp
                            return f"{name} {target}".strip()[:160]
                        if first.get("type") == "text":
                            return str(first.get("text") or "").strip()[:160]
                if isinstance(content, str) and content.strip():
                    return content.strip()[:160]
            elif isinstance(msg, str) and msg.strip():
                return msg.strip()[:160]

            cb = data.get("content_block")
            if isinstance(cb, dict):
                if cb.get("type") == "tool_use":
                    name = str(cb.get("name") or "")
                    return f"(llamada a herramienta: {name})" if name else "(llamada a herramienta)"
                if cb.get("text"):
                    return str(cb.get("text")).strip()[:160]

            item = data.get("item")
            if isinstance(item, dict):
                if item.get("text"):
                    return str(item.get("text")).strip()[:160]
                if item.get("type") == "tool_call":
                    name = str(item.get("name") or "")
                    return f"(llamada a herramienta: {name})" if name else "(llamada a herramienta)"

            call = data.get("tool_call") or data.get("call")
            if isinstance(call, dict):
                name = str(call.get("name") or "tool")
                args = call.get("args") or call.get("input")
                target = ""
                if isinstance(args, dict):
                    target = str(args.get("path") or args.get("file_path") or args.get("command") or args.get("target") or "")
                elif isinstance(args, str):
                    target = args
                return f"{name} {target}".strip()[:160]

            for key in ("response", "result", "text", "summary", "error", "content"):
                val = data.get(key)
                if isinstance(val, str) and val.strip():
                    val_str = val.strip()
                    if not (val_str.startswith("{") and val_str.endswith("}")):
                        return val_str[:160]

            dtype = data.get("type")
            if dtype and isinstance(dtype, str):
                if dtype in ("content_block_delta", "input_json_delta"):
                    name = data.get("name")
                    return f"(llamada a herramienta: {name})" if name else "(llamada a herramienta)"
                return f"[{dtype}]"[:160]
            return ""
    if clean.startswith("{") or clean.startswith("["):
        return "(llamada a herramienta)"
    return clean[:160]


def _active_log_lines(project: Path, raw_path: str, n_lines: int) -> list[str]:
    from hefesto import activity

    try:
        path = activity.resolve_log_path(project, raw_path)
    except (OSError, ValueError, RuntimeError):
        return ["(ruta de log no válida)"]
    return _read_last_log_lines(path, max_bytes=8192, n_lines=n_lines)


def cmd_jobs(args: argparse.Namespace) -> int:
    from hefesto import activity, skills, util

    proj = _project(args)
    active = activity.list_active(proj)
    items: list[dict[str, Any]] = []
    now = util.utcnow()
    for entry in active:
        item = dict(entry)
        try:
            started = util.parse_iso(entry.get("started_at") or util.now_iso())
            elapsed = max(0, int((now - started).total_seconds()))
        except Exception:
            elapsed = 0
        item["seconds"] = elapsed
        item["elapsed_seconds"] = elapsed
        last_lines = _active_log_lines(proj, entry.get("log_path", ""), n_lines=1)
        raw_last = last_lines[-1] if last_lines else ""
        item["last_line"] = skills.sanitize_text(skills.ANSI_RE.sub("", raw_last))
        items.append(item)

    if getattr(args, "json", False):
        print(json.dumps(items, indent=2, ensure_ascii=False))
        return EXIT_OK

    if not items:
        print("No hay agentes activos.")
        return EXIT_OK

    headers = ["TIPO", "ROL", "PROVEEDOR", "TAREA", "TIEMPO", "ÚLTIMA LÍNEA"]
    rows = []
    for it in items:
        summary = summarize_log_line(str(it.get("last_line") or ""))
        clean_last = skills.sanitize_text(skills.ANSI_RE.sub("", summary))
        rows.append([
            str(it.get("kind") or "worker"),
            str(it.get("role") or ""),
            str(it.get("provider") or ""),
            str(it.get("task_id") or "-"),
            f"{it.get('seconds', 0)}s",
            clean_last,
        ])
    widths = [len(h) for h in headers]
    for r in rows:
        for i, val in enumerate(r):
            widths[i] = max(widths[i], len(val))
    print("  ".join(h.ljust(widths[i]) for i, h in enumerate(headers)))
    for r in rows:
        print("  ".join(val.ljust(widths[i]) for i, val in enumerate(r)))
    return EXIT_OK


def cmd_watch(args: argparse.Namespace) -> int:
    import io
    import time
    from hefesto import activity, limits, skills, util

    proj = _project(args)
    interval = float(getattr(args, "interval", 2.0) or 2.0)
    once = getattr(args, "once", False)

    def render() -> str:
        buf = io.StringIO()
        buf.write("== Límites ==\n")
        buf.write(limits.render_table() + "\n\n")

        active = activity.list_active(proj)
        if not active:
            buf.write("No hay agentes activos.\n")
        else:
            now = util.utcnow()
            for entry in active:
                prov = str(entry.get("provider") or "")
                role = str(entry.get("role") or "")
                task = str(entry.get("task_id") or "-")
                try:
                    started = util.parse_iso(entry.get("started_at") or util.now_iso())
                    elapsed = max(0, int((now - started).total_seconds()))
                except Exception:
                    elapsed = 0
                buf.write(f"[{prov}] rol={role} tarea={task} ({elapsed}s)\n")
                lines = _active_log_lines(proj, entry.get("log_path", ""), n_lines=8)
                if not lines:
                    buf.write("  (sin salida)\n")
                else:
                    for l in lines:
                        summary = summarize_log_line(l)
                        clean = skills.sanitize_text(skills.ANSI_RE.sub("", summary))
                        buf.write(f"  {clean}\n")
                buf.write("\n")
        return buf.getvalue().rstrip() + "\n"

    if once:
        sys.stdout.write(render())
        sys.stdout.flush()
        return EXIT_OK

    try:
        while True:
            sys.stdout.write("\033[2J\033[H")
            sys.stdout.write(render())
            sys.stdout.flush()
            time.sleep(interval)
    except KeyboardInterrupt:
        return EXIT_OK


# ---------------------------------------------------------------------------
# Pack memory: memoria global entre proyectos


def cmd_remember(args: argparse.Namespace) -> int:
    from hefesto import memory

    proj: Path | None = None
    if getattr(args, "project", None) or args.scope == "project":
        try:
            proj = _project(args)
        except HefestoError:
            if args.scope == "project":
                raise
            proj = None
    else:
        try:
            proj = _project(args)
        except HefestoError:
            proj = None

    path = memory.remember(
        proj,
        args.text,
        scope=args.scope,
        kind=args.kind,
        stack=getattr(args, "stack", None),
    )
    if args.scope == "global":
        print(f"Candidata global registrada en {path}.")
    else:
        print(f"Nota registrada en {path}.")
    return EXIT_OK


def cmd_memory_review(args: argparse.Namespace) -> int:
    from hefesto import memory

    if getattr(args, "yes", False):
        _check_yes_barriers(args)
    try:
        proj: Path | None = _project(args)
    except HefestoError:
        proj = None
    msg = memory.review(proj, yes=getattr(args, "yes", False))
    print(msg)
    return EXIT_OK


def cmd_memory_prune(args: argparse.Namespace) -> int:
    from hefesto import memory

    if getattr(args, "yes", False):
        _check_yes_barriers(args)
    try:
        proj: Path | None = _project(args)
    except HefestoError:
        proj = None
    msg = memory.prune(proj, yes=getattr(args, "yes", False))
    print(msg)
    return EXIT_OK


def cmd_memory_search(args: argparse.Namespace) -> int:
    from hefesto import memory

    results = memory.search(args.term, limit=getattr(args, "limit", 10))
    if not results:
        print(f"No se encontraron resultados para '{args.term}'.")
        return EXIT_OK
    for r in results:
        print(f"== {r['path']} ==")
        print(r["snippet"])
    return EXIT_OK


# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="hefesto", description="Orquestación multiagente determinista.")
    parser.add_argument("--version", action="version", version=f"hefesto {__version__}")
    parser.add_argument("-C", "--project", help="directorio del proyecto (por defecto, el actual)")
    sub = parser.add_subparsers(dest="command", metavar="COMANDO")

    p = sub.add_parser("init", help="inicializa Hefesto en un proyecto (idempotente)")
    p.add_argument("dir", nargs="?", help="directorio del proyecto")
    p.add_argument("--providers", help="proveedores externos autorizados, p. ej. gemini,codex ('' = ninguno)")
    p.add_argument("--shared", action="store_true", help="modo compartido: versiona ficheros en la raíz y .gitignore")
    p.set_defaults(func=cmd_init)

    p = sub.add_parser("bootstrap", help="detecta el modo de trabajo (crear, continuar, mejorar) y arranca")
    p.add_argument("dir", nargs="?", help="directorio del proyecto")
    p.add_argument("--providers", help="proveedores externos autorizados, p. ej. gemini,codex ('' = ninguno)")
    p.add_argument("--yes", action="store_true", help="arranca sin confirmación interactiva ni TTY")
    p.set_defaults(func=cmd_bootstrap)

    p = sub.add_parser("adopt", help="adopta un proyecto existente sin modificar su código")
    p.add_argument("dir", nargs="?", help="directorio del proyecto")
    p.add_argument("--providers", help="proveedores externos autorizados, p. ej. gemini,codex ('' = ninguno)")
    p.set_defaults(func=cmd_adopt)

    p = sub.add_parser("self-update", help="actualiza la copia instalada de Hefesto al último tag o al indicado")
    p.add_argument("--to", help="tag de la versión destino (por defecto, la más reciente)")
    p.set_defaults(func=cmd_self_update)

    p = sub.add_parser("migrate", help="aplica migraciones de configuración y plantillas si la versión cambió")
    p.set_defaults(func=cmd_migrate)

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
    p.add_argument("--protocol", action="store_true", help="imprime también el contenido del protocolo (AGENTS.md)")
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
    p.add_argument("--yes", action="store_true", help="desbloquea sin TTY si no es headless ni se usa -C")
    p.set_defaults(func=cmd_unblock)

    p = sub.add_parser("trust", help="aprueba los comandos verify del ROADMAP (solo humanos, TTY)")
    p.add_argument("--yes", action="store_true", help="aprueba sin TTY si no es headless ni se usa -C")
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
    p.add_argument("--yes", action="store_true", help="integra sin confirmación interactiva ni TTY")
    p.set_defaults(func=cmd_finish)

    p = sub.add_parser("evidence", help="muestra la evidencia de una tarea o comprueba la cadena (`verify`)")
    p.add_argument("target", help="TASK-NNN o `verify`")
    p.set_defaults(func=cmd_evidence)

    p = sub.add_parser("clean", help="elimina worktrees y ramas hefesto huérfanos sin cambios")
    p.add_argument("--yes", action="store_true", help="limpia sin confirmación interactiva ni TTY")
    p.add_argument("--include-unmerged", action="store_true", help="elimina también ramas huérfanas no integradas")
    p.set_defaults(func=cmd_clean)

    p = sub.add_parser("discard", help="revierte una tarea o descarta una fase dejando constancia en DECISIONS")
    p.add_argument("target", help="ID de tarea (TASK-NNN) o slug/nombre de fase")
    p.add_argument("--yes", action="store_true", help="descarta sin confirmación interactiva ni TTY")
    p.set_defaults(func=cmd_discard)

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
    p.add_argument("task", nargs="?", default=None, help="ID de la tarea (opcional; si se omite, muestra el lote paralelo)")
    p.add_argument("--dry-run", action="store_true", required=True, help="obligatorio: route nunca llama a nada")
    p.set_defaults(func=cmd_route)

    p = sub.add_parser("parallel", help="ejecuta tareas en paralelo respetando concurrencia y límites")
    p.add_argument("--max-parallel", type=int, help="máximo de tareas en paralelo")
    p.set_defaults(func=cmd_parallel)

    p = sub.add_parser("providers", help="estado de proveedores, allowlist del proyecto y cooldowns")
    p.add_argument("action", choices=["status", "check", "add", "remove", "reset"])
    p.add_argument("name", nargs="?", choices=list(config.PROVIDERS))
    p.add_argument("--probe", action="store_true", help="con `check`: sonda de una llamada mínima por proveedor")
    p.set_defaults(func=cmd_providers)

    p = sub.add_parser("local", help="activa o desactiva el modelo local (API compatible con OpenAI)")
    p.add_argument("action", choices=["enable", "disable"])
    p.add_argument("--base-url")
    p.add_argument("--model")
    p.add_argument("--role", choices=list(config.ROLES), help="añade `local` a la cadena de este rol")
    p.add_argument("--first", action="store_true", help="al principio de la cadena (por defecto, al final)")
    p.add_argument("--global", dest="global_scope", action="store_true", help="en ~/hefesto/settings.json")
    p.set_defaults(func=cmd_local)

    p = sub.add_parser("sync", help="regenera skills, subagentes, comandos y hooks de Claude, Codex y Gemini")
    p.add_argument("--check", action="store_true", help="solo comprueba; sale con 1 si hay cambios pendientes")
    p.set_defaults(func=cmd_sync)

    p = sub.add_parser("note", help="registra un paso de progreso de la tarea en curso")
    p.add_argument("text")
    p.set_defaults(func=cmd_note)

    p = sub.add_parser("resume", help="resumen para que cualquier orquestador continúe donde lo dejó el anterior")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_resume)

    p = sub.add_parser("handoff", help="relevo limpio a otro orquestador")
    p.add_argument("--to", choices=["claude", "codex", "gemini"])
    p.add_argument("--reason", choices=["quota", "manual"], default="manual")
    p.add_argument("--from", dest="source", choices=["claude", "codex", "gemini"],
                   help="orquestador saliente (por defecto, el detectado)")
    p.set_defaults(func=cmd_handoff)

    p = sub.add_parser("loop", help="ciclo headless con topes, STOP y relevo automático")
    p.add_argument("--orchestrator", choices=["claude", "codex", "gemini", "auto"])
    p.add_argument("--max-tasks", type=int)
    p.add_argument("--max-turns", type=int)
    p.add_argument("--wait", action="store_true", help="si no hay cuota, espera al primer cooldown_until")
    p.set_defaults(func=cmd_loop)

    p = sub.add_parser("stop", help="crea .hefesto/STOP para detener el loop")
    p.set_defaults(func=cmd_stop)

    p = sub.add_parser("prompt", help="imprime un paso del protocolo (para orquestadores sin comandos propios)")
    p.add_argument("step", choices=["plan", "next", "retro"])
    p.set_defaults(func=cmd_prompt)

    p = sub.add_parser("skill", help="busca y gestiona skills locales y del registro público")
    p.add_argument("args", nargs="*", help="subcomando (find, fetch, update, suggest) o término de búsqueda")
    p.add_argument("--owner", help="filtra por propietario en `skill find`")
    p.add_argument("--online", action="store_true", help="con suggest, busca también en skills.sh; nunca descarga")
    p.set_defaults(func=cmd_skill)

    p = sub.add_parser("promote", help="promueve una propuesta de skill a promovidas")
    p.add_argument("name", help="nombre de la propuesta (en skills/_proposals)")
    p.add_argument("--yes", action="store_true", help="promueve sin confirmación interactiva ni TTY")
    p.set_defaults(func=cmd_promote)

    p = sub.add_parser("pack", help="catálogo y gestión de packs externos curados")
    psub = p.add_subparsers(dest="pack_cmd", metavar="ACCIÓN", required=True)

    c = psub.add_parser("list", help="muestra los packs del catálogo y su estado")
    c.set_defaults(func=cmd_pack_list)

    c = psub.add_parser("enable", help="activa un pack en el proyecto")
    c.add_argument("name", help="nombre del pack")
    c.add_argument("--yes", action="store_true", help="activa sin confirmación interactiva ni TTY")
    c.add_argument("--accept-extra-cost", action="store_true", help="acepta costes adicionales del pack")
    c.set_defaults(func=cmd_pack_enable)

    c = psub.add_parser("disable", help="desactiva un pack en el proyecto")
    c.add_argument("name", help="nombre del pack")
    c.set_defaults(func=cmd_pack_disable)

    c = psub.add_parser("update", help="descarga la nueva versión de las skills de un pack")
    c.add_argument("name", help="nombre del pack")
    c.set_defaults(func=cmd_pack_update)

    p = sub.add_parser("limits", help="uso real de ventanas de 5 h y semanal de cada proveedor")
    p.add_argument("--json", action="store_true", help="emite la lista de límites en JSON")
    p.set_defaults(func=cmd_limits)

    p = sub.add_parser("statusline", help="comando de línea de estado para Claude Code (lee JSON por stdin)")
    p.set_defaults(func=cmd_statusline)

    p = sub.add_parser("jobs", help="muestra los agentes activos y la última línea de su log")
    p.add_argument("--json", action="store_true", help="emite la lista de agentes activos en JSON")
    p.set_defaults(func=cmd_jobs)

    p = sub.add_parser("watch", help="monitor de límites y actividad de agentes en directo")
    p.add_argument("--once", action="store_true", help="imprime una vez y sale")
    p.add_argument("--interval", type=float, default=2.0, help="intervalo de refresco en segundos (por defecto 2)")
    p.set_defaults(func=cmd_watch)

    p = sub.add_parser("remember", help="guarda una nota en la memoria local o global")
    p.add_argument("text", help="texto de la lección, preferencia o error resuelto")
    p.add_argument("--scope", choices=["project", "global"], default="project",
                   help="ámbito de la nota (project: .hefesto/LEARNINGS.md; global: _pendientes/)")
    p.add_argument("--kind", choices=["preferencias", "stacks", "errores-resueltos", "decisiones"],
                   default="errores-resueltos", help="categoría para la memoria global")
    p.add_argument("--stack", help="tecnología asociada si kind=stacks")
    p.set_defaults(func=cmd_remember)

    p = sub.add_parser("memory", help="gestión de la memoria global entre proyectos")
    msub = p.add_subparsers(dest="memory_cmd", metavar="ACCIÓN", required=True)

    c = msub.add_parser("review", help="revisa notas pendientes de la memoria global")
    c.add_argument("--yes", action="store_true", help="aprueba todas sin confirmación interactiva ni TTY")
    c.set_defaults(func=cmd_memory_review)

    c = msub.add_parser("prune", help="limpia duplicados y notas obsoletas de la memoria")
    c.add_argument("--yes", action="store_true", help="elimina todas las propuestas sin confirmación")
    c.set_defaults(func=cmd_memory_prune)

    c = msub.add_parser("search", help="busca en la memoria global")
    c.add_argument("term", help="término a buscar")
    c.add_argument("--limit", type=int, default=10, help="máximo de resultados")
    c.set_defaults(func=cmd_memory_search)

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
