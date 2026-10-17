"""Instalación automática de skills desde el registro público cuando las locales no bastan."""

from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from hefesto import config, roadmap, skill_select, skills, util
from hefesto.util import HefestoError


MAX_FETCH_ATTEMPTS = 3


@dataclass
class AutoResult:
    installed: list[str] = field(default_factory=list)
    rejected: list[tuple[str, str]] = field(default_factory=list)
    skipped: str | None = None


def _log_event(record: dict[str, Any]) -> None:
    path = util.hefesto_home() / "skills" / "auto.jsonl"
    try:
        if hasattr(util, "append_jsonl"):
            util.append_jsonl(path, record)
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "a", encoding="utf-8", newline="\n") as fh:
                fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    except (OSError, TypeError, ValueError):
        return


def _is_recent_search(term: str, ttl_hours: int) -> bool:
    if ttl_hours <= 0:
        return False
    log_path = util.hefesto_home() / "skills" / "auto.jsonl"
    try:
        now_dt = datetime.fromisoformat(util.now_iso())
        entries = util.read_jsonl(log_path)
    except (OSError, UnicodeDecodeError, ValueError, json.JSONDecodeError):
        return False
    for entry in entries:
        if not isinstance(entry, dict) or entry.get("event") != "search":
            continue
        if entry.get("term") != term:
            continue
        raw_at = entry.get("at")
        if not isinstance(raw_at, str):
            continue
        try:
            prev_dt = datetime.fromisoformat(raw_at)
            elapsed_seconds = (now_dt - prev_dt).total_seconds()
        except (ValueError, TypeError):
            continue
        if elapsed_seconds < ttl_hours * 3600:
            return True
    return False


def ensure(
    project: Path,
    cfg: config.Config,
    role: str,
    task: roadmap.Task,
) -> AutoResult:
    if "HEFESTO_ROLE" in os.environ:
        return AutoResult(skipped="trabajador delegado: nunca instala")
    if not bool(cfg.get("skills_auto", "enabled", default=True)):
        return AutoResult(skipped="desactivado")

    good_score = int(cfg.get("skills_auto", "good_score", default=6))
    choices = skill_select.select(project, cfg, role, task)
    if any(c.score >= good_score for c in choices):
        return AutoResult(skipped="las skills instaladas bastan")

    term = " ".join(skill_select.keywords(role, task)[:2]).strip()
    if not term:
        return AutoResult(skipped="sin palabras clave")

    ttl_hours = int(cfg.get("skills_auto", "search_ttl_hours", default=24))
    if _is_recent_search(term, ttl_hours):
        return AutoResult(skipped="búsqueda reciente")

    task_id = getattr(task, "id", "") or ""
    _log_event({"event": "search", "at": util.now_iso(), "term": term, "task": task_id})

    try:
        found = skills.find(term)
    except (HefestoError, OSError) as exc:
        _log_event({
            "event": "error",
            "at": util.now_iso(),
            "stage": "find",
            "term": term,
            "task": task_id,
            "reason": str(exc),
        })
        return AutoResult()

    min_installs = int(cfg.get("skills_auto", "min_installs", default=1000))
    min_stars = int(cfg.get("skills_auto", "min_stars", default=100))
    installed_names = {
        info.name.casefold()
        for info in skill_select.discover(skill_select.default_roots(project, cfg))
    }

    candidates: list[dict[str, Any]] = []
    for item in found:
        if not isinstance(item, dict):
            continue
        installs = item.get("installs")
        stars = item.get("stars")
        if isinstance(installs, bool) or not isinstance(installs, int) or installs < min_installs:
            continue
        if isinstance(stars, bool) or not isinstance(stars, int) or stars < min_stars:
            continue
        skill_name = item.get("skill")
        spec = item.get("name")
        if not isinstance(skill_name, str) or not skill_name or not isinstance(spec, str) or not spec:
            continue
        if skill_name.casefold().startswith("hefesto-"):
            continue
        if skill_name.casefold() in installed_names:
            continue
        candidates.append(item)

    candidates.sort(key=lambda r: (-r["installs"], -r["stars"], r["name"]))

    max_per_task = int(cfg.get("skills_auto", "max_per_task", default=1))
    result = AutoResult()
    fetch_attempts = 0

    for cand in candidates:
        if len(result.installed) >= max_per_task or fetch_attempts >= MAX_FETCH_ATTEMPTS:
            break
        spec = cand["name"]
        skill_name = cand["skill"]
        if skill_name.casefold() in installed_names:
            continue
        fetch_attempts += 1
        try:
            proposal = skills.fetch(spec)
        except (HefestoError, OSError) as exc:
            _log_event({
                "event": "error",
                "at": util.now_iso(),
                "stage": "fetch",
                "spec": spec,
                "skill": skill_name,
                "task": task_id,
                "reason": str(exc),
            })
            continue

        try:
            source_data = util.read_json(proposal / "SOURCE.json", default={})
            risks: list[dict[str, str]] = []
            if isinstance(source_data, dict) and isinstance(source_data.get("risks"), list):
                risks.extend(r_item for r_item in source_data["risks"] if isinstance(r_item, dict))
            _, id_risks = skills.skill_identity((proposal / "SKILL.md").read_bytes(), skill_name)
            for r_item in id_risks:
                if r_item not in risks:
                    risks.append(r_item)
        except (HefestoError, OSError, ValueError) as exc:
            _log_event({
                "event": "error",
                "at": util.now_iso(),
                "stage": "inspect",
                "spec": spec,
                "skill": skill_name,
                "task": task_id,
                "reason": str(exc),
            })
            continue

        if risks:
            try:
                skills.check_confinement(proposal, util.hefesto_home())
                shutil.rmtree(proposal, ignore_errors=True)
            except (HefestoError, OSError):
                pass
            kinds = ", ".join(sorted({str(x.get("kind", "riesgo")) for x in risks}))
            reason = f"riesgos detectados: {kinds}"
            result.rejected.append((spec, reason))
            _log_event({
                "event": "rejected",
                "at": util.now_iso(),
                "spec": spec,
                "skill": skill_name,
                "reason": reason,
                "risks": risks,
                "task": task_id,
            })
            continue

        findings = skills.agentshield_scan(proposal)
        if findings and skills.agentshield_blocks(findings):
            try:
                skills.check_confinement(proposal, util.hefesto_home())
                shutil.rmtree(proposal, ignore_errors=True)
            except (HefestoError, OSError):
                pass
            reason = "agentshield: hallazgos high/critical"
            result.rejected.append((spec, reason))
            _log_event({
                "event": "rejected",
                "at": util.now_iso(),
                "spec": spec,
                "skill": skill_name,
                "reason": reason,
                "findings": findings,
                "task": task_id,
            })
            continue

        try:
            with contextlib.redirect_stdout(io.StringIO()):
                skills.promote(proposal.name, yes=True)
        except (HefestoError, OSError) as exc:
            _log_event({
                "event": "error",
                "at": util.now_iso(),
                "stage": "promote",
                "spec": spec,
                "skill": skill_name,
                "task": task_id,
                "reason": str(exc),
            })
            continue

        result.installed.append(skill_name)
        installed_names.add(skill_name.casefold())
        _log_event({
            "event": "installed",
            "at": util.now_iso(),
            "spec": spec,
            "skill": skill_name,
            "installs": cand["installs"],
            "stars": cand["stars"],
            "task": task_id,
        })

    return result
