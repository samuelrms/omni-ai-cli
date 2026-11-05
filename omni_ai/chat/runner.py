"""The subprocess runner that drives the router CLI and turns its event stream into screen output."""
import concurrent.futures
import re
import json
import os
import subprocess
import threading
import time

from .. import config as config_tool
from .. import ui
from ..osutils import group_kwargs, kill_tree, module_cmd, module_env
from ..tools import TOOLS
from .i18n import tr, ESCAPES
from .session import Session
from .spend import spend, spend_label, record_spend

ACTIVE = set()  # runners working right now, so Ctrl-C at the input can stop them
NOISE = re.compile(r'^[─━-]{5,}$|^(🤖|📦|🚀|🌐|📊|→ Credenciais)|Reading additional input from stdin|^AGY_ERROR: |'
                   r'^\d{4}-\d\d-\d\dT[\d:.]+Z\s+(ERROR|WARN|INFO)|^\s*$')

def model_label(provider, profile=''):
    values = config_tool.model_values()
    if provider not in TOOLS:
        return profile
    # codex shows its effort too; an empty model is the tool's own default
    label = f"{values['codex']} {values['codex_effort']}" if provider == 'codex' else values[provider] or \
        {'openrouter': 'gratuito', 'deepseek': 'deepseek-chat'}.get(provider, 'padrão')
    return ' · '.join(x for x in (profile, label) if x)


def describe_tool(name, data):
    """One-line summary of a Claude tool call, like Claude Code's own transcript."""
    data = data or {}
    if name.startswith('mcp__'):
        parts = name.split('__')
        return 'Called', f"{parts[1]} · {parts[-1]}"
    if name == 'Bash':
        return 'Ran', data.get('description') or data.get('command', '')
    if name in ('Read', 'Write', 'Edit', 'NotebookEdit'):
        return {'Read': 'Read', 'Write': 'Wrote', 'Edit': 'Edited', 'NotebookEdit': 'Edited'}[name], \
            ui.home_path(data.get('file_path') or data.get('notebook_path', ''))
    if name in ('Grep', 'Glob'):
        return 'Searched', data.get('pattern', '')
    if name in ('WebFetch', 'WebSearch'):
        return 'Fetched' if name == 'WebFetch' else 'Searched web', data.get('url') or data.get('query', '')
    if name in ('Task', 'Agent'):
        return 'Agent', data.get('description', '')
    if name == 'Skill':
        return 'Skill', data.get('skill', '')
    if name == 'TodoWrite':
        return 'Updated todos', ''
    return name, json.dumps(data, ensure_ascii=False)

class Runner:
    def __init__(self, cwd, mode='default'):
        self.cwd, self.mode = cwd, mode
        self.children = set()
        self.lock = threading.Lock()
        self.cancelled = threading.Event()

    def cancel(self):
        self.cancelled.set()
        with self.lock:
            children = list(self.children)
        for child in children:
            kill_tree(child)

    def run(self, job, classify_prompt, readonly=False):
        env = module_env(dict(os.environ, SMART_AI_EVENTS='1', SMART_AI_CLASSIFY_PROMPT=classify_prompt, SMART_AI_MODE=self.mode,
                   SMART_AI_READ_ONLY='1' if readonly else '0', PYTHONUNBUFFERED='1', NO_COLOR='1'))
        args = module_cmd('cli')
        if job['engine'] != 'auto':
            args += ['--' + job['engine']]
        args += ['--', job['sent']]
        job['events'] = []
        job['answers'] = []
        job['view'] = ui.View(f"[{job['id']}] " if readonly else '')
        try:
            with self.lock:
                if self.cancelled.is_set():
                    job['status'] = 'cancelled'
                    return job
                child = subprocess.Popen(args, cwd=self.cwd, env=env, stdin=subprocess.DEVNULL,
                                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                         encoding='utf-8', errors='replace', **group_kwargs())
                self.children.add(child)
            try:
                for raw in child.stdout:
                    self.event(job, raw.rstrip())
                code = child.wait()
            finally:
                child.stdout.close()
                with self.lock:
                    self.children.discard(child)
            job['exit_code'] = code
            job['status'] = 'cancelled' if self.cancelled.is_set() else ('done' if code == 0 and job['answers'] and not job.get('protocol_error') else 'failed')
        except (OSError, ValueError) as error:
            job['status'] = 'failed'
            job['view'].error(str(error))
        return job

    def event(self, job, raw):
        if not raw:
            return
        view = job['view']
        try:
            event = json.loads(raw)
            if not isinstance(event, dict):
                raise ValueError()
        except ValueError:
            # Router/CLI status lines: kept for /trace, shown only when they say something useful
            text = ESCAPES.sub('', raw)
            job['events'].append({'type': 'status', 'text': text})
            if not NOISE.search(text):
                view.note(ui.short(text, ui.width() - 4))
            return
        kind = event.get('type', '')
        if kind in ('reasoning', 'thinking') or event.get('item', {}).get('type') == 'reasoning':
            return
        if kind == 'assistant':
            message = event.get('message', {})
            message['content'] = [block for block in message.get('content', []) if block.get('type') not in ('thinking', 'redacted_thinking')]
        job['events'].append(event)
        if kind == 'smart.route':
            job['route'] = {'label': event.get('label'), 'by': event.get('by')}
            view.note(tr('route_' + ('complex' if event.get('label') == 'complex' else 'simple'),
                         by=tr('by_rules') if event.get('by') == 'rules' else str(event.get('by'))))
        elif kind == 'smart.provider':
            job['provider'] = event['provider']
            job['answers'] = []
            job['protocol_error'] = False
            job.setdefault('dispatches', []).append({'provider': event['provider'], 'profile': event.get('profile'),
                                                    'sent': job['sent'] if event['provider'] != 'openrouter' else None})
            view.provider(event['provider'], model_label(event['provider'], event.get('profile', '')))
        elif kind == 'smart.request':
            job['payload'] = event['data']
            if job.get('dispatches'):
                job['dispatches'][-1]['sent'] = event['data']
            view.note('dados pessoais mascarados antes do envio · /sent para ver')
        elif kind == 'smart.delta':
            # Stream the answer line by line as the tokens arrive (OpenRouter).
            job['stream_buf'] = job.get('stream_buf', '') + event.get('text', '')
            job['streamed'] = True
            while '\n' in job['stream_buf']:
                line, job['stream_buf'] = job['stream_buf'].split('\n', 1)
                view.stream_line(line)
        elif kind == 'smart.answer':
            job['model'] = event.get('model')
            spend(job, event.get('usage') or {})
            if job.get('streamed'):
                if job.get('stream_buf'):
                    view.stream_line(job['stream_buf'])
                    job['stream_buf'] = ''
                # the streamed lines were display-only; keep the full, unmasked answer for the session
                job['answers'] = [event.get('content', '')]
            else:
                self.answer(job, event.get('content', ''))
            view.note('modelo: ' + str(event.get('model', '?')))
        elif kind in ('item.started', 'item.completed'):
            item = event.get('item', {})
            item_type = item.get('type', '')
            done = kind == 'item.completed'
            if item_type == 'agent_message' and done:
                self.answer(job, item.get('text', ''))
            elif item_type == 'command_execution':
                if not done:
                    view.action('Ran', item.get('command', ''))
                else:
                    view.result(item.get('aggregated_output', ''), failed=item.get('exit_code') not in (0, None))
            elif item_type == 'file_change' and done:
                view.action('Edited', ', '.join(ui.home_path(c.get('path', '')) for c in item.get('changes', [])))
            elif item_type == 'mcp_tool_call':
                if not done:
                    view.action('Called', f"{item.get('server', '')} · {item.get('tool', '')}")
                else:
                    view.result(json.dumps(item.get('result') or item.get('error') or '', ensure_ascii=False),
                                failed=item.get('status') == 'failed')
            elif item_type == 'web_search' and not done:
                view.action('Searched web', item.get('query', ''))
        elif kind == 'assistant':
            for block in event.get('message', {}).get('content', []):
                if block.get('type') == 'text':
                    self.answer(job, block.get('text', ''))
                elif block.get('type') == 'tool_use':
                    view.action(*describe_tool(block.get('name', ''), block.get('input')))
        elif kind == 'user':
            # Claude tool results: one short line under the action they belong to
            for block in event.get('message', {}).get('content', []) or []:
                if isinstance(block, dict) and block.get('type') == 'tool_result':
                    content = block.get('content')
                    if isinstance(content, list):
                        content = '\n'.join(c.get('text', '') for c in content if isinstance(c, dict))
                    view.result(content or '', failed=bool(block.get('is_error')))
        elif kind == 'result':
            # Claude reports a cost even on a subscription; it is only billed on an API key
            if str(job.get('dispatches', [{}])[-1].get('profile') or '').startswith('api-key'):
                job['cost'] = event.get('total_cost_usd')
            spend(job, event.get('usage') or {})
            if event.get('is_error'):
                job['protocol_error'] = True
                view.error(str(event.get('result') or 'erro na ferramenta'))
            elif event.get('result') and not job['answers']:
                self.answer(job, event['result'])
        elif kind == 'turn.completed':
            spend(job, event.get('usage') or {})
        elif kind in ('error', 'turn.failed'):
            job['protocol_error'] = True
            detail = event.get('message') or event.get('error', {})
            view.error(detail.get('message', str(detail)) if isinstance(detail, dict) else str(detail))

    def answer(self, job, content):
        if content and content.strip():
            job['answers'].append(content)
            job['view'].answer(content)

def execute(session, tasks, parallel=False, raw=False):
    runner = Runner(session.data['cwd'], mode=session.data.get('mode', 'default'))
    ACTIVE.add(runner)
    jobs = []
    for engine, prompt in tasks:
        job = {'id': 'agente-' + str(len(session.data['requests']) + 1), 'engine': engine,
               'prompt': prompt, 'sent': prompt if raw else session.context(prompt), 'status': 'running',
               'created': time.time()}
        if parallel:
            job['sent'] += '\n\nSubtarefa delegada: faça somente análise, sem alterar arquivos. Entregue evidências e conclusões.'
        session.data['requests'].append(job)
        jobs.append(job)
    if not session.data.get('title'):
        # First question names the conversation (tab title, /sessions)
        session.data['title'] = ui.short(tasks[0][1].split('\n')[0], 48)
        ui.set_title(f"Omni AI · {session.data['title']}")
    session.data['messages'].append({'role': 'user', 'content': '\n'.join(prompt for _, prompt in tasks)})
    session.save()
    # Run even single requests in a worker so Ctrl-C can terminate all child groups.
    started = time.time()
    ui.SPINNER.start(tr('working'))
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=len(jobs))
    futures = [pool.submit(runner.run, job, job['prompt'], parallel) for job in jobs]
    try:
        for future in futures:
            future.result()
    except KeyboardInterrupt:
        ui.SPINNER.stop()
        ui.out(ui.yellow('  ' + tr('cancelling')))
        runner.cancel()
        for future in futures:
            try:
                future.result(timeout=5)
            except concurrent.futures.TimeoutError:
                with runner.lock:
                    children = list(runner.children)
                for child in children:
                    kill_tree(child, force=True)
    finally:
        pool.shutdown(wait=True)
        ACTIVE.discard(runner)
        ui.SPINNER.stop()
        for job in jobs:
            prefix = f"[{job['id']}] " if parallel else ''
            if job['status'] == 'done':
                ui.out('\n' + ui.gradient(ui.MARK) + ' ' + ui.dim(f"{prefix}{tr('took', t=ui.elapsed(started))} · {job.get('provider', job['engine'])}{spend_label(job)}"))
            elif job['status'] == 'cancelled':
                ui.out('\n' + ui.yellow(f"{prefix}{ui.MARK} {tr('job_cancelled')}"))
            else:
                ui.out('\n' + ui.red(f"{prefix}✗ {tr('job_failed')}"))
        for job in jobs:
            record_spend(job)
            job.pop('view', None)
            if job.get('answers') and job['status'] == 'done':
                session.data['messages'].append({'role': 'assistant', 'content': '\n'.join(job['answers']),
                                                'agent': job['id'], 'provider': job.get('provider', job['engine'])})
        session.save()
        if time.time() - started > 20:
            # Long run finished: ring the bell and mark the tab, so it's noticed from another window
            ui.attention(f"{ui.MARK} {tr('took', t=ui.elapsed(started))} · Omni AI · {session.data.get('title') or session.id}")
