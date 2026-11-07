"""One request, one router process: isolated environment, the permission mode chosen for the server, and a way to stop it."""
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import threading

from .. import agy_events
from .. import config as config_tool
from ..chat.spend import record_spend, spend
from ..cli.base import LIMIT_TEXT
from ..osutils import group_kwargs, module_cmd, module_env, send_prompt, stop_tree
from ..tools import TOOLS
from .auth import SECRET_VARS

FORCE_AFTER = 5  # seconds between asking a process to stop and killing it
LIMITED = re.compile(LIMIT_TEXT.pattern + '|cooldown', re.I)
MASKED = 'PII mascarado'  # the API clients say this when placeholders may show up in the streamed pieces

OK, FAILED, RATE_LIMITED, TIMEOUT, DISCONNECTED, SHUTDOWN, SWITCHED, DENIED = (
    'ok', 'failed', 'rate_limited', 'timeout', 'disconnected', 'shutdown', 'switched', 'denied')
# A headless CLI ends with an error when the server's mode refuses an action; Grok says it in a status line
ACTION_REFUSED = re.compile(r'cannot ask for approval', re.I)


class Execution:
    def __init__(self, tool, prompt, last_user, temperature=None, max_tokens=None, mode='plan', workdir=None, no_tools=False):
        self.tool, self.prompt, self.last_user = tool, prompt, last_user
        self.mode, self.workdir, self.no_tools = mode, workdir, no_tools
        self.temperature, self.max_tokens = temperature, max_tokens
        self.child = None
        self.lock = threading.Lock()
        self.stop_reason = None
        self.provider = None
        self.profile = ''
        self.answer = ''
        self.model = None
        self.protocol_error = False
        self.limited = False
        self.denied = False
        self.masked = False
        self.sent_delta = False
        self.tokens = None
        self.job = {'engine': tool}
        self.on_delta = None

    def _env(self):
        env = dict(os.environ, SMART_AI_EVENTS='1', SMART_AI_READ_ONLY='1' if self.mode == 'plan' else '0',
                   SMART_AI_MODE=self.mode, NO_COLOR='1',
                   PYTHONUNBUFFERED='1', SMART_AI_CLASSIFY_PROMPT=self.last_user)
        for name in SECRET_VARS:
            env.pop(name, None)
        env.pop('SMART_AI_NO_TOOLS', None)
        if self.no_tools:
            env['SMART_AI_NO_TOOLS'] = '1'
        env.pop('SMART_AI_TEMPERATURE', None)
        env.pop('SMART_AI_MAX_TOKENS', None)
        if self.temperature is not None:
            env['SMART_AI_TEMPERATURE'] = str(self.temperature)
        if self.max_tokens is not None:
            env['SMART_AI_MAX_TOKENS'] = str(self.max_tokens)
        return module_env(env)

    def run(self, on_delta=None):
        """Blocks until the router exits. Returns the outcome; `on_delta` gets streamed text as it arrives."""
        self.on_delta = on_delta
        # stdin: a command line can't hold a long prompt on Windows
        args = module_cmd('cli') + ([] if self.tool == 'auto' else ['--' + self.tool]) + ['--prompt-file', '-']
        with tempfile.TemporaryDirectory(prefix='omni-serve-', ignore_cleanup_errors=True) as folder:
            try:
                with self.lock:
                    if self.stop_reason:
                        return self.stop_reason
                    self.child = subprocess.Popen(args, cwd=self.workdir or folder, env=self._env(), stdin=subprocess.PIPE,
                                                  stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                                  encoding='utf-8', errors='replace', **group_kwargs())
                try:
                    send_prompt(self.child, self.prompt)
                    for raw in self.child.stdout:
                        self._line(raw.rstrip())
                    code = self.child.wait()
                finally:
                    self.child.stdout.close()
            except OSError:
                return FAILED
            finally:
                self.job['provider'] = self.provider or self.tool
                record_spend(self.job)
        if self.stop_reason:
            return self.stop_reason
        if code == 0 and self.answer.strip() and not self.protocol_error:
            return OK
        if self.limited:
            return RATE_LIMITED
        return DENIED if self.denied else FAILED

    def stop(self, reason):
        """Stops the whole process tree, gently first. The first reason given is the one reported."""
        with self.lock:
            if self.stop_reason:
                return
            self.stop_reason = reason
            child = self.child
        if child is not None:
            stop_tree(child, FORCE_AFTER)

    def _line(self, raw):
        if not raw:
            return
        try:
            event = json.loads(raw)
            if not isinstance(event, dict):
                raise ValueError()
        except ValueError:
            # Router status line: only used to tell why it failed, never sent to the client
            self.masked = self.masked or MASKED in raw
            self.limited = self.limited or bool(LIMITED.search(raw))
            self.denied = self.denied or bool(ACTION_REFUSED.search(raw))
            return
        if agy_events.is_agy_event(event):
            self._antigravity(event)
            return
        kind = event.get('type', '')
        if kind == 'smart.provider':
            if self.sent_delta:
                self.stop(SWITCHED)  # the client already has part of another answer
                return
            # It ends up in a response header, and this line is whatever the tool printed
            name = event.get('provider')
            self.provider, self.profile = name if isinstance(name, str) and name in TOOLS else None, event.get('profile') or ''
            self.answer, self.protocol_error, self.tokens = '', False, None
        elif kind == 'smart.delta':
            self._delta(event.get('text', ''))
        elif kind == 'smart.answer':
            self.answer, self.model = event.get('content', ''), event.get('model')
            self._usage(event.get('usage'))
        elif kind == 'item.completed' and event.get('item', {}).get('type') == 'agent_message':
            self.answer = event['item'].get('text', '') or self.answer
        elif kind == 'assistant':
            for block in event.get('message', {}).get('content', []):
                if block.get('type') == 'text' and block.get('text', '').strip():
                    self.answer = block['text']
        elif kind == 'result':
            if str(self.profile).startswith('api-key') and event.get('total_cost_usd'):
                self.job['cost'] = (self.job.get('cost') or 0) + event['total_cost_usd']
            self._usage(event.get('usage'))
            if event.get('is_error'):
                self.protocol_error = True
                self.limited = self.limited or bool(LIMITED.search(str(event.get('result') or '')))
            elif event.get('result'):
                self.answer = event['result']
        elif kind == 'turn.completed':
            self._usage(event.get('usage'))
        elif kind in ('error', 'turn.failed'):
            self.protocol_error = True
            self.limited = self.limited or bool(LIMITED.search(json.dumps(event, ensure_ascii=False)))

    def _antigravity(self, event):
        parsed = agy_events.parse(event)
        if parsed and parsed[0] == 'answer':
            self.answer = parsed[1] or self.answer
            self._usage(parsed[2])
        elif parsed and parsed[0] == 'error':
            self.protocol_error = True
            self.limited = self.limited or bool(LIMITED.search(parsed[1]))

    def _delta(self, text):
        # With masked data a piece can be a placeholder split in two: wait for the whole, restored answer.
        if not text or self.masked or self.on_delta is None:
            return
        try:
            self.on_delta(text)
            self.sent_delta = True
        except OSError:
            self.stop(DISCONNECTED)

    def _usage(self, usage):
        if not isinstance(usage, dict):
            return
        spend(self.job, usage)
        prompt = usage.get('prompt_tokens') or usage.get('input_tokens') or 0
        completion = usage.get('completion_tokens') or usage.get('output_tokens') or 0
        if prompt or completion:
            self.tokens = (int(prompt), int(completion))


def uses_shared_credentials():
    """Legacy Claude profiles swap ~/.claude/.credentials.json during a call, so they cannot run in parallel."""
    profiles = config_tool.profiles().get('profiles', {})
    main = Path('~/.claude/.credentials.json').expanduser()
    for name in config_tool.enabled_profiles():
        conf = profiles.get(name, {})
        # A profile pointing at the main file swaps nothing: it is what a default install has
        if conf.get('credentials_file') and not conf.get('config_dir') and Path(conf['credentials_file']).expanduser() != main:
            return True
    return False
