"""Límites de uso reales de los proveedores y comando statusline."""

from __future__ import annotations

import datetime as dt
import json
import math
import os
from pathlib import Path
from typing import Any

from hefesto import util


def _parse_window(win: Any) -> dict[str, Any] | str:
    if not isinstance(win, dict):
        return "unknown"
    try:
        raw_used = win.get("used_percent")
        if raw_used is None:
            raw_used = win.get("used_percentage")
        resets_at = win.get("resets_at")
        if raw_used is None or resets_at is None:
            return "unknown"
        if isinstance(raw_used, bool) or isinstance(resets_at, bool):
            return "unknown"

        try:
            used_percent = float(raw_used)
        except (TypeError, ValueError, OverflowError):
            return "unknown"

        if not math.isfinite(used_percent) or not (0.0 <= used_percent <= 100.0):
            return "unknown"

        if isinstance(resets_at, (int, float)):
            if not math.isfinite(resets_at) or resets_at < 0:
                return "unknown"
            try:
                resets_dt = dt.datetime.fromtimestamp(resets_at, tz=dt.timezone.utc).replace(microsecond=0)
            except (ValueError, OverflowError, OSError):
                return "unknown"
        elif isinstance(resets_at, str):
            cleaned_str = resets_at.strip()
            is_numeric = False
            try:
                val = float(cleaned_str)
                is_numeric = True
            except ValueError:
                is_numeric = False

            if is_numeric:
                if not math.isfinite(val) or val < 0:
                    return "unknown"
                try:
                    resets_dt = dt.datetime.fromtimestamp(val, tz=dt.timezone.utc).replace(microsecond=0)
                except (ValueError, OverflowError, OSError):
                    return "unknown"
            else:
                try:
                    cleaned = cleaned_str.replace("Z", "+00:00")
                    resets_dt = dt.datetime.fromisoformat(cleaned)
                    if resets_dt.tzinfo is None:
                        resets_dt = resets_dt.replace(tzinfo=dt.timezone.utc)
                    resets_dt = resets_dt.astimezone(dt.timezone.utc).replace(microsecond=0)
                except (ValueError, OSError):
                    return "unknown"
        else:
            return "unknown"

        if resets_dt.year < 1970 or resets_dt.year > 2100:
            return "unknown"

        now = dt.datetime.now(dt.timezone.utc)
        if resets_dt <= now:
            used_percent = 0.0

        return {
            "used_percent": used_percent,
            "resets_at": resets_dt.isoformat(),
        }
    except Exception:
        return "unknown"


def codex_limits() -> dict[str, Any]:
    """Lee los límites reales de Codex de las sesiones más recientes en $CODEX_HOME/sessions."""
    try:
        codex_home = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")
        sessions_dir = codex_home / "sessions"
        if not sessions_dir.is_dir():
            return {
                "provider": "codex",
                "five_hour": "unknown",
                "weekly": "unknown",
                "observed_at": None,
                "plan": None,
            }

        try:
            candidates = list(sessions_dir.rglob("rollout-*.jsonl"))
        except OSError:
            candidates = []

        mtime_list: list[tuple[float, Path]] = []
        for p in candidates:
            try:
                if p.is_file():
                    st = p.stat()
                    mtime_list.append((st.st_mtime, p))
            except OSError:
                continue

        mtime_list.sort(key=lambda item: item[0], reverse=True)
        session_files = [p for _, p in mtime_list[:20]]

        CHUNK_SIZE = 512 * 1024
        for session_path in session_files:
            try:
                with session_path.open("rb") as f:
                    f.seek(0, os.SEEK_END)
                    size = f.tell()
                    if size > CHUNK_SIZE:
                        f.seek(size - CHUNK_SIZE, os.SEEK_SET)
                        raw = f.read(CHUNK_SIZE)
                        first_nl = raw.find(b"\n")
                        if first_nl != -1:
                            raw = raw[first_nl + 1 :]
                        else:
                            raw = b""
                    else:
                        f.seek(0, os.SEEK_SET)
                        raw = f.read()
                content = raw.decode("utf-8", errors="replace")
            except OSError:
                continue

            lines = content.splitlines()
            for line in reversed(lines):
                line = line.strip()
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                except Exception:
                    continue
                if not isinstance(ev, dict) or ev.get("type") != "event_msg":
                    continue
                payload = ev.get("payload")
                if not isinstance(payload, dict) or payload.get("type") != "token_count":
                    continue
                rate_limits = payload.get("rate_limits")
                if not isinstance(rate_limits, dict):
                    continue
                primary = rate_limits.get("primary")
                if primary is None or not isinstance(primary, dict):
                    continue

                try:
                    five_hour = _parse_window(primary)
                    secondary = rate_limits.get("secondary")
                    weekly = _parse_window(secondary)
                    plan = rate_limits.get("plan_type") or rate_limits.get("plan")
                    observed_at = ev.get("timestamp") or ev.get("observed_at")
                    return {
                        "provider": "codex",
                        "five_hour": five_hour,
                        "weekly": weekly,
                        "observed_at": str(observed_at) if observed_at is not None else None,
                        "plan": str(plan) if plan is not None else None,
                    }
                except Exception:
                    continue

        return {
            "provider": "codex",
            "five_hour": "unknown",
            "weekly": "unknown",
            "observed_at": None,
            "plan": None,
        }
    except Exception:
        return {
            "provider": "codex",
            "five_hour": "unknown",
            "weekly": "unknown",
            "observed_at": None,
            "plan": None,
        }


def claude_cache_path() -> Path:
    return util.state_home() / "limits" / "claude.json"


def claude_limits() -> dict[str, Any]:
    """Lee los límites reales de Claude desde la caché ~/.hefesto/limits/claude.json."""
    try:
        cache_path = claude_cache_path()
        data = None
        try:
            loaded = util.read_json(cache_path, default=None)
            if isinstance(loaded, dict):
                data = loaded
        except Exception:
            data = None

        if not isinstance(data, dict):
            return {
                "provider": "claude",
                "five_hour": "unknown",
                "weekly": "unknown",
                "observed_at": None,
                "plan": None,
            }

        five_hour = _parse_window(data.get("five_hour"))
        weekly = _parse_window(data.get("weekly") or data.get("seven_day"))
        return {
            "provider": "claude",
            "five_hour": five_hour,
            "weekly": weekly,
            "observed_at": data.get("observed_at"),
            "plan": data.get("plan"),
        }
    except Exception:
        return {
            "provider": "claude",
            "five_hour": "unknown",
            "weekly": "unknown",
            "observed_at": None,
            "plan": None,
        }


def gemini_limits() -> dict[str, Any]:
    """Gemini no expone límites de cuota legibles."""
    return {
        "provider": "gemini",
        "five_hour": "unknown",
        "weekly": "unknown",
        "observed_at": None,
        "plan": None,
    }


def all_limits() -> list[dict[str, Any]]:
    """Límites de claude, codex y gemini en ese orden."""
    try:
        return [claude_limits(), codex_limits(), gemini_limits()]
    except Exception:
        return [
            {"provider": "claude", "five_hour": "unknown", "weekly": "unknown", "observed_at": None, "plan": None},
            {"provider": "codex", "five_hour": "unknown", "weekly": "unknown", "observed_at": None, "plan": None},
            {"provider": "gemini", "five_hour": "unknown", "weekly": "unknown", "observed_at": None, "plan": None},
        ]


def near_limit(provider: str, threshold: float) -> str | None:
    """Devuelve un motivo si alguna ventana conocida y no reiniciada tiene used_percent >= threshold * 100."""
    try:
        if provider == "codex":
            info = codex_limits()
        elif provider == "claude":
            info = claude_limits()
        elif provider == "gemini":
            info = gemini_limits()
        else:
            return None

        if not isinstance(info, dict):
            return None

        try:
            target = float(threshold) * 100.0
        except (ValueError, TypeError):
            return None
        if not math.isfinite(target):
            return None

        fh = info.get("five_hour")
        if isinstance(fh, dict):
            used = fh.get("used_percent")
            if used is not None:
                try:
                    used_f = float(used)
                except (ValueError, TypeError):
                    used_f = None
                if used_f is not None and math.isfinite(used_f) and used_f >= target:
                    resets_at = fh.get("resets_at")
                    expired = False
                    if resets_at:
                        try:
                            cleaned = str(resets_at).replace("Z", "+00:00")
                            rdt = dt.datetime.fromisoformat(cleaned)
                            if rdt.tzinfo is None:
                                rdt = rdt.replace(tzinfo=dt.timezone.utc)
                            if rdt <= dt.datetime.now(dt.timezone.utc):
                                expired = True
                        except Exception:
                            pass
                    if not expired:
                        pct = int(round(used_f))
                        return f"cerca del límite (5 h {pct} %)"

        wk = info.get("weekly")
        if isinstance(wk, dict):
            used = wk.get("used_percent")
            if used is not None:
                try:
                    used_f = float(used)
                except (ValueError, TypeError):
                    used_f = None
                if used_f is not None and math.isfinite(used_f) and used_f >= target:
                    resets_at = wk.get("resets_at")
                    expired = False
                    if resets_at:
                        try:
                            cleaned = str(resets_at).replace("Z", "+00:00")
                            rdt = dt.datetime.fromisoformat(cleaned)
                            if rdt.tzinfo is None:
                                rdt = rdt.replace(tzinfo=dt.timezone.utc)
                            if rdt <= dt.datetime.now(dt.timezone.utc):
                                expired = True
                        except Exception:
                            pass
                    if not expired:
                        pct = int(round(used_f))
                        return f"cerca del límite (semanal {pct} %)"

        return None
    except Exception:
        return None


def update_claude_cache(raw_limits: dict[str, Any]) -> None:
    """Actualiza la caché de límites de Claude de forma atómica."""
    try:
        cache_path = claude_cache_path()
        existing: dict[str, Any] = {}
        try:
            loaded = util.read_json(cache_path, default=None)
            if isinstance(loaded, dict):
                existing = loaded
        except Exception:
            existing = {}

        if not isinstance(raw_limits, dict):
            raw_limits = {}

        fh_raw = raw_limits.get("five_hour")
        wk_raw = raw_limits.get("seven_day") or raw_limits.get("weekly")

        fh_parsed = _parse_window(fh_raw)
        wk_parsed = _parse_window(wk_raw)

        five_hour = fh_parsed if fh_parsed != "unknown" else existing.get("five_hour", "unknown")
        weekly = wk_parsed if wk_parsed != "unknown" else existing.get("weekly", "unknown")

        plan = raw_limits.get("plan_type") or raw_limits.get("plan") or existing.get("plan")

        record = {
            "provider": "claude",
            "five_hour": five_hour,
            "weekly": weekly,
            "observed_at": util.now_iso(),
            "plan": plan,
        }
        util.atomic_write_json(cache_path, record)
    except Exception:
        pass


def current_task_summary(cwd: str | None) -> str | None:
    """Identificador y rol de la tarea en curso si cwd está en un proyecto Hefesto."""
    if not cwd:
        return None
    try:
        from hefesto import project, state

        proj = project.find_project(Path(cwd))
        st = state.load(proj)
        if st.get("status") == "IN_PROGRESS" and st.get("current_task_id"):
            task_id = st["current_task_id"]
            role = st.get("active_role")
            return f"{task_id} ({role})" if role else str(task_id)
    except Exception:
        return None
    return None


def handle_statusline(raw_stdin: str) -> str:
    """Procesa el JSON de Claude Code y genera la línea de estado."""
    cwd: str | None = None
    try:
        data = json.loads(raw_stdin) if raw_stdin and raw_stdin.strip() else {}
    except Exception:
        data = {}

    if isinstance(data, dict):
        cwd = data.get("cwd")
        raw_limits = data.get("rate_limits")
        if isinstance(raw_limits, dict):
            update_claude_cache(raw_limits)

    parts: list[str] = ["hefesto"]
    task_info = current_task_summary(cwd)
    if task_info:
        parts.append(task_info)

    claude_info = claude_limits()
    fh = claude_info.get("five_hour")
    wk = claude_info.get("weekly")
    if isinstance(fh, dict) and fh.get("used_percent") is not None:
        parts.append(f"5h {int(round(float(fh['used_percent'])))}%")
    if isinstance(wk, dict) and wk.get("used_percent") is not None:
        parts.append(f"7d {int(round(float(wk['used_percent'])))}%")

    return " · ".join(parts)


def _format_local_time(iso_str: str | None) -> str:
    if not iso_str:
        return "-"
    try:
        cleaned = iso_str.replace("Z", "+00:00")
        dt_val = dt.datetime.fromisoformat(cleaned)
        if dt_val.tzinfo is None:
            dt_val = dt_val.replace(tzinfo=dt.timezone.utc)
        local_dt = dt_val.astimezone()
        return local_dt.strftime("%Y-%m-%d %H:%M")
    except Exception:
        return str(iso_str)


def _format_age(iso_str: str | None) -> str:
    if not iso_str:
        return "-"
    try:
        cleaned = iso_str.replace("Z", "+00:00")
        dt_val = dt.datetime.fromisoformat(cleaned)
        if dt_val.tzinfo is None:
            dt_val = dt_val.replace(tzinfo=dt.timezone.utc)
        diff = dt.datetime.now(dt.timezone.utc) - dt_val
        secs = int(diff.total_seconds())
        if secs < 0:
            return "ahora"
        if secs < 60:
            return f"hace {secs}s"
        if secs < 3600:
            return f"hace {secs // 60}m"
        if secs < 86400:
            return f"hace {secs // 3600}h"
        return f"hace {secs // 86400}d"
    except Exception:
        return "-"


def render_table(entries: list[dict[str, Any]] | None = None) -> str:
    """Renderiza la tabla de límites para `hefesto limits`."""
    if entries is None:
        entries = all_limits()
    headers = ["proveedor", "5 h", "reinicio (5 h)", "semanal", "reinicio (semanal)", "antigüedad"]
    rows: list[list[str]] = [headers]
    for e in entries:
        prov = str(e.get("provider", ""))
        fh = e.get("five_hour")
        if isinstance(fh, dict):
            fh_used = f"{fh.get('used_percent', 0):.0f}%"
            fh_reset = _format_local_time(fh.get("resets_at"))
        else:
            fh_used = "unknown"
            fh_reset = "-"

        wk = e.get("weekly")
        if isinstance(wk, dict):
            wk_used = f"{wk.get('used_percent', 0):.0f}%"
            wk_reset = _format_local_time(wk.get("resets_at"))
        else:
            wk_used = "unknown"
            wk_reset = "-"

        age = _format_age(e.get("observed_at"))
        rows.append([prov, fh_used, fh_reset, wk_used, wk_reset, age])

    col_widths = [max(len(row[i]) for row in rows) for i in range(len(headers))]
    lines = ["  ".join(cell.ljust(col_widths[i]) for i, cell in enumerate(row)) for row in rows]
    return "\n".join(lines)


def limits_summary(provider: str) -> str:
    """Resumen de límites para hefesto doctor."""
    info = (
        claude_limits()
        if provider == "claude"
        else codex_limits()
        if provider == "codex"
        else gemini_limits()
        if provider == "gemini"
        else None
    )
    if not info:
        return "sin datos"
    fh = info.get("five_hour")
    wk = info.get("weekly")
    fh_str = f"{fh['used_percent']:.0f}%" if isinstance(fh, dict) else "unknown"
    wk_str = f"{wk['used_percent']:.0f}%" if isinstance(wk, dict) else "unknown"
    return f"5 h: {fh_str}, semanal: {wk_str}"
