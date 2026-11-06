import isolation  # noqa: F401  (first: keeps the tests away from real data)
import http.client
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from test_server import ROOT

sys.path.insert(0, str(ROOT))
from omni_ai.server import workspaces  # noqa: E402
from omni_ai.server.protocol import RequestError  # noqa: E402
from omni_ai.server.workspaces import BRANCH_HEADER, COPY_HEADER, Workspaces  # noqa: E402

# Writes a file named after the prompt, waits so requests overlap, and answers like the real CLI
CODEX = """#!/bin/sh
for last; do :; done
case "$last" in *wait*) sleep 1 ;; esac
case "$last" in *nothing*) ;; *fail*) echo partial > partial.txt; echo boom; exit 1 ;; *) echo "$last" > "req-$$.txt"; echo base >> shared.txt ;; esac
pwd >> "$FAKE_LOG"
echo '{"type":"item.completed","item":{"type":"agent_message","text":"done"}}'
"""


def git(folder, *args):
    return subprocess.run(['git', '-C', str(folder), '-c', 'user.name=t', '-c', 'user.email=t@t', *args],
                          capture_output=True, text=True, check=True).stdout.strip()


class Temp(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        self.folder = self.root / 'project'
        self.folder.mkdir()
        (self.folder / 'shared.txt').write_text('start\n')

    def init_git(self):
        git(self.folder, 'init', '-q', '-b', 'main')
        git(self.folder, 'add', '-A')
        git(self.folder, 'commit', '-qm', 'first')


class RecoveryAfterGitInit(Temp):
    def test_copies_survive_when_the_folder_becomes_a_repository(self):
        work = Workspaces(self.folder).acquire({})
        (work.path / 'kept.txt').write_text('copy')
        work.finish()
        self.assertEqual(work.header[0], COPY_HEADER)
        self.init_git()
        Workspaces(self.folder)  # the next server start
        self.assertEqual((work.path / 'kept.txt').read_text(), 'copy')


class GitWorkspace(Temp):
    def setUp(self):
        super().setUp()
        self.init_git()
        self.spaces = Workspaces(self.folder)

    def branches(self):
        return git(self.folder, 'branch', '--format=%(refname:short)').split()

    def test_changes_go_to_a_branch_and_main_stays_clean(self):
        work = self.spaces.acquire({})
        (work.path / 'new.txt').write_text('x')
        work.finish()
        name = work.header[1]
        self.assertEqual(work.header[0], BRANCH_HEADER)
        self.assertEqual(git(self.folder, 'show', f'{name}:new.txt'), 'x')
        self.assertFalse((self.folder / 'new.txt').exists())
        self.assertEqual(git(self.folder, 'status', '--porcelain'), '')
        self.assertEqual(git(self.folder, 'worktree', 'list', '--porcelain').count('worktree '), 1)

    def test_no_changes_leaves_nothing(self):
        work = self.spaces.acquire({})
        work.finish()
        self.assertIsNone(work.header)
        self.assertEqual(self.branches(), ['main'])

    def test_commit_made_by_the_tool_counts(self):
        work = self.spaces.acquire({})
        (work.path / 'a.txt').write_text('x')
        git(work.path, 'add', '-A')
        git(work.path, 'commit', '-qm', 'by the tool')
        work.finish()
        self.assertEqual(git(self.folder, 'show', f'{work.header[1]}:a.txt'), 'x')

    def test_continue_on_the_same_branch(self):
        first = self.spaces.acquire({})
        (first.path / 'a.txt').write_text('1')
        first.finish()
        second = self.spaces.acquire({BRANCH_HEADER: first.header[1]})
        self.assertEqual((second.path / 'a.txt').read_text(), '1')
        second.finish()  # nothing new: the branch is kept
        self.assertIn(first.header[1], self.branches())
        self.assertEqual(second.header, first.header)

    def test_same_branch_at_once_is_refused(self):
        first = self.spaces.acquire({})
        (first.path / 'a.txt').write_text('1')
        first.finish()
        second = self.spaces.acquire({BRANCH_HEADER: first.header[1]})
        with self.assertRaises(RequestError) as caught:
            self.spaces.acquire({BRANCH_HEADER: first.header[1]})
        self.assertEqual(caught.exception.status, 409)
        second.finish()

    def test_only_branches_made_here(self):
        for wanted, status in (('main', 400), ('omni/../main', 400), ('--force', 400), ('omni/0123456789abcdef', 404)):
            with self.assertRaises(RequestError, msg=wanted) as caught:
                self.spaces.acquire({BRANCH_HEADER: wanted})
            self.assertEqual(caught.exception.status, status, wanted)
        self.spaces.acquire({}).finish()  # a refused branch stays usable and nothing stays claimed
        self.assertEqual(self.spaces.busy, set())

    def test_subfolder_stays_the_base(self):
        sub = self.folder / 'pkg'
        sub.mkdir()
        (sub / 'f').write_text('1')
        git(self.folder, 'add', '-A')
        git(self.folder, 'commit', '-qm', 'sub')
        spaces = Workspaces(sub)
        work = spaces.acquire({})
        self.assertEqual(work.path.name, 'pkg')
        self.assertTrue((work.path / 'f').is_file())
        work.finish()

    def test_signing_configured_does_not_lose_the_work(self):
        git(self.folder, 'config', 'commit.gpgsign', 'true')
        git(self.folder, 'config', 'gpg.program', '/bin/false')
        spaces = Workspaces(self.folder)
        work = spaces.acquire({})
        (work.path / 'new.txt').write_text('work')
        work.finish()
        self.assertEqual(work.header[0], BRANCH_HEADER)
        self.assertEqual(git(self.folder, 'show', f'{work.header[1]}:new.txt'), 'work')

    def test_failed_commit_keeps_the_folder_and_the_branch(self):
        spaces = Workspaces(self.folder)
        work = spaces.acquire({})
        (work.path / 'new.txt').write_text('work')
        real = workspaces._git
        def fail_commit(folder, *args):
            return subprocess.CompletedProcess(args, 1, '', 'no') if args[0] == 'commit' else real(folder, *args)
        with patch.object(workspaces, '_git', fail_commit), patch('sys.stderr', io.StringIO()) as err:
            work.finish()
        self.assertEqual((work.path / 'new.txt').read_text(), 'work')
        self.assertIn(str(work.path), err.getvalue())
        self.assertEqual(work.header[0], BRANCH_HEADER)
        self.assertIn(work.header[1], git(self.folder, 'branch', '--list', 'omni/*'))
        Workspaces(self.folder)  # the next server start saves it
        self.assertEqual(git(self.folder, 'show', f'{work.header[1]}:new.txt'), 'work')
        self.assertFalse(work.path.exists())

    def test_crash_recovery_keeps_the_work(self):
        work = self.spaces.acquire({})
        (work.path / 'lost.txt').write_text('saved')
        name = work.header[1]
        Workspaces(self.folder)  # the next server start
        self.assertEqual(git(self.folder, 'show', f'{name}:lost.txt'), 'saved')
        self.assertFalse(work.path.exists())

    def test_many_at_once(self):
        works, errors = [], []

        def take():
            try:
                works.append(self.spaces.acquire({}))
            except RequestError as error:
                errors.append(error)
        threads = [threading.Thread(target=take) for _ in range(8)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        self.assertEqual((len(works), errors), (8, []))
        self.assertEqual(len({w.path for w in works}), 8)
        [w.finish() for w in works]


class CopyWorkspace(Temp):
    def setUp(self):
        super().setUp()
        self.spaces = Workspaces(self.folder)

    def test_changes_stay_in_the_copy(self):
        self.assertFalse(self.spaces.uses_git)
        work = self.spaces.acquire({})
        self.assertEqual(work.header[0], COPY_HEADER)
        (work.path / 'shared.txt').write_text('changed')
        work.finish()
        self.assertEqual((self.folder / 'shared.txt').read_text(), 'start\n')
        self.assertEqual((work.path / 'shared.txt').read_text(), 'changed')
        self.assertIsNotNone(work.header)

    def test_no_changes_removes_it(self):
        work = self.spaces.acquire({})
        work.finish()
        self.assertIsNone(work.header)
        self.assertFalse(work.path.exists())

    def test_continue(self):
        first = self.spaces.acquire({})
        (first.path / 'a').write_text('1')
        first.finish()
        second = self.spaces.acquire({COPY_HEADER: first.id})
        self.assertEqual(second.path, first.path)
        with self.assertRaises(RequestError) as caught:
            self.spaces.acquire({COPY_HEADER: first.id})
        self.assertEqual(caught.exception.status, 409)
        second.finish()
        self.assertTrue(first.path.exists())

    def test_ids_cannot_escape(self):
        for wanted, status in (('../../etc', 400), ('0123456789abcdef', 404)):
            with self.assertRaises(RequestError) as caught:
                self.spaces.acquire({COPY_HEADER: wanted})
            self.assertEqual(caught.exception.status, status)

    def test_folder_too_large(self):
        from unittest.mock import patch
        with patch('omni_ai.server.workspaces.MAX_COPY_BYTES', 1), self.assertRaises(RequestError) as caught:
            self.spaces.acquire({})
        self.assertEqual(caught.exception.status, 507)
        self.assertEqual(self.spaces.busy, set())

    def test_repository_without_commits_uses_a_copy(self):
        git(self.folder, 'init', '-q')
        self.assertFalse(Workspaces(self.folder).uses_git)


class Cleanup(Temp):
    def cli(self, *args):
        out = subprocess.run([sys.executable, '-m', 'omni_ai.cli', 'serve', 'work', *args, '--workdir', str(self.folder)],
                             env={**os.environ, 'HOME': str(self.root), 'PYTHONPATH': str(ROOT)}, capture_output=True, text=True, timeout=30)
        return out.returncode, out.stdout + out.stderr

    def make(self, content='x'):
        import unittest.mock as mock
        with mock.patch.dict(os.environ, {'HOME': str(self.root)}):
            spaces = Workspaces(self.folder)
            work = spaces.acquire({})
            (work.path / 'new.txt').write_text(content)
            work.finish()
        return work

    def test_list_and_nothing_goes_without_yes(self):
        self.init_git()
        branch = self.make().header[1]
        code, text = self.cli('list')
        self.assertIn(branch, text)
        self.assertEqual(self.cli('remove', branch)[0], 0)
        self.assertIn(branch, git(self.folder, 'branch'))
        self.assertEqual(self.cli('remove', branch, '--yes')[0], 0)
        self.assertNotIn(branch, git(self.folder, 'branch'))

    def test_other_branches_are_out_of_reach(self):
        self.init_git()
        git(self.folder, 'branch', 'omni/mine')
        git(self.folder, 'branch', 'feature')
        for name in ('main', 'feature', 'omni/mine'):
            self.assertEqual(self.cli('remove', name, '--yes')[0], 1, name)
        self.assertEqual(self.cli('prune', '--merged', '--yes')[0], 0)
        self.assertEqual(sorted(git(self.folder, 'branch', '--format=%(refname:short)').split()), ['feature', 'main', 'omni/mine'])

    def test_prune_merged_keeps_unmerged(self):
        self.init_git()
        kept = self.make().header[1]
        merged = self.make('y').header[1]
        git(self.folder, 'merge', '-q', merged)
        self.assertEqual(self.cli('prune', '--merged', '--yes')[0], 0)
        names = git(self.folder, 'branch', '--format=%(refname:short)').split()
        self.assertIn(kept, names)
        self.assertNotIn(merged, names)

    def test_prune_needs_a_filter(self):
        self.init_git()
        self.assertEqual(self.cli('prune', '--yes')[0], 2)

    def test_older_than_spares_recent(self):
        self.init_git()
        branch = self.make().header[1]
        self.cli('prune', '--older-than', '30', '--yes')
        self.assertIn(branch, git(self.folder, 'branch'))

    def test_recent_copy_is_kept_and_old_one_goes(self):
        work = self.make()
        self.assertEqual(self.cli('remove', work.id, '--yes')[0], 1)
        self.assertTrue(work.path.exists())
        old = time.time() - 40 * 86400
        for root, folders, files in os.walk(work.path):
            for name in folders + files:
                os.utime(os.path.join(root, name), (old, old))
        os.utime(work.path, (old, old))
        self.assertEqual(self.cli('prune', '--older-than', '30', '--yes')[0], 0)
        self.assertFalse(work.path.exists())

    def test_prose_stays_a_prompt(self):
        fake = self.root / 'codex'
        fake.write_text('#!/bin/sh\necho "CALLED $*"\n')
        fake.chmod(0o700)
        out = subprocess.run([sys.executable, '-m', 'omni_ai.cli', '--codex', 'serve', 'work', 'done'], capture_output=True, text=True,
                             env={**os.environ, 'HOME': str(self.root), 'PYTHONPATH': str(ROOT), 'CODEX_BIN': str(fake)})
        self.assertIn('serve work done', out.stdout)


class OverHttp(Temp):
    def start(self, *args):
        fake = self.root / 'codex'
        fake.write_text(CODEX)
        fake.chmod(0o700)
        self.log = self.root / 'calls'
        (self.root / 'profiles.json').write_text(json.dumps({'profiles': {'main': {'priority': 1}}}))
        env = {**os.environ, 'HOME': str(self.root), 'PYTHONPATH': str(ROOT), 'SMART_AI_HOME': str(self.root / 'cfg'),
               'SMART_AI_CONFIG': str(self.root / 'profiles.json'), 'SMART_AI_STATE': str(self.root / 'state'),
               'SMART_AI_LOG': str(self.root / 'usage.log'), 'CODEX_BIN': str(fake), 'FAKE_LOG': str(self.log),
               'SMART_AI_SERVER_KEY': 'client'}
        process = subprocess.Popen([sys.executable, '-m', 'omni_ai.cli', 'serve', '--port', '0', '--mode', 'auto', '--workdir', str(self.folder), *args],
                                   env=env, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
        self.addCleanup(process.wait)
        self.addCleanup(process.stdout.close)
        self.addCleanup(process.terminate)
        self.port = int(process.stdout.readline().split(':')[2].split('/')[0])

    def post(self, content='write', headers=None):
        connection = http.client.HTTPConnection('127.0.0.1', self.port, timeout=30)
        self.addCleanup(connection.close)
        connection.request('POST', '/v1/chat/completions', json.dumps({'model': 'omni/codex', 'messages': [{'role': 'user', 'content': content}]}),
                           {'Authorization': 'Bearer client', **(headers or {})})
        response = connection.getresponse()
        response.read()
        return response

    def parallel(self, count, content='wait'):
        out = []
        threads = [threading.Thread(target=lambda: out.append(self.post(content))) for _ in range(count)]
        began = time.monotonic()
        [t.start() for t in threads]
        [t.join() for t in threads]
        return out, time.monotonic() - began

    def test_parallel_requests_do_not_touch_each_other(self):
        self.init_git()
        self.start()
        responses, spent = self.parallel(4)
        self.assertEqual([r.status for r in responses], [200] * 4)
        self.assertLess(spent, 3.5)  # four 1 s requests, not one after the other
        names = {r.headers[BRANCH_HEADER] for r in responses}
        self.assertEqual(len(names), 4)
        for name in names:
            self.assertEqual(git(self.folder, 'show', f'{name}:shared.txt').split(), ['start', 'base'])
        self.assertEqual((self.folder / 'shared.txt').read_text(), 'start\n')

    def test_folder_without_git(self):
        self.start()
        response = self.post()
        self.assertEqual(response.status, 200)
        copy = response.headers[COPY_HEADER]
        self.assertEqual((self.folder / 'shared.txt').read_text(), 'start\n')
        found = list((self.root / '.local/share/smart-ai/runs').glob(f'*/{copy}'))
        self.assertEqual(len(found), 1)
        self.assertIn('base', (found[0] / 'shared.txt').read_text())

    def test_failure_keeps_the_work_and_says_where(self):
        self.init_git()
        self.start()
        response = self.post('fail')
        self.assertEqual(response.status, 502)
        self.assertEqual(git(self.folder, 'show', f'{response.headers[BRANCH_HEADER]}:partial.txt'), 'partial')

    def test_nothing_changed_has_no_header(self):
        self.init_git()
        self.start()
        response = self.post('nothing')
        self.assertEqual(response.status, 200)
        self.assertIsNone(response.headers[BRANCH_HEADER])

    def test_queue_waits_instead_of_refusing(self):
        self.init_git()
        self.start('--max-concurrent', '1')
        responses, spent = self.parallel(3)
        self.assertEqual([r.status for r in responses], [200] * 3)
        self.assertGreater(spent, 2.5)

    def test_queue_full_is_429(self):
        self.init_git()
        self.start('--max-concurrent', '1', '--queue-size', '1')
        responses, _ = self.parallel(4)
        statuses = sorted(r.status for r in responses)
        self.assertEqual(statuses.count(200), 2)
        self.assertEqual(statuses.count(429), 2)

    def test_wait_runs_out_is_429(self):
        self.init_git()
        fake_wait = 'sleep 4'
        self.start('--max-concurrent', '1', '--queue-wait', '1')
        (self.root / 'codex').write_text(CODEX.replace('sleep 1', fake_wait))
        responses, _ = self.parallel(2)
        self.assertEqual(sorted(r.status for r in responses), [200, 429])


if __name__ == '__main__':
    unittest.main()
