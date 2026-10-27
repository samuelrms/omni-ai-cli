import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from omni_ai import chat, cli  # noqa: E402


class RouteReasonTests(unittest.TestCase):
    """Automatic routing says why it picked a tool, and the chat shows it in one line."""

    def route(self, prompt, llm=None, provider='groq'):
        events = []

        def classify_llm(_):
            if llm:
                cli.CLASSIFIED_BY[0] = provider
            return llm
        fakes = {name: (lambda _: 0) for name in cli.runners()}

        def real_claude(_):
            raise AssertionError('the real Claude must never run in tests')
        with patch.object(cli, 'claude_with_fallback', side_effect=real_claude), \
                patch.object(cli, 'classify_llm', side_effect=classify_llm), patch.object(cli, 'emit', side_effect=events.append), \
                patch.object(cli, 'runners', return_value=fakes), patch.object(cli, 'say'), \
                patch.dict(os.environ, {'SMART_AI_CLASSIFY_PROMPT': ''}):
            cli.route(prompt)
        return [e for e in events if e.get('type') == 'smart.route']

    def test_classifier_service_is_named(self):
        self.assertEqual(self.route('explain REST', llm='simple', provider='cerebras'),
                         [{'type': 'smart.route', 'label': 'simple', 'by': 'cerebras'}])

    def test_keyword_rules_when_no_service_answers(self):
        self.assertEqual(self.route('refatorar o módulo de pagamentos'),
                         [{'type': 'smart.route', 'label': 'complex', 'by': 'rules'}])

    def test_forced_tool_has_no_route_line(self):
        with patch.object(cli, 'runners', return_value={'codex': lambda _: 0}), patch.object(cli, 'emit') as emit:
            cli.route('anything', 'codex')
        emit.assert_not_called()

    def test_chat_shows_the_reason(self):
        chat.set_lang('pt')
        job = {'id': 'agente-1', 'sent': 'x', 'events': [], 'answers': []}
        notes = []
        job['view'] = type('View', (), {'note': lambda self, text: notes.append(text)})()
        runner = chat.Runner('.')
        runner.event(job, '{"type": "smart.route", "label": "simple", "by": "groq"}')
        runner.event(job, '{"type": "smart.route", "label": "complex", "by": "rules"}')
        self.assertEqual(job['route'], {'label': 'complex', 'by': 'rules'})
        self.assertIn('groq', notes[0])
        self.assertIn('palavras-chave', notes[1])
        self.assertIn('Claude', notes[1])


if __name__ == '__main__':
    unittest.main()
