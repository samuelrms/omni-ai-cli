"""Request queue: typed requests wait their turn while the input stays free."""
import threading

from .. import ui
from .i18n import tr, display
from .runner import ACTIVE, execute
from .ops import compact
class Queue:
    """Requests typed while another one runs wait their turn here; the input stays free the whole time.

    A request is a callable or a dict spec ({'kind': 'ask' | 'skill' | 'parallel' | 'compact', ...}) that run()
    executes; specs are what gets saved with the session, so a closed chat can pick the line up again.
    """

    def __init__(self, skip=None, run=None, changed=None):
        self.items, self.current, self.current_spec = [], None, None
        self.skip, self.run, self.changed = skip, run, changed
        self.condition = threading.Condition()
        threading.Thread(target=self._loop, daemon=True).start()

    def put(self, label, work, waited=None):
        with self.condition:
            # only a request that actually waited gets a "from the queue" line when its turn comes
            busy = self.current is not None or bool(self.items)
            self.items.append((label, work, busy if waited is None else waited))
            self.condition.notify()
        self._changed()

    def busy(self):
        with self.condition:
            return self.current is not None or bool(self.items)

    def snapshot(self):
        with self.condition:
            return self.current, [label for label, _, _ in self.items]

    def pending(self):
        """Saveable specs, the running one first (it reruns on resume: it never finished)."""
        with self.condition:
            specs = ([dict(self.current_spec, label=self.current)] if isinstance(self.current_spec, dict) else []) + \
                    [dict(work, label=label) for label, work, _ in self.items if isinstance(work, dict)]
        return specs

    def _changed(self):
        if self.changed:
            self.changed(self.pending())

    def _loop(self):
        while True:
            with self.condition:
                while not self.items:
                    self.condition.wait()
                label, work, waited = self.items.pop(0)
                self.current, self.current_spec = label, work
            self._changed()
            if waited:
                ui.out('\n' + ui.dim(tr('next_up', v=ui.short(label.split('\n')[0], ui.width() - 20))))
            try:
                if waited and self.skip and self.skip(label):
                    ui.out(ui.dim(tr('skipped', v=ui.short(label.split('\n')[0], ui.width() - 50))))
                    continue
                work() if callable(work) else self.run(work)
            except Exception as error:  # a broken request must not take the worker (and the queue) down
                display(tr('error', e=error))
            finally:
                with self.condition:
                    self.current = self.current_spec = None
                    self.condition.notify_all()
                self._changed()

    def remove(self, number):
        """Take a waiting request out (1 = next in line); returns its label."""
        with self.condition:
            self._check(number)
            label = self.items.pop(number - 1)[0]
        self._changed()
        return label

    def move(self, number, offset):
        """Move a waiting request up (offset -1) or down (+1); returns its new position."""
        with self.condition:
            self._check(number)
            target = min(max(number - 1 + offset, 0), len(self.items) - 1)
            self.items.insert(target, self.items.pop(number - 1))
        self._changed()
        return target + 1

    def clear(self):
        """Drop every waiting request, leave the running one alone; returns how many were dropped."""
        with self.condition:
            dropped = len(self.items)
            self.items.clear()
        self._changed()
        return dropped

    def _check(self, number):
        if not 1 <= number <= len(self.items):
            raise ValueError(tr('queue_bad', n=len(self.items)))

    def cancel(self):
        """Drop what waits and stop what runs; returns how many waiting requests were dropped."""
        dropped = self.clear()
        for runner in list(ACTIVE):
            runner.cancel()
        return dropped

    def wait(self, timeout=10):
        with self.condition:
            self.condition.wait_for(lambda: self.current is None and not self.items, timeout)


def run_spec(session, spec):
    """Run a saved queue request in the given session."""
    kind = spec['kind']
    if kind == 'ask':
        execute(session, [(spec['engine'], spec['text'])])
    elif kind == 'skill':
        execute(session, [(spec['tool'], spec['prompt'])], raw=spec.get('raw', False))
    elif kind == 'parallel':
        execute(session, [tuple(task) for task in spec['tasks']], parallel=True)
    elif kind == 'compact':
        compact(session)
    else:
        raise ValueError(f'pedido desconhecido na fila: {kind}')


def queue_command(queue, argument):
    """/queue · /queue rm N · /queue up N · /queue down N · /queue clear"""
    action, _, number = argument.strip().partition(' ')
    if action in ('rm', 'remove', 'up', 'down'):
        if not number.strip().isdigit():
            raise ValueError(tr('queue_usage'))
        n = int(number)
        if action in ('rm', 'remove'):
            display(tr('queue_removed', v=ui.short(queue.remove(n).split('\n')[0], 60)))
        else:
            display(tr('queue_moved', n=queue.move(n, -1 if action == 'up' else 1)))
    elif action == 'clear':
        display(tr('queue_cleared', n=queue.clear()))
    elif action:
        raise ValueError(tr('queue_usage'))
    current, waiting = queue.snapshot()
    if current is None and not waiting:
        display(tr('queue_empty'))
        return
    if current is not None:
        display(f"▶ {ui.short(current.split(chr(10))[0], ui.width() - 4)}")
    for i, label in enumerate(waiting, 1):
        display(f"{i:>2}. {ui.short(label.split(chr(10))[0], ui.width() - 5)}")


def activity(queue):
    """Lines above the input: the request running now and the ones waiting, in order."""
    current, waiting = queue.snapshot()
    if current is None:
        return []
    room = ui.width() - 30
    since = ui.elapsed(ui.SPINNER.started) if ui.SPINNER.started else ''
    lines = [f"<running>{ui.SPINNER.frame()} {ui.escape(ui.SPINNER.label or tr('working'))}</running> "
             f"<queued>{since} · {ui.escape(ui.short(current.split(chr(10))[0], room))} · {ui.escape(tr('queue_cancel'))}</queued>"]
    if waiting:
        lines.append(f"<queued>  {ui.escape(tr('queued', n=len(waiting)))}</queued>")
        lines += [f"<queued>  {i}. {ui.escape(ui.short(label.split(chr(10))[0], room))}</queued>" for i, label in enumerate(waiting[:5], 1)]
        if len(waiting) > 5:
            lines.append(f'<queued>  … +{len(waiting) - 5}</queued>')
    return lines
