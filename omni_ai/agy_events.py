"""Antigravity's stream-json has its own shape; this turns it into something the chat and the server share."""
import json


def is_agy_event(event):
    return 'event' in event and 'type' not in event


def parse(event):
    """One of: ('answer', text, usage), ('error', text), ('tool_start', name, parameters),
    ('tool_done', output, failed), or None for events that carry nothing to act on."""
    kind = event.get('event')
    if kind == 'result' and isinstance(event.get('result'), dict):
        result = event['result']
        if result.get('status') == 'SUCCESS':
            return 'answer', str(result.get('response') or '').strip(), result.get('usage')
        return 'error', str(result.get('error') or result.get('message') or result.get('status') or 'agy failed')
    if kind == 'error':
        return 'error', str(event.get('message') or json.dumps(event, ensure_ascii=False))
    step = event.get('step_update')
    if kind == 'step_update' and isinstance(step, dict) and step.get('step_type') == 'tool':
        info = step.get('tool_info') if isinstance(step.get('tool_info'), dict) else {}
        if step.get('state') == 'ACTIVE':
            return 'tool_start', str(step.get('tool_name') or info.get('name') or ''), info.get('parameters')
        return 'tool_done', str(info.get('output') or ''), step.get('state') != 'DONE'
    return None
