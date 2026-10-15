"""Progreso continuo y relevo entre orquestadores: `note`, `resume` y `handoff`.

Todo el estado vive en `hefesto`, no en la conversación: cualquier orquestador puede continuar donde
lo dejó otro leyendo `hefesto resume`.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from hefesto import acceptance, config, gitops, providers, quota, state
from hefesto.guard import redact
from hefesto.util import EXIT_INPUT, HefestoError, append_jsonl, now_iso, read_jsonl

ORCHESTRATOR_COMMANDS = {
    "claude": 'claude "/hefesto-next"',
    "codex": 'codex "$hefesto-next"',
    "gemini": 'gemini -i "/hefesto:next"',
}
MAX_NOTES = 8
MAX_RUNS = 10


def progress_path(project: Path) -> Path:
    return project / ".hefesto" / "progress.jsonl"


def current_orchestrator() -> str:
    """Quién está orquestando: `HEFESTO_ORCHESTRATOR` (lo fija `loop`) o las variables de cada CLI."""
    explicit = os.environ.get("HEFESTO_ORCHESTRATOR", "").strip()
    if explicit:
        return explicit
    if os.environ.get("CLAUDECODE") == "1":
        return "claude"
    if os.environ.get("GEMINI_CLI") == "1":
        return "gemini"
    if any(key.startswith("CODEX_") for key in os.environ):
        return "codex"
    return "unknown"


def note(project: Path, text: str, orchestrator: str | None = None) -> dict[str, Any]:
    if not text.strip():
        raise HefestoError("La nota está vacía.", EXIT_INPUT)
    data = state.load(project)
    redacted_text = redact(text.strip())[:2000]
    record = {"task_id": data.get("current_task_id"), "run_id": data.get("run_id"),
              "orchestrator": orchestrator or current_orchestrator(), "text": redacted_text, "at": now_iso()}
    append_jsonl(progress_path(project), record)
    return record


def resume_data(project: Path) -> dict[str, Any]:
    from hefesto import memory

    data = state.load(project)
    out: dict[str, Any] = {"status": data["status"], "task": None, "next_action": data["next_action"],
                           "last_error": data.get("last_error"), "notes": [], "runs": [], "review": None,
                           "diff_stat": "", "branch": data.get("branch"), "workdir": data.get("workdir"),
                           "graph": "", "memory": []}
    notes = read_jsonl(progress_path(project))
    task_obj = None
    if data.get("current_task_id"):
        from hefesto import packs, roadmap

        cfg = config.load(project)
        task = roadmap.load_valid(project).get(data["current_task_id"])
        task_obj = task
        out["task"] = {"id": task.id, "title": task.title, "role": data.get("active_role") or task.role,
                       "attempts": data["attempts"], "verify": task.verify, "files": task.files}
        notes = [n for n in notes if n.get("run_id") == data.get("run_id")]
        runs = acceptance.runs_for_task(project, data.get("run_id"))
        out["runs"] = [{"run_id": r.get("run_id"), "role": r.get("role"), "model_id": r.get("model_id"),
                        "success": r.get("success"), "error_type": r.get("error_type"),
                        "summary": str(r.get("summary") or "")[:300]} for r in runs[-MAX_RUNS:]]
        out["review"] = data.get("review")
        workdir = Path(data.get("workdir") or project)
        if workdir.exists() and data.get("task_base"):
            stat = gitops.git(workdir, "diff", "--stat", data["task_base"], check=False).stdout.strip()
            untracked = [p for p in gitops.dirty_paths(workdir) if p not in stat]
            out["diff_stat"] = stat + (f"\n sin versionar: {', '.join(untracked[:10])}" if untracked else "")
        if packs.is_pack_enabled(project, "context"):
            graph_extract = packs.query_graph(project, workdir, task, cfg)
            from hefesto import skills
            out["graph"] = skills.sanitize_text(redact(graph_extract))
    out["notes"] = notes[-MAX_NOTES:]
    out["memory"] = [redact(m) for m in memory.relevant(project, task=task_obj)]
    return out


def render_resume(info: dict[str, Any]) -> str:
    lines = [f"== hefesto resume ({info['status']}) =="]
    task = info.get("task")
    if task:
        lines.append(f"tarea: {task['id']} — {task['title']}  rol: {task['role']}  intentos: {task['attempts']}")
        lines.append(f"verify: {task['verify']}   files: {', '.join(task['files'])}")
        lines.append(f"rama: {info.get('branch') or '-'}   directorio: {info.get('workdir') or '-'}")
    if info.get("last_error"):
        lines.append(f"último error: {str(info['last_error'])[:400]}")
    if info["runs"]:
        lines.append("delegaciones de esta tarea (no las repitas si tuvieron éxito):")
        for run in info["runs"]:
            state_txt = "ok" if run["success"] else (run.get("error_type") or "falló")
            lines.append(f"  - {run['run_id']} {run['role']:<8} {run['model_id']:<16} {state_txt}: {run['summary'][:160]}")
    review = info.get("review")
    if review:
        lines.append(f"revisión registrada: {review.get('verdict')} por {review.get('reviewer')}")
    if info["notes"]:
        lines.append("notas de progreso:")
        lines += [f"  - [{n.get('orchestrator')}] {n.get('text')}" for n in info["notes"]]
    if info.get("diff_stat"):
        lines.append("cambios de la tarea:\n" + "\n".join("  " + line for line in info["diff_stat"].splitlines()))
    if info.get("graph"):
        lines.append("grafo de código:")
        graph_lines = [l.strip() for l in info["graph"].strip().splitlines() if l.strip()]
        lines += [f"  {l[:160]}" for l in graph_lines[:8]]
    if info.get("memory"):
        lines.append("memoria global:")
        for m in info["memory"]:
            lines.append(f"  - {m}")
    lines.append(f"siguiente acción: {info['next_action']}")
    return "\n".join(lines)


def orchestrator_available(cfg: config.Config, name: str) -> tuple[bool, str]:
    return providers.available(cfg, name)


def next_orchestrator(cfg: config.Config, exclude: str | None = None) -> str | None:
    for entry in cfg.get("orchestrator", "use", default=[]) or []:
        name = providers.split_entry(entry)[0]
        if name != exclude and orchestrator_available(cfg, name)[0]:
            return name
    return None


def handoff(project: Path, to: str | None, reason: str, source: str | None = None) -> str:
    cfg = config.load(project)
    source = source or current_orchestrator()
    if reason == "quota" and source in ORCHESTRATOR_COMMANDS:
        until = quota.cooldown_until("", int(cfg.get("quota", "cooldown_default_minutes")))
        quota.mark_cooldown(source, until, "quota", "relevo manual por cuota")
    target = to or next_orchestrator(cfg, exclude=source)
    note(project, f"relevo ({reason}) de {source} a {target or 'nadie disponible'}", orchestrator=source)
    if not target:
        return "No hay otro orquestador disponible ahora. El estado queda guardado; `hefesto resume` lo retomará."
    ok, why = orchestrator_available(cfg, target)
    warning = "" if ok else f"\nAviso: {target} no está disponible ({why})."
    return (f"Relevo registrado. Para continuar con {target}:\n\n  cd {project}\n  {ORCHESTRATOR_COMMANDS[target]}\n\n"
            f"o abre `{target}` en el proyecto y di «continúa»: su primera acción será `hefesto resume`.{warning}")
