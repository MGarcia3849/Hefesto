"""TASK-3805, offline. ACTIONS permanece exacto por contrato; clone es un tipo aparte.

Configuración de terminal: projects root RUTA; set_github_owner(OWNER) guarda github_owner
en projects.toml. Las claves superiores studio/vscode admiten la ruta del lanzador.
El encargo tras clonar genera su propia aprobación plan, nunca se ejecuta texto como shell.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest import mock

from hefesto import telegram, telegram_intent as intent, telegram_ops as ops, workspaces as ws
from hefesto.util import HefestoError
from tests.helpers import HefestoTestCase, tty
from tests.test_telegram import FakeTransport, OWNER, STRANGER, message
from tests.test_telegram_ops import ok_result

URL = 'https://github.com/example/solgrif-solution.git'


class IntentTests(HefestoTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.projects = [ws.Project('manjair', 'Manjair', self.tmp),
                         ws.Project('solgrif', 'Solgrif-Solution', self.tmp)]

    def test_user_examples(self) -> None:
        a = intent.parse('abre el proyecto manjair en android studio y haz estos cambios', self.projects)
        self.assertEqual((a.kind, a.project_id, a.editor, a.instruction),
                         ('open_and_work', 'manjair', 'studio', 'haz estos cambios'))
        b = intent.parse('abre el proyecto solgrif solution en vsc y que lo haga', self.projects)
        self.assertEqual((b.kind, b.project_id, b.editor, b.instruction),
                         ('open_and_work', 'solgrif', 'vscode', 'que lo haga'))
        b = intent.parse('abre el proyecto solgrif solution en vsc y arréglalo', self.projects)
        self.assertEqual((b.kind, b.instruction), ('open_and_work', 'arréglalo'))

    def test_normalization_aliases_and_open(self) -> None:
        for editor in ('vsc', 'vs code', 'visual studio code'):
            found = intent.parse(f'Ábreme el proyecto SOLGRÍF_solution en {editor}', self.projects)
            self.assertEqual((found.kind, found.project_id, found.editor), ('open', 'solgrif', 'vscode'))
        self.assertEqual(intent.parse('abrir el proyecto manjair en studio', self.projects).editor, 'studio')

    def test_exact_before_prefix_and_ambiguity(self) -> None:
        projects = self.projects + [ws.Project('manjair-web', 'Manjair Web', self.tmp)]
        self.assertEqual(intent.parse('abre el proyecto manjair', projects).project_id, 'manjair')
        projects = [ws.Project('one', 'Solgrif Web', self.tmp), ws.Project('two', 'Solgrif App', self.tmp)]
        found = intent.parse('abre el proyecto solgrif', projects)
        self.assertEqual((found.kind, found.options), ('ambiguous', ['one', 'two']))

    def test_typo_work_unknown_and_remote(self) -> None:
        self.assertEqual(intent.parse('abre el proyecto manjairr', self.projects).project_id, 'manjair')
        self.assertEqual(intent.parse('arregla el proyecto manjair', self.projects).kind, 'work')
        self.assertEqual(intent.parse('abre el proyecto inexistente', self.projects).kind, 'unknown')
        self.assertEqual(intent.parse('echo $(calc)', self.projects).kind, 'unknown')
        found = intent.parse('abre el proyecto solgrif solution en studio y cambia el tema', [],
                             [{'name': 'solgrif-solution', 'url': URL}])
        self.assertEqual((found.kind, found.repo_url, found.editor), ('clone_and_work', URL, 'studio'))


class EditorsTests(HefestoTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.folder = self.tmp / 'demo'
        self.folder.mkdir()
        self.project_ws = ws.Project('demo', 'Demo', self.folder)

    def test_desktop_required_and_missing_launcher_is_honest(self) -> None:
        for desktop, launcher in ((False, 'studio'), (True, None)):
            with self.subTest(desktop=desktop), mock.patch.object(ws, 'has_desktop', return_value=desktop), \
                    mock.patch.object(ws, 'find_editor', return_value=launcher), mock.patch.object(ws.subprocess, 'Popen') as popen:
                ok, reply = ws.open_in_editor(self.project_ws, 'studio')
                self.assertFalse(ok)
                self.assertIn('Android Studio', reply)
                popen.assert_not_called()

    def test_studio_open_without_shell_or_close(self) -> None:
        with mock.patch.object(ws, 'has_desktop', return_value=True), \
                mock.patch.object(ws, 'find_editor', return_value='studio64.exe'), \
                mock.patch.object(ws.subprocess, 'Popen') as popen:
            self.assertTrue(ws.open_in_editor(self.project_ws, 'studio')[0])
            popen.assert_called_once_with(['studio64.exe', str(self.folder.resolve())], shell=False)
            popen.side_effect = OSError
            self.assertFalse(ws.open_in_editor(self.project_ws, 'studio')[0])

    def test_detect_or_configure_launcher(self) -> None:
        with mock.patch.object(ws.shutil, 'which', side_effect=[None, None, 'studio']) as which:
            self.assertEqual(ws.find_editor('studio'), 'studio')
            self.assertEqual(which.call_args.args, ('studio',))
        launcher = self.tmp / 'studio64.exe'
        launcher.touch()
        ws.registry_path().parent.mkdir(parents=True, exist_ok=True)
        ws.registry_path().write_text('studio = ' + json.dumps(str(launcher)) + '\n', encoding='utf-8')
        self.assertEqual(ws.find_editor('studio'), str(launcher))

    def test_windows_installer_location(self) -> None:
        launcher = self.tmp / 'Android/Android Studio/bin/studio64.exe'
        launcher.parent.mkdir(parents=True)
        launcher.touch()
        with mock.patch.dict('os.environ', {'ProgramFiles': str(self.tmp)}), \
                mock.patch.object(ws.shutil, 'which', return_value=None):
            self.assertEqual(ws.find_editor('studio'), str(launcher))

    def test_root_cli_tty_and_settings_survive_registry_changes(self) -> None:
        with tty(False), self.assertRaises(HefestoError):
            ws.set_projects_root(self.tmp)
        code, out, err = self.cli('projects', 'root', str(self.tmp), tty_value=True)
        self.assertEqual(code, 0, out + err)
        with tty(True):
            ws.set_github_owner('example')
            ws.add_project('demo', self.folder)
            ws.remove_project('demo')
        self.assertEqual(ws.projects_root(), self.tmp.resolve())
        self.assertEqual(ws._settings()['github_owner'], 'example')

    def test_gh_list_injectable_without_shell(self) -> None:
        with tty(True):
            ws.set_github_owner('example')
        runner = mock.Mock(return_value=subprocess.CompletedProcess([], 0, json.dumps([{'name': 'demo', 'url': URL}])))
        self.assertEqual(ws.github_repos(runner)[0]['name'], 'demo')
        self.assertEqual(runner.call_args.args[0], ['gh', 'repo', 'list', 'example', '--json', 'name,url', '--limit', '200'])
        self.assertIs(runner.call_args.kwargs['shell'], False)
        runner.return_value = subprocess.CompletedProcess([], 1, '')
        with self.assertRaises(HefestoError):
            ws.github_repos(runner)


class CloneCase(HefestoTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.root = self.tmp / 'projects'
        self.root.mkdir()
        with tty(True):
            ws.set_projects_root(self.root)
        self.now = 1000
        self.store = ops.Store(clock=lambda: self.now)

    def create(self, **kw) -> dict:
        return self.store.create_clone(URL, OWNER, OWNER, **kw)

    def approve(self, op: dict) -> dict:
        return self.store.approve(op['id'], OWNER, OWNER)


class CloneTests(CloneCase):
    def test_only_strict_github_https_urls(self) -> None:
        for url in ('https://evil.com/a/b', 'https://user:' + 'clave@github.com/a/b',  # credenciales sintéticas
                    'https://github.com/a/b?x=1', 'https://github.com/a/b#x',
                    'https://github.com/a/../b', 'git@github.com:a/b', '/tmp/repo',
                    'https://github.com/a/b/', 'https://github.com:443/a/b',
                    'https://github.com/a/CON.git', 'https://github.com/a/b;echo'):
            with self.subTest(url=url), self.assertRaises(HefestoError):
                self.store.create_clone(url, OWNER, OWNER)

    def test_missing_root_and_existing_destination(self) -> None:
        with mock.patch.object(ws, '_settings', return_value={}):
            with self.assertRaisesRegex(HefestoError, 'root'):
                self.create()
        (self.root / 'solgrif-solution').mkdir()
        with self.assertRaisesRegex(HefestoError, 'existe'):
            self.create()

    def test_clone_needs_approval_then_registers_once(self) -> None:
        op = self.create()
        with mock.patch.object(ops.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, '')) as run:
            with self.assertRaises(HefestoError):
                self.store.execute(op, mock.Mock())
            run.assert_not_called()
            approved = self.approve(op)
            done = self.store.execute(approved, mock.Mock())
            self.assertEqual(done['status'], 'done', done)
            self.assertEqual(run.call_args.args[0], ['git', 'clone', '--', URL, str(self.root / 'solgrif-solution')])
            self.assertIs(run.call_args.kwargs['shell'], False)
            self.assertEqual(ws.resolve('solgrif-solution').path, self.root / 'solgrif-solution')
            with self.assertRaises(HefestoError):
                self.store.execute(approved, mock.Mock())
            self.assertEqual(run.call_count, 1)

    def test_actor_chat_thread_expiry_and_scope(self) -> None:
        op = self.create()
        for actor, chat, thread in ((STRANGER, OWNER, None), (OWNER, STRANGER, None), (OWNER, OWNER, 10)):
            with self.assertRaises(HefestoError):
                self.store.approve(op['id'], actor, chat, thread)
        self.now += ops.DEFAULT_TTL
        with self.assertRaises(HefestoError):
            self.approve(op)
        self.assertEqual(self.store.get(op['id'])['status'], 'expired')
        op = self.create()
        other = self.tmp / 'other'
        other.mkdir()
        with tty(True):
            ws.set_projects_root(other)
        with self.assertRaises(HefestoError):
            self.approve(op)

    def test_destination_created_after_approval_and_network_failure(self) -> None:
        op = self.approve(self.create())
        (self.root / 'solgrif-solution').mkdir()
        with mock.patch.object(ops.subprocess, 'run') as run:
            self.assertEqual(self.store.execute(op, mock.Mock())['status'], 'failed')
            run.assert_not_called()
        (self.root / 'solgrif-solution').rmdir()
        op = self.approve(self.create())
        with mock.patch.object(ops.subprocess, 'run', side_effect=OSError):
            done = self.store.execute(op, mock.Mock())
        self.assertEqual(done['status'], 'failed')
        self.assertIn('red', done['result']['summary'])
        self.assertEqual(ws.list_projects(), [])

    def test_clone_work_opens_and_proposes_separate_plan(self) -> None:
        op = self.approve(self.create(instruction='arregla el tema', editor='studio'))
        with mock.patch.object(ops.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, '')), \
                mock.patch.object(ws, 'open_in_editor', return_value=(True, 'Abierto')) as opened:
            done = self.store.execute(op, mock.Mock())
        self.assertEqual(done['status'], 'done')
        opened.assert_called_once()
        plans = [o for o in self.store.list() if o['action'] == 'plan']
        self.assertEqual((len(plans), plans[0]['status'], plans[0]['instruction']), (1, 'pending', 'arregla el tema'))
        self.assertIn('/approve', done['result']['summary'])

    def test_registration_refuses_unapproved_operation(self) -> None:
        op = self.create()
        with mock.patch.object(ops.subprocess, 'run') as run, self.assertRaises(HefestoError):
            self.store._execute_clone(op)
        run.assert_not_called()
        project = ws.clone_target(URL)
        project.path.mkdir()
        with self.assertRaises(HefestoError):
            ws._register_clone(project, op, self.store)


class GatewayTests(CloneCase):
    def setUp(self) -> None:
        super().setUp()
        folder = self.tmp / 'manjair'
        folder.mkdir()
        with tty(True):
            self.local = ws.add_project('manjair', folder)
            ws.add_project('solgrif', self.tmp, name='Solgrif-Solution')
        self.executor = mock.Mock(return_value=ok_result())
        self.gateway = telegram.Gateway(telegram.Settings(frozenset({OWNER}), rate_per_minute=100),
                                       FakeTransport(), ops=self.store, executor=self.executor)

    def send(self, text: str) -> str:
        return '\n'.join(self.gateway.handle(message(text)))

    def test_natural_open_direct_and_changes_only_after_approve(self) -> None:
        with mock.patch.object(ws, 'open_in_editor', return_value=(True, 'Android Studio abierto')) as opened:
            reply = self.send('abre el proyecto manjair en android studio y haz estos cambios')
        opened.assert_called_once_with(self.local, 'studio')
        self.assertIn('/approve', reply)
        self.executor.assert_not_called()
        op = self.store.list()[0]
        self.assertEqual(op['instruction'], 'haz estos cambios')
        self.send('/approve ' + op['id'])
        self.executor.assert_called_once()

    def test_second_example_and_plain_open(self) -> None:
        with mock.patch.object(ws, 'open_in_editor', return_value=(True, 'Abierto')) as opened:
            reply = self.send('abre el proyecto solgrif solution en vsc y que lo haga')
            self.assertIn('/approve', reply)
            self.assertEqual(opened.call_args.args[1], 'vscode')
            self.send('abre el proyecto manjair en studio')
        self.assertEqual(len(self.store.list()), 1)
        self.assertEqual(ws.active(OWNER, None).id, 'manjair')

    def test_open_commands_default_and_studio(self) -> None:
        ws.select(OWNER, 'manjair')
        with mock.patch.object(ws, 'open_in_vscode', return_value=(True, 'VS Code')) as vscode, \
                mock.patch.object(ws, 'open_in_editor', return_value=(False, 'Android Studio no encontrado')) as studio:
            self.assertIn('VS Code', self.send('/open'))
            vscode.assert_called_once()
            self.assertIn('no encontrado', self.send('/open studio'))
            studio.assert_called_once_with(self.local, 'studio')
        self.assertEqual(self.store.list(), [])

    def test_unknown_and_ambiguous_do_not_create_plan(self) -> None:
        with tty(True):
            ws.add_project('solgrif-web', self.root, name='Solgrif S Web')
            ws.add_project('solgrif-app', self.root, name='Solgrif S App')
        self.assertIn('Opciones', self.send('abre el proyecto solgrif s'))
        self.assertIn('No encuentro', self.send('abre el proyecto inexistente'))
        self.assertIn('proyecto', self.send('arregla esto'))
        self.assertEqual(self.store.list(), [])

    def test_clone_without_active_project_can_be_approved(self) -> None:
        self.assertIn('/approve', self.send('/clone ' + URL))
        op = self.store.list()[0]
        with mock.patch.object(ops.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, '')):
            self.assertIn('completada', self.send('/approve ' + op['id']))
        self.assertEqual(ws.active(OWNER, None).id, 'solgrif-solution')

    def test_remote_natural_request_uses_gh_only_after_local_lookup(self) -> None:
        with mock.patch.object(ws, 'github_repos', return_value=[{'name': 'remote', 'url': URL}]) as repos:
            with mock.patch.object(ws, 'open_in_editor', return_value=(True, 'Abierto')):
                self.send('abre el proyecto manjair')
            repos.assert_not_called()
            reply = self.send('abre el proyecto remote en studio y cambia el tema')
            self.assertIn('/approve', reply)
        op = self.store.list()[0]
        self.assertEqual((op['action'], op['scope']['editor']), ('clone', 'studio'))
        self.executor.assert_not_called()

    def test_unauthorized_and_forwarded_clone_have_no_effect(self) -> None:
        self.gateway.handle(message('/clone ' + URL, user=STRANGER))
        self.gateway.handle(message('/clone ' + URL, forward_origin={'type': 'user'}))
        self.assertEqual(self.store.list(), [])

    def test_unknown_text_in_bound_topic_keeps_plan_behavior(self) -> None:
        group = -100500
        self.gateway.settings = telegram.Settings(frozenset({OWNER}), frozenset({group}))
        ws.bind(group, 10, 'manjair')
        reply = self.gateway.handle(message('arregla el calendario', chat=group, chat_type='supergroup',
                                            message_thread_id=10))
        self.assertIn('/approve', '\n'.join(reply))
        op = self.store.list()[0]
        self.assertEqual((op['project'], op['thread'], op['instruction']), ('manjair', 10, 'arregla el calendario'))


class GithubOwnerCliTests(HefestoTestCase):
    def test_owner_is_set_from_the_terminal_and_shown(self) -> None:
        code, out, err = self.run_cli("projects", "owner", "usuario-ejemplo", tty_value=True)
        self.assertEqual(code, 0, out + err)
        self.assertEqual(ws.github_owner(), "usuario-ejemplo")
        code, out, _ = self.run_cli("projects", "owner")
        self.assertEqual(code, 0)
        self.assertIn("usuario-ejemplo", out)

    def test_owner_needs_a_terminal_and_a_valid_name(self) -> None:
        code, _, _ = self.run_cli("projects", "owner", "usuario-ejemplo", tty_value=False)
        self.assertNotEqual(code, 0)
        code, _, _ = self.run_cli("projects", "owner", "no/valido", tty_value=True)
        self.assertNotEqual(code, 0)
        self.assertIsNone(ws.github_owner())
