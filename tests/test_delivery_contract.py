"""Contratos de la Fase 20: entrega autónoma por proyecto (TASK-2001).

Hefesto lleva una fase cerrada hasta un PR y, si el proyecto declara un destino, hasta su despliegue, sin tocar la
rama principal y solo con autorizaciones que una persona ha dado antes y que quedan persistidas.

API que fijan estos tests (todo en `hefesto/delivery.py` salvo donde se indique):

Destino de despliegue del proyecto
- `TARGET_FILE == ".hefesto/deploy.toml"`; sección `[target]` con `name`, `environment` (obligatorios),
  `command` (lista no vacía de cadenas; obligatoria), `healthcheck` y `rollback` (listas, opcionales),
  `url` (opcional), `idempotent` (bool, por defecto false) y `timeout_seconds` (int, por defecto 600).
- `DeployTarget` (dataclass inmutable) con esos campos; las listas se exponen como tuplas.
- `load_target(project) -> DeployTarget | None`: None sin fichero. HefestoError (EXIT_INPUT) si `command` es una
  cadena (nunca shell), si está vacía, si falta `environment` o si hay claves desconocidas. El destino es del
  proyecto: nunca se lee de la configuración global ni de otro proyecto.
- `target_fingerprint(target) -> str`: sha256 hex (64) estable; cambia si cambia cualquier campo.

Puertas de autorización (persistidas)
- `ACTIONS == ("pr", "deploy")`.
- `authorizations_path(project) == project / ".hefesto" / "delivery.json"`; está en `guard.CONTROL_FILES`
  (ningún agente puede editarlo).
- `authorize(project, action, yes=False) -> dict`: acción desconocida → HefestoError (EXIT_INPUT). Con
  `HEFESTO_ROLE` definido siempre se niega (EXIT_BLOCKED), también con `yes`. Sin `yes` exige TTY
  (`util.require_tty`). `deploy` exige destino y guarda su `fingerprint`; la entrada lleva `approved_at`.
- `is_authorized(project, action) -> bool`: la de `deploy` caduca si la huella del destino actual no coincide.
- `revoke(project, action) -> bool`: True si había autorización.

Cierre de fase
- `phase_closure(project, phase=None) -> Closure` (slug, branch, ready, pending: list[str]): `ready` solo con
  todas las tareas de la fase en `[x]`, con evidencia, y sin tarea en curso en esa rama.

PR idempotente
- `ensure_pr(project, phase=None) -> PrResult` (number: int, url: str, created: bool). Se niega (EXIT_BLOCKED) sin
  ejecutar nada si la fase no está cerrada o falta la autorización `pr`. Consulta primero
  `gh pr list --head <rama> --state open --json number,url`; si ya hay uno lo devuelve con `created=False` y
  no crea otro. Si no, `git push -u origin <rama>` (nunca `--force`) y `gh pr create --base <principal>
  --head <rama> …`. Siempre `subprocess.run` con lista de argumentos y sin shell. Nunca integra en la principal.

Despliegue
- `deploy(project, phase=None) -> DeployResult` (ok: bool, exit_code: int, status: str). Se niega (HefestoError,
  EXIT_BLOCKED) sin ejecutar nada si la fase no está cerrada, no hay destino o falta la autorización `deploy`
  vigente. Ejecuta `command` con `cwd=project`, sin shell y con `timeout_seconds`; después `healthcheck` (si hay).
  Si el comando o el healthcheck fallan, ejecuta `rollback` (si hay), `ok=False`, `status="failed"` y llama a
  `notify.event(project, "deploy", …)`. Cada paso se añade a `.hefesto/logs/deliveries.jsonl` (`phase`, `step`,
  `status`, `exit_code`, `at`, `fingerprint`) con la salida redactada (`guard.redact`).

Recuperación
- `record_path(project, slug) == project / ".hefesto" / "delivery" / f"{slug}.json"`: `{"phase", "branch",
  "steps": {"pr": {...}, "deploy": {...}}}`; cada paso con `status` en `pending|running|done|failed|skipped|
  interrupted` y `deploy.attempts`. El despliegue se marca `running` antes de lanzar el comando.
- `deliver(project, phase=None) -> DeliveryReport` (code: int, steps: dict[str, str]): cierre → pr → deploy,
  saltando los pasos `done`. Sin destino, `deploy` queda `skipped` y `code == 0`. Sin autorización `pr` se para
  en `pr` (code EXIT_BLOCKED) sin intentar el despliegue.
- Un `deploy` que quedó `running` (proceso muerto) se reintenta solo si el destino es `idempotent`; si no, pasa a
  `interrupted`, `deliver` sale con EXIT_BLOCKED sin ejecutar nada, y solo una nueva `authorize(…, "deploy")`
  humana lo rearma.
- Un `deploy` fallido se reintenta en el siguiente `deliver` hasta `[delivery] max_deploy_attempts` (por
  defecto 2); alcanzado el tope, EXIT_BLOCKED sin ejecutar.
- `authorize(project, "deploy", …)` rearma el paso: si estaba `interrupted` o `failed` vuelve a `pending` y
  reinicia `attempts` a 0 en el registro de cada fase abierta. Un `deploy` ya `done` no se repite salvo rearme.

CLI (`hefesto/cli.py`)
- `hefesto deliver run [FASE]`, `hefesto deliver status [FASE]`, `hefesto deliver authorize pr|deploy [--yes]`,
  `hefesto deliver revoke pr|deploy`.

Solo `ContractDefinitionTests` pasa antes de la implementación; el resto falla por `hefesto.delivery` ausente.
"""

from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any
from unittest import mock

from hefesto import guard, state
from hefesto.util import EXIT_BLOCKED, EXIT_INPUT, HefestoError, read_jsonl
from tests.helpers import HefestoTestCase, git

ROADMAP_ONE = """# ROADMAP

## Fase 1 — Entrega
- [ ] TASK-001: Saludo entregable
  - verify: `true`
  - files: src/
  - role: runner
  - size: S
"""

ROADMAP_TWO = ROADMAP_ONE + """- [ ] TASK-002: Segunda pieza
  - verify: `true`
  - files: src/
  - role: runner
  - size: S
"""

DEPLOY_SCRIPT = """import pathlib, sys
marker = pathlib.Path(sys.argv[1])
marker.parent.mkdir(parents=True, exist_ok=True)
with marker.open("a", encoding="utf-8") as fh:
    fh.write("deploy\\n")
print("token=" + "sk-" + "ant-" + "api03-" + "ZYXWVUTSRQPONMLKJIHGFEDCBA9876543210")
sys.exit(int(sys.argv[2]) if len(sys.argv) > 2 else 0)
"""


def _delivery() -> Any:
    from hefesto import delivery  # import diferido: el contrato compila sin la implementación

    return delivery


class GhFake:
    """Sustituye `subprocess.run` solo para `gh`; el resto (git real, scripts de despliegue) pasa tal cual."""

    def __init__(self, existing: list[dict[str, Any]] | None = None) -> None:
        self.prs: list[dict[str, Any]] = list(existing or [])
        self.calls: list[tuple[list[str], dict[str, Any]]] = []
        self.real = subprocess.run

    def __call__(self, cmd: Any, *args: Any, **kwargs: Any) -> Any:
        argv = [str(x) for x in cmd] if isinstance(cmd, (list, tuple)) else [str(cmd)]
        self.calls.append((argv, kwargs))
        if Path(argv[0]).stem.lower() != "gh":
            return self.real(cmd, *args, **kwargs)
        text = bool(kwargs.get("text") or kwargs.get("encoding"))
        if "list" in argv:
            head = argv[argv.index("--head") + 1] if "--head" in argv else None
            found = [{"number": p["number"], "url": p["url"]} for p in self.prs if head in (None, p["head"])]
            out = json.dumps(found)
            return subprocess.CompletedProcess(argv, 0, out if text else out.encode(), "" if text else b"")
        if "create" in argv:
            number = len(self.prs) + 1
            head = argv[argv.index("--head") + 1]
            url = f"https://github.invalid/acme/proyecto/pull/{number}"
            self.prs.append({"number": number, "url": url, "head": head})
            return subprocess.CompletedProcess(argv, 0, url + "\n" if text else (url + "\n").encode(),
                                               "" if text else b"")
        return subprocess.CompletedProcess(argv, 1, "" if text else b"", "no soportado" if text else b"")

    def gh_calls(self, verb: str | None = None) -> list[list[str]]:
        found = [argv for argv, _ in self.calls if Path(argv[0]).stem.lower() == "gh"]
        return [a for a in found if verb is None or verb in a]

    def git_calls(self, verb: str) -> list[list[str]]:
        return [argv for argv, _ in self.calls if Path(argv[0]).stem.lower() == "git" and verb in argv]


class DeliveryCase(HefestoTestCase):
    roadmap_text = ROADMAP_ONE

    def setUp(self) -> None:
        super().setUp()
        self.init_project()
        (self.project / ".hefesto" / "ROADMAP.md").write_text(self.roadmap_text, encoding="utf-8")
        git(self.project, "add", "-A")
        git(self.project, "commit", "-q", "-m", "roadmap")
        self.origin = self.tmp / "origin.git"
        git(self.tmp, "init", "-q", "--bare", str(self.origin))
        git(self.project, "remote", "add", "origin", str(self.origin))
        self.main_head = git(self.project, "rev-parse", "HEAD")
        self.marker = self.tmp / "despliegues" / "marker.txt"
        self.script = self.tmp / "deploy_script.py"
        self.script.write_text(DEPLOY_SCRIPT, encoding="utf-8")

    def close_task(self, task_id: str = "TASK-001") -> None:
        code, out, err = self.cli("start", task_id)
        self.assertEqual(code, 0, out + err)
        code, out, err = self.cli("done", task_id)
        self.assertEqual(code, 0, out + err)

    def slug(self) -> str:
        phases = state.load(self.project).get("phases") or {}
        self.assertEqual(len(phases), 1, phases)
        return next(iter(phases))

    def branch(self) -> str:
        return (state.load(self.project).get("phases") or {})[self.slug()]["branch"]

    def write_target(self, exit_code: int = 0, **extra: Any) -> Path:
        command = [sys.executable, str(self.script), str(self.marker), str(exit_code)]
        lines = ["[target]", 'name = "staging"', 'environment = "staging"', f"command = {json.dumps(command)}"]
        for key, value in extra.items():
            lines.append(f"{key} = {json.dumps(value)}")
        path = self.project / ".hefesto" / "deploy.toml"
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return path

    def deploys(self) -> int:
        if not self.marker.exists():
            return 0
        return self.marker.read_text(encoding="utf-8").count("deploy")

    def record(self) -> dict[str, Any]:
        return json.loads(_delivery().record_path(self.project, self.slug()).read_text(encoding="utf-8"))


# --- Definición del contrato (pasa antes de implementar) --------------------------------------------------------


class ContractDefinitionTests(HefestoTestCase):
    REQUIRED_CLASSES = {
        "TargetTests": 5, "AuthorizationTests": 6, "ClosureTests": 3, "PullRequestTests": 5,
        "DeployTests": 6, "RecoveryTests": 6, "CliTests": 3,
    }
    API = ("TARGET_FILE", "DeployTarget", "load_target", "target_fingerprint", "ACTIONS", "authorizations_path",
           "authorize", "is_authorized", "revoke", "phase_closure", "ensure_pr", "PrResult", "deploy",
           "DeployResult", "record_path", "deliver", "DeliveryReport", "max_deploy_attempts", "deliver run",
           "deliver status", "deliver authorize", "deliver revoke")

    def tree(self) -> ast.Module:
        return ast.parse(Path(__file__).read_text(encoding="utf-8"))

    def test_docstring_names_the_whole_api(self) -> None:
        doc = ast.get_docstring(self.tree()) or ""
        for name in self.API:
            with self.subTest(name=name):
                self.assertIn(name, doc)

    def test_every_behaviour_has_its_tests(self) -> None:
        classes = {n.name: n for n in self.tree().body if isinstance(n, ast.ClassDef)}
        for name, minimum in self.REQUIRED_CLASSES.items():
            with self.subTest(cls=name):
                self.assertIn(name, classes)
                tests = [f for f in classes[name].body if isinstance(f, ast.FunctionDef)
                         and f.name.startswith("test_")]
                self.assertGreaterEqual(len(tests), minimum)

    def test_implementation_is_imported_lazily(self) -> None:
        # Si se importara arriba, el módulo no compilaría sin la implementación y nada fallaría «por el motivo
        # correcto»: cada test de comportamiento debe fallar con ModuleNotFoundError de `hefesto.delivery`.
        for node in self.tree().body:
            if isinstance(node, ast.ImportFrom):
                self.assertNotEqual(node.module, "hefesto.delivery")
                if node.module == "hefesto":
                    self.assertNotIn("delivery", [a.name for a in node.names])
            if isinstance(node, ast.Import):
                self.assertNotIn("hefesto.delivery", [a.name for a in node.names])

    def test_contract_is_offline_and_never_uses_a_shell(self) -> None:
        source = Path(__file__).read_text(encoding="utf-8")
        self.assertNotIn("shell=" + "True", source)
        self.assertNotIn("github" + ".com", source)
        self.assertNotIn("url" + "lib", source)

    def test_fixture_scripts_are_valid_python(self) -> None:
        compile(DEPLOY_SCRIPT, "deploy_script.py", "exec")


# --- Destino de despliegue del proyecto -------------------------------------------------------------------------


class TargetTests(DeliveryCase):
    def test_no_file_means_no_target(self) -> None:
        self.assertEqual(_delivery().TARGET_FILE, ".hefesto/deploy.toml")
        self.assertIsNone(_delivery().load_target(self.project))

    def test_loads_the_project_target(self) -> None:
        self.write_target(healthcheck=[sys.executable, "-c", "print('ok')"], url="https://staging.invalid",
                          idempotent=True)
        target = _delivery().load_target(self.project)
        self.assertEqual(target.name, "staging")
        self.assertEqual(target.environment, "staging")
        self.assertIsInstance(target.command, tuple)
        self.assertEqual(target.command[0], sys.executable)
        self.assertEqual(target.healthcheck[0], sys.executable)
        self.assertEqual(target.rollback, ())
        self.assertEqual(target.url, "https://staging.invalid")
        self.assertTrue(target.idempotent)
        self.assertEqual(target.timeout_seconds, 600)

    def test_invalid_targets_are_rejected(self) -> None:
        path = self.project / ".hefesto" / "deploy.toml"
        cases = {
            "comando como cadena (shell)": '[target]\nname = "x"\nenvironment = "prod"\ncommand = "make deploy"\n',
            "comando vacío": '[target]\nname = "x"\nenvironment = "prod"\ncommand = []\n',
            "sin environment": '[target]\nname = "x"\ncommand = ["make"]\n',
            "clave desconocida": '[target]\nname = "x"\nenvironment = "prod"\ncommand = ["make"]\nshell = true\n',
        }
        for label, text in cases.items():
            with self.subTest(caso=label):
                path.write_text(text, encoding="utf-8")
                with self.assertRaises(HefestoError) as ctx:
                    _delivery().load_target(self.project)
                self.assertEqual(ctx.exception.code, EXIT_INPUT)

    def test_target_belongs_to_the_project(self) -> None:
        other = self.tmp / "otro"
        (other / ".hefesto").mkdir(parents=True)
        self.write_target()
        self.assertIsNone(_delivery().load_target(other))
        home = Path(os.environ["HEFESTO_HOME"])
        home.mkdir(parents=True, exist_ok=True)
        (home / "deploy.toml").write_text('[target]\nname = "g"\nenvironment = "prod"\ncommand = ["x"]\n',
                                          encoding="utf-8")
        self.assertIsNone(_delivery().load_target(other))

    def test_fingerprint_is_stable_and_tracks_changes(self) -> None:
        d = _delivery()
        self.write_target()
        first = d.target_fingerprint(d.load_target(self.project))
        self.assertRegex(first, r"^[0-9a-f]{64}$")
        self.assertEqual(first, d.target_fingerprint(d.load_target(self.project)))
        self.write_target(exit_code=1)
        self.assertNotEqual(first, d.target_fingerprint(d.load_target(self.project)))


# --- Puertas de autorización ------------------------------------------------------------------------------------


class AuthorizationTests(DeliveryCase):
    def test_actions_and_path(self) -> None:
        d = _delivery()
        self.assertEqual(d.ACTIONS, ("pr", "deploy"))
        self.assertEqual(d.authorizations_path(self.project), self.project / ".hefesto" / "delivery.json")

    def test_authorization_is_persisted(self) -> None:
        d = _delivery()
        self.assertFalse(d.is_authorized(self.project, "pr"))
        entry = d.authorize(self.project, "pr", yes=True)
        self.assertIn("approved_at", entry)
        self.assertTrue(d.authorizations_path(self.project).exists())
        self.assertTrue(d.is_authorized(self.project, "pr"))
        self.assertFalse(d.is_authorized(self.project, "deploy"))
        self.assertTrue(d.revoke(self.project, "pr"))
        self.assertFalse(d.is_authorized(self.project, "pr"))
        self.assertFalse(d.revoke(self.project, "pr"))

    def test_agents_can_never_authorize(self) -> None:
        d = _delivery()
        with mock.patch.dict(os.environ, {"HEFESTO_ROLE": "runner"}):
            with self.assertRaises(HefestoError) as ctx:
                d.authorize(self.project, "pr", yes=True)
        self.assertEqual(ctx.exception.code, EXIT_BLOCKED)
        self.assertFalse(d.is_authorized(self.project, "pr"))

    def test_without_yes_requires_a_human_terminal(self) -> None:
        d = _delivery()
        with mock.patch("hefesto.util.is_tty", return_value=False), self.assertRaises(HefestoError):
            d.authorize(self.project, "pr")
        self.assertFalse(d.is_authorized(self.project, "pr"))
        with self.assertRaises(HefestoError) as ctx:
            d.authorize(self.project, "merge", yes=True)
        self.assertEqual(ctx.exception.code, EXIT_INPUT)

    def test_deploy_authorization_is_bound_to_the_target(self) -> None:
        d = _delivery()
        with self.assertRaises(HefestoError):
            d.authorize(self.project, "deploy", yes=True)  # sin destino no hay nada que autorizar
        self.write_target()
        entry = d.authorize(self.project, "deploy", yes=True)
        self.assertEqual(entry["fingerprint"], d.target_fingerprint(d.load_target(self.project)))
        self.assertTrue(d.is_authorized(self.project, "deploy"))
        self.write_target(exit_code=1)  # alguien cambia el destino: la autorización ya no vale
        self.assertFalse(d.is_authorized(self.project, "deploy"))

    def test_authorizations_file_is_protected_from_agents(self) -> None:
        self.assertIn(".hefesto/delivery.json", guard.CONTROL_FILES)
        reason = guard.check_tool_use({"tool_name": "Edit",
                                       "tool_input": {"file_path": "/repo/.hefesto/delivery.json"}})
        self.assertIsNotNone(reason)


# --- Cierre de fase ---------------------------------------------------------------------------------------------


class ClosureTests(DeliveryCase):
    roadmap_text = ROADMAP_TWO

    def test_phase_with_pending_tasks_is_not_closed(self) -> None:
        self.close_task("TASK-001")
        closure = _delivery().phase_closure(self.project)
        self.assertFalse(closure.ready)
        self.assertEqual(closure.pending, ["TASK-002"])
        self.assertEqual(closure.slug, self.slug())
        self.assertEqual(closure.branch, self.branch())

    def test_phase_with_task_in_progress_is_not_closed(self) -> None:
        self.close_task("TASK-001")
        self.cli("start", "TASK-002")
        self.assertFalse(_delivery().phase_closure(self.project).ready)

    def test_complete_phase_is_closed(self) -> None:
        self.close_task("TASK-001")
        self.close_task("TASK-002")
        closure = _delivery().phase_closure(self.project)
        self.assertTrue(closure.ready, closure)
        self.assertEqual(closure.pending, [])


# --- PR idempotente ---------------------------------------------------------------------------------------------


class PullRequestTests(DeliveryCase):
    def test_refuses_without_authorization_and_runs_nothing(self) -> None:
        self.close_task()
        fake = GhFake()
        with mock.patch("subprocess.run", side_effect=fake), self.assertRaises(HefestoError) as ctx:
            _delivery().ensure_pr(self.project)
        self.assertEqual(ctx.exception.code, EXIT_BLOCKED)
        self.assertEqual(fake.gh_calls(), [])
        self.assertEqual(fake.git_calls("push"), [])

    def test_refuses_an_open_phase(self) -> None:
        d = _delivery()
        d.authorize(self.project, "pr", yes=True)
        self.cli("start", "TASK-001")
        fake = GhFake()
        with mock.patch("subprocess.run", side_effect=fake), self.assertRaises(HefestoError) as ctx:
            d.ensure_pr(self.project)
        self.assertEqual(ctx.exception.code, EXIT_BLOCKED)
        self.assertEqual(fake.gh_calls("create"), [])

    def test_creates_one_pr_and_pushes_without_force(self) -> None:
        d = _delivery()
        self.close_task()
        d.authorize(self.project, "pr", yes=True)
        fake = GhFake()
        with mock.patch("subprocess.run", side_effect=fake):
            result = d.ensure_pr(self.project)
        self.assertTrue(result.created)
        self.assertEqual(result.number, 1)
        self.assertIn("/pull/1", result.url)
        creates = fake.gh_calls("create")
        self.assertEqual(len(creates), 1)
        self.assertEqual(creates[0][creates[0].index("--head") + 1], self.branch())
        self.assertIn("--base", creates[0])
        pushes = fake.git_calls("push")
        self.assertTrue(pushes)
        for argv in pushes:
            self.assertFalse({"--force", "-f", "--force-with-lease"} & set(argv), argv)
        for argv, kwargs in fake.calls:
            self.assertNotEqual(kwargs.get("shell"), True, argv)
        self.assertEqual(git(self.origin, "rev-parse", self.branch()), git(self.project, "rev-parse", self.branch()))

    def test_second_call_reuses_the_open_pr(self) -> None:
        d = _delivery()
        self.close_task()
        d.authorize(self.project, "pr", yes=True)
        fake = GhFake()
        with mock.patch("subprocess.run", side_effect=fake):
            first = d.ensure_pr(self.project)
            second = d.ensure_pr(self.project)
        self.assertFalse(second.created)
        self.assertEqual(second.number, first.number)
        self.assertEqual(len(fake.gh_calls("create")), 1)

    def test_pr_opened_elsewhere_is_adopted_and_main_is_untouched(self) -> None:
        d = _delivery()
        self.close_task()
        d.authorize(self.project, "pr", yes=True)
        fake = GhFake([{"number": 7, "url": "https://github.invalid/acme/proyecto/pull/7", "head": self.branch()}])
        with mock.patch("subprocess.run", side_effect=fake):
            result = d.ensure_pr(self.project)
        self.assertEqual((result.number, result.created), (7, False))
        self.assertEqual(fake.gh_calls("create"), [])
        self.assertEqual(git(self.project, "rev-parse", "HEAD"), self.main_head)  # nunca integra en la principal


# --- Despliegue -------------------------------------------------------------------------------------------------


class DeployTests(DeliveryCase):
    def ready(self, exit_code: int = 0, authorize: bool = True, **extra: Any) -> None:
        self.close_task()
        self.write_target(exit_code=exit_code, **extra)
        if authorize:
            _delivery().authorize(self.project, "deploy", yes=True)

    def test_refuses_without_authorization(self) -> None:
        self.ready(authorize=False)
        with self.assertRaises(HefestoError) as ctx:
            _delivery().deploy(self.project)
        self.assertEqual(ctx.exception.code, EXIT_BLOCKED)
        self.assertEqual(self.deploys(), 0)

    def test_refuses_without_target_or_with_open_phase(self) -> None:
        d = _delivery()
        self.cli("start", "TASK-001")
        self.write_target()
        d.authorize(self.project, "deploy", yes=True)
        with self.assertRaises(HefestoError) as ctx:  # fase abierta
            d.deploy(self.project)
        self.assertEqual(ctx.exception.code, EXIT_BLOCKED)
        self.cli("done", "TASK-001")
        (self.project / ".hefesto" / "deploy.toml").unlink()
        with self.assertRaises(HefestoError) as ctx:  # sin destino
            d.deploy(self.project)
        self.assertEqual(ctx.exception.code, EXIT_BLOCKED)
        self.assertEqual(self.deploys(), 0)

    def test_refuses_when_target_changed_after_authorization(self) -> None:
        self.ready()
        self.write_target(exit_code=0, timeout_seconds=30)
        with self.assertRaises(HefestoError) as ctx:
            _delivery().deploy(self.project)
        self.assertEqual(ctx.exception.code, EXIT_BLOCKED)
        self.assertEqual(self.deploys(), 0)

    def test_runs_the_target_without_shell_and_logs_redacted(self) -> None:
        self.ready(healthcheck=[sys.executable, "-c", "print('sano')"])
        real = subprocess.run
        seen: list[tuple[Any, dict[str, Any]]] = []

        def spy(cmd: Any, *a: Any, **kw: Any) -> Any:
            seen.append((cmd, kw))
            return real(cmd, *a, **kw)

        with mock.patch("subprocess.run", side_effect=spy):
            result = _delivery().deploy(self.project)
        self.assertTrue(result.ok)
        self.assertEqual((result.exit_code, result.status), (0, "done"))
        self.assertEqual(self.deploys(), 1)
        target_calls = [(c, kw) for c, kw in seen if isinstance(c, (list, tuple)) and str(self.script) in map(str, c)]
        self.assertEqual(len(target_calls), 1)
        self.assertNotEqual(target_calls[0][1].get("shell"), True)
        self.assertEqual(Path(target_calls[0][1]["cwd"]).resolve(), self.project.resolve())
        self.assertTrue(target_calls[0][1].get("timeout"))
        log = self.project / ".hefesto" / "logs" / "deliveries.jsonl"
        rows = read_jsonl(log)
        self.assertTrue(any(r["step"] == "deploy" and r["status"] == "done" for r in rows), rows)
        self.assertTrue(all({"phase", "step", "status", "exit_code", "at", "fingerprint"} <= set(r) for r in rows
                            if r["step"] == "deploy"))
        self.assertNotIn("ZYXWVUTSRQPONMLKJIHGFEDCBA9876543210", log.read_text(encoding="utf-8"))

    def test_failed_deploy_rolls_back_and_notifies(self) -> None:
        rollback_marker = self.tmp / "despliegues" / "rollback.txt"
        self.ready(exit_code=3, rollback=[sys.executable, "-c",
                                          f"open({str(rollback_marker)!r}, 'w').write('rollback')"])
        with mock.patch("hefesto.notify.event", return_value=True) as event:
            result = _delivery().deploy(self.project)
        self.assertFalse(result.ok)
        self.assertEqual((result.exit_code, result.status), (3, "failed"))
        self.assertTrue(rollback_marker.exists())
        self.assertIn("deploy", [c.args[1] for c in event.call_args_list])

    def test_failed_healthcheck_rolls_back(self) -> None:
        rollback_marker = self.tmp / "despliegues" / "rollback.txt"
        self.ready(healthcheck=[sys.executable, "-c", "import sys; sys.exit(1)"],
                   rollback=[sys.executable, "-c", f"open({str(rollback_marker)!r}, 'w').write('rollback')"])
        with mock.patch("hefesto.notify.event", return_value=True):
            result = _delivery().deploy(self.project)
        self.assertFalse(result.ok)
        self.assertEqual(result.status, "failed")
        self.assertEqual(self.deploys(), 1)
        self.assertTrue(rollback_marker.exists())


# --- Recuperación -----------------------------------------------------------------------------------------------


class RecoveryTests(DeliveryCase):
    def prepare(self, exit_code: int = 0, deploy: bool = True, **extra: Any) -> None:
        d = _delivery()
        self.close_task()
        d.authorize(self.project, "pr", yes=True)
        if deploy:
            self.write_target(exit_code=exit_code, **extra)
            d.authorize(self.project, "deploy", yes=True)

    def deliver(self, fake: GhFake) -> Any:
        with mock.patch("subprocess.run", side_effect=fake), \
                mock.patch("hefesto.notify.event", return_value=True):
            return _delivery().deliver(self.project)

    def test_full_delivery_records_every_step_and_is_idempotent(self) -> None:
        self.prepare()
        fake = GhFake()
        first = self.deliver(fake)
        self.assertEqual(first.code, 0)
        self.assertEqual(first.steps, {"pr": "done", "deploy": "done"})
        self.assertEqual(_delivery().record_path(self.project, self.slug()),
                         self.project / ".hefesto" / "delivery" / f"{self.slug()}.json")
        record = self.record()
        self.assertEqual(record["branch"], self.branch())
        self.assertEqual(record["steps"]["pr"]["status"], "done")
        self.assertEqual(record["steps"]["deploy"]["attempts"], 1)
        second = self.deliver(fake)
        self.assertEqual(second.code, 0)
        self.assertEqual(len(fake.gh_calls("create")), 1)
        self.assertEqual(self.deploys(), 1)

    def test_without_target_deploy_is_skipped(self) -> None:
        self.prepare(deploy=False)
        report = self.deliver(GhFake())
        self.assertEqual(report.code, 0)
        self.assertEqual(report.steps, {"pr": "done", "deploy": "skipped"})

    def test_missing_pr_authorization_stops_before_deploy(self) -> None:
        self.prepare()
        _delivery().revoke(self.project, "pr")
        fake = GhFake()
        report = self.deliver(fake)
        self.assertEqual(report.code, EXIT_BLOCKED)
        self.assertNotEqual(report.steps.get("deploy"), "done")
        self.assertEqual(fake.gh_calls("create"), [])
        self.assertEqual(self.deploys(), 0)

    def test_failed_deploy_is_retried_up_to_the_limit(self) -> None:
        self.prepare(exit_code=1)
        fake = GhFake()
        self.assertNotEqual(self.deliver(fake).code, 0)
        self.assertEqual(self.record()["steps"]["deploy"]["status"], "failed")
        self.assertNotEqual(self.deliver(fake).code, 0)       # 2.º intento (tope por defecto: 2)
        self.assertEqual(self.deploys(), 2)
        third = self.deliver(fake)
        self.assertEqual(third.code, EXIT_BLOCKED)
        self.assertEqual(self.deploys(), 2)                    # alcanzado el tope no se ejecuta
        self.assertEqual(len(fake.gh_calls("create")), 1)      # el PR no se repite en ningún reintento

    def _interrupt(self) -> None:
        """Simula un proceso muerto a mitad del despliegue: el paso quedó `running`."""
        path = _delivery().record_path(self.project, self.slug())
        data = json.loads(path.read_text(encoding="utf-8"))
        data["steps"]["deploy"]["status"] = "running"
        path.write_text(json.dumps(data), encoding="utf-8")

    def test_interrupted_non_idempotent_deploy_needs_a_human(self) -> None:
        self.prepare()
        fake = GhFake()
        self.deliver(fake)
        self._interrupt()
        blocked = self.deliver(fake)
        self.assertEqual(blocked.code, EXIT_BLOCKED)
        self.assertEqual(blocked.steps["deploy"], "interrupted")
        self.assertEqual(self.deploys(), 1)
        _delivery().authorize(self.project, "deploy", yes=True)  # el humano lo rearma
        resumed = self.deliver(fake)
        self.assertEqual(resumed.code, 0)
        self.assertEqual(self.deploys(), 2)

    def test_interrupted_idempotent_deploy_is_retried(self) -> None:
        self.prepare(idempotent=True)
        fake = GhFake()
        self.deliver(fake)
        self._interrupt()
        report = self.deliver(fake)
        self.assertEqual(report.code, 0)
        self.assertEqual(report.steps["deploy"], "done")
        self.assertEqual(self.deploys(), 2)


# --- CLI --------------------------------------------------------------------------------------------------------


class CliTests(DeliveryCase):
    def test_authorize_and_revoke(self) -> None:
        code, out, err = self.cli("deliver", "authorize", "pr", "--yes")
        self.assertEqual(code, 0, out + err)
        self.assertTrue(_delivery().is_authorized(self.project, "pr"))
        code, out, err = self.cli("deliver", "revoke", "pr")
        self.assertEqual(code, 0, out + err)
        self.assertFalse(_delivery().is_authorized(self.project, "pr"))

    def test_authorize_refused_for_agents(self) -> None:
        with mock.patch.dict(os.environ, {"HEFESTO_ROLE": "runner"}):
            code, out, err = self.cli("deliver", "authorize", "pr", "--yes")
        self.assertEqual(code, EXIT_BLOCKED, out + err)
        self.assertFalse(_delivery().is_authorized(self.project, "pr"))

    def test_run_and_status(self) -> None:
        self.close_task()
        self.cli("deliver", "authorize", "pr", "--yes")
        fake = GhFake()
        with mock.patch("subprocess.run", side_effect=fake):
            code, out, err = self.cli("deliver", "run")
            self.assertEqual(code, 0, out + err)
            code, out, err = self.cli("deliver", "status")
        self.assertEqual(code, 0, out + err)
        self.assertIn("pr", out)
        self.assertIn("skipped", out)
        self.assertEqual(len(fake.gh_calls("create")), 1)
