"""One isolated place per write request: a git worktree on its own branch, or a copy of a folder without git."""
import hashlib
import os
from pathlib import Path
import re
import secrets
import shutil
import subprocess
import sys
import threading
import time

from .protocol import RequestError

BRANCH_HEADER = 'X-Omni-Branch'
COPY_HEADER = 'X-Omni-Copy'
BRANCH_PREFIX = 'omni/'
WORK_ID = re.compile(r'[a-f0-9]{16}')
MAX_COPY_BYTES = 1024 ** 3
MAX_RUNS_BYTES = 20 * 1024 ** 3  # all copies of a folder: `ai serve work prune` frees them
IN_USE_SECONDS = 900  # a copy touched this recently may belong to a request still running


def runs_root(workdir):
    digest = hashlib.sha256(str(workdir).encode('utf-8')).hexdigest()[:16]
    return Path.home() / '.local/share/smart-ai/runs' / digest


def _git(folder, *args):
    # Server-owned commands on a folder the administrator chose: a host folder's owner may differ inside a container
    command = ['git', '-c', 'safe.directory=*', '-c', 'user.name=Omni AI', '-c', 'user.email=omni-ai@localhost',
               '-c', 'commit.gpgsign=false',  # the server has no one to answer a signing prompt
               '-c', 'core.fsmonitor=false', '-c', 'core.hooksPath=/dev/null',  # no commands from the repository's settings
               '-C', str(folder), *args]
    try:
        return subprocess.run(command, capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=120)
    except (OSError, subprocess.TimeoutExpired) as error:
        return subprocess.CompletedProcess(command, 1, '', str(error))


def _relink_inside(copy, source):
    """A copied absolute link can still point at the original folder, and writing through it would change it."""
    for root, folders, files in os.walk(copy):
        for name in folders + files:
            link = Path(root) / name
            if not link.is_symlink():
                continue
            target = Path(os.readlink(link))
            if not target.is_absolute():
                continue
            try:
                inside = target.resolve().relative_to(source)
            except (OSError, ValueError):
                continue
            link.unlink()
            link.symlink_to(Path(copy) / inside)


def _snapshot(folder):
    found = {}
    for root, _, files in os.walk(folder):
        for name in files:
            path = os.path.join(root, name)
            try:
                info = os.lstat(path)
                found[path] = (info.st_size, info.st_mtime_ns)
            except OSError:
                pass
    return found


def _size(folder):
    return sum(size for size, _ in _snapshot(folder).values())


class Work:
    """Where one request runs. `finish` keeps what changed and returns the header that points at it, if any."""

    def __init__(self, owner, work_id, path, new):
        self.owner, self.id, self.path, self.new = owner, work_id, path, new
        self.header = None


class Workspaces:
    def __init__(self, workdir):
        self.workdir = Path(workdir).resolve()
        self.root = runs_root(self.workdir)
        self.real_root = Path(os.path.realpath(self.root))
        self.lock = threading.Lock()  # git locks its own files, and a second worktree add at once would fail
        self.busy = set()
        top = _git(self.workdir, 'rev-parse', '--show-toplevel')
        has_commit = _git(self.workdir, 'rev-parse', '--verify', 'HEAD').returncode == 0
        self.repo = Path(top.stdout.strip()).resolve() if top.returncode == 0 and has_commit else None
        self.recover()

    @property
    def uses_git(self):
        return self.repo is not None

    def _claim(self, work_id):
        with self.lock:
            if work_id in self.busy:
                raise RequestError(409, 'Another request is working on this one.', code='work_in_progress')
            self.busy.add(work_id)

    def _release(self, work_id):
        with self.lock:
            self.busy.discard(work_id)

    def recover(self):
        """A crash leaves worktrees behind. Their changes are committed first, so no work is lost."""
        if not self.uses_git or not self.root.is_dir():
            if self.uses_git:
                _git(self.repo, 'worktree', 'prune')
            return
        # Only worktrees: this folder can hold copies from when the project had no git, and they are the user's
        listed = _git(self.repo, 'worktree', 'list', '--porcelain').stdout.splitlines()
        worktrees = {Path(line[len('worktree '):]).resolve() for line in listed if line.startswith('worktree ')}
        for folder in self.root.iterdir():
            if WORK_ID.fullmatch(folder.name) and folder.resolve() in worktrees:
                self._save_and_remove(folder.name, folder)
        _git(self.repo, 'worktree', 'prune')

    def acquire(self, headers):
        return self._acquire_git(headers.get(BRANCH_HEADER)) if self.uses_git else self._acquire_copy(headers.get(COPY_HEADER))

    def _acquire_git(self, wanted):
        new = not wanted
        if wanted:
            work_id = wanted[len(BRANCH_PREFIX):] if wanted.startswith(BRANCH_PREFIX) else ''
            # Only branches made here: a client must not be able to work directly on main
            if not WORK_ID.fullmatch(work_id):
                raise RequestError(400, 'Invalid branch.', param=BRANCH_HEADER)
        else:
            work_id = secrets.token_hex(8)
        self._claim(work_id)
        branch, path = BRANCH_PREFIX + work_id, self.root / work_id
        with self.lock:
            self.root.mkdir(parents=True, exist_ok=True)
            if new:
                result = _git(self.repo, 'worktree', 'add', '-q', '-b', branch, str(path), 'HEAD')
            elif _git(self.repo, 'rev-parse', '--verify', f'refs/heads/{branch}').returncode != 0:
                self.busy.discard(work_id)
                raise RequestError(404, 'Branch not found.', param=BRANCH_HEADER, code='work_not_found')
            else:
                result = _git(self.repo, 'worktree', 'add', '-q', str(path), branch)
            if result.returncode != 0:
                self.busy.discard(work_id)
                raise RequestError(500, 'Could not prepare the working folder.', 'api_error')
        inner = path / self.workdir.relative_to(self.repo)
        if not inner.resolve().is_relative_to(path.resolve()):  # a link committed on the branch
            with self.lock:
                _git(self.repo, 'worktree', 'remove', '--force', str(path))
                self.busy.discard(work_id)
            raise RequestError(400, 'The working folder on this branch leads outside it.', param=BRANCH_HEADER)
        return GitWork(self, work_id, inner, new, path)

    def _acquire_copy(self, wanted):
        if wanted and not WORK_ID.fullmatch(wanted):
            raise RequestError(400, 'Invalid copy id.', param=COPY_HEADER)
        work_id = wanted or secrets.token_hex(8)
        self._claim(work_id)
        path = self.root / work_id
        try:
            if wanted and not path.is_dir():
                raise RequestError(404, 'Copy not found.', param=COPY_HEADER, code='work_not_found')
            if not wanted:
                size = _size(self.workdir)
                self.root.mkdir(parents=True, exist_ok=True)
                if size > MAX_COPY_BYTES or size > shutil.disk_usage(self.root).free:
                    raise RequestError(507, 'The folder is too large to copy for this request.', 'api_error', code='workspace_too_large')
                if size + _size(self.root) > MAX_RUNS_BYTES:
                    raise RequestError(507, 'The copies of earlier requests fill the space allowed. Remove them with `ai serve work prune`.',
                                       'api_error', code='workspace_quota')
                shutil.copytree(self.workdir, path, symlinks=True)
                _relink_inside(path, self.workdir)
        except (OSError, shutil.Error):
            self._release(work_id)
            shutil.rmtree(path, ignore_errors=True)
            raise RequestError(500, 'Could not prepare the working folder.', 'api_error') from None
        except RequestError:
            self._release(work_id)
            raise
        return CopyWork(self, work_id, path, not wanted)

    def _tree_git(self, work_id, path, *args):
        """Git on a folder the tool could write to. The repository is named here, never discovered from a `.git`
        in that folder, whose configuration can run commands."""
        common = self.repo / _git(self.repo, 'rev-parse', '--git-common-dir').stdout.strip()
        return _git(path, f'--git-dir={common.resolve() / "worktrees" / work_id}', f'--work-tree={path}', *args)

    def _save_and_remove(self, work_id, path):
        """Commits what the request left. Returns False when the branch ended with nothing of its own."""
        branch = BRANCH_PREFIX + work_id
        with self.lock:
            if Path(os.path.realpath(path)) != self.real_root / work_id:
                # A link put in its place: git would save whatever is at the other end into this branch
                print(f'Omni AI: request {work_id} was not saved: its folder now leads somewhere else', file=sys.stderr)
                return True
            self._tree_git(work_id, path, 'add', '-A')
            self._tree_git(work_id, path, 'commit', '-q', '--no-verify', '-m', f'Omni AI request {work_id}')
            left = self._tree_git(work_id, path, 'status', '--porcelain')
            if left.returncode != 0 or left.stdout.strip():
                # Not saved: the folder is the only copy of the work, so it stays
                print(f'Omni AI: could not commit request {work_id}; its work stays in {path}', file=sys.stderr)
                return True
            ahead = _git(self.repo, 'rev-list', '--count', f'HEAD..{branch}').stdout.strip()
            _git(self.repo, 'worktree', 'remove', '--force', str(path))
            if path.exists():
                shutil.rmtree(path, ignore_errors=True)
                _git(self.repo, 'worktree', 'prune')
        return ahead not in ('', '0')


class GitWork(Work):
    def __init__(self, owner, work_id, path, new, tree):
        super().__init__(owner, work_id, path, new)
        self.tree = tree
        self.header = (BRANCH_HEADER, BRANCH_PREFIX + work_id)

    def finish(self):
        try:
            changed = self.owner._save_and_remove(self.id, self.tree)
            if self.new and not changed:
                with self.owner.lock:
                    _git(self.owner.repo, 'branch', '-q', '-D', BRANCH_PREFIX + self.id)
                self.header = None
        finally:
            self.owner._release(self.id)


class CopyWork(Work):
    def __init__(self, owner, work_id, path, new):
        super().__init__(owner, work_id, path, new)
        self.before = _snapshot(path)
        self.header = (COPY_HEADER, work_id)

    def finish(self):
        try:
            if self.new and _snapshot(self.path) == self.before:
                shutil.rmtree(self.path, ignore_errors=True)
                self.header = None
        finally:
            self.owner._release(self.id)


def _repo_of(workdir):
    top = _git(workdir, 'rev-parse', '--show-toplevel')
    has_commit = _git(workdir, 'rev-parse', '--verify', 'HEAD').returncode == 0
    return Path(top.stdout.strip()).resolve() if top.returncode == 0 and has_commit else None


def _newest(folder):
    times = [stamp / 1e9 for _, stamp in _snapshot(folder).values()]
    return max(times, default=os.path.getmtime(folder))


def list_work(workdir):
    """What requests left for this folder: branches and copies. Each item: id, kind, age in days, merged, size, in_use."""
    workdir = Path(workdir).resolve()
    root, repo, now, found = runs_root(workdir), _repo_of(workdir), time.time(), []
    if repo:
        output = _git(repo, 'for-each-ref', '--format=%(refname:short) %(committerdate:unix)', f'refs/heads/{BRANCH_PREFIX}*').stdout
        merged = set(_git(repo, 'branch', '--merged', 'HEAD', '--format=%(refname:short)').stdout.split())
        for line in output.splitlines():
            name, stamp = line.split()
            if WORK_ID.fullmatch(name[len(BRANCH_PREFIX):]):
                found.append({'id': name, 'kind': 'branch', 'age_days': (now - int(stamp)) / 86400, 'merged': name in merged, 'size': None,
                              'in_use': (root / name[len(BRANCH_PREFIX):]).exists()})
    if root.is_dir():
        for folder in sorted(root.iterdir()):
            if WORK_ID.fullmatch(folder.name) and not repo:
                newest = _newest(folder)
                found.append({'id': folder.name, 'kind': 'copy', 'age_days': (now - newest) / 86400, 'merged': False,
                              'size': _size(folder), 'in_use': now - newest < IN_USE_SECONDS})
    return found


def remove_work(workdir, item):
    """Deletes one item from list_work. Only ids made here reach this point, never another branch or folder."""
    workdir = Path(workdir).resolve()
    if item['kind'] == 'branch':
        return _git(_repo_of(workdir), 'branch', '-q', '-D', item['id']).returncode == 0
    shutil.rmtree(runs_root(workdir) / item['id'], ignore_errors=True)
    return not (runs_root(workdir) / item['id']).exists()
