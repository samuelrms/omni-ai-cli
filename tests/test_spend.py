import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from omni_ai import chat, cli  # noqa: E402


class SpendTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.log = Path(temp.name) / 'usage.log'
        patcher = patch.dict(os.environ, {'SMART_AI_LOG': str(self.log)})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_tokens_and_provider_cost(self):
        job = {}
        chat.spend(job, {'prompt_tokens': 800, 'completion_tokens': 700, 'cost': 0.0012})
        chat.spend(job, {'input_tokens': 10, 'output_tokens': 5})
        self.assertEqual(job['tokens'], 1515)
        self.assertAlmostEqual(job['cost'], 0.0012)
        self.assertEqual(chat.spend_label(job), ' · US$ 0.0012 · 1.5k tokens')

    def test_xai_ticks(self):
        job = {}
        chat.spend(job, {'prompt_tokens': 3, 'cost_in_usd_ticks': 25_000_000})
        self.assertAlmostEqual(job['cost'], 0.0025)
        self.assertEqual(chat.spend_label(job), ' · US$ 0.0025 · 3 tokens')

    def test_nothing_known_shows_nothing(self):
        job = {}
        chat.spend(job, {})
        self.assertEqual(chat.spend_label(job), '')
        chat.record_spend(dict(job, engine='codex'))
        self.assertFalse(self.log.with_name('costs.jsonl').exists())

    def test_claude_cost_counts_only_on_an_api_key(self):
        runner = chat.Runner('.')
        result = json.dumps({'type': 'result', 'total_cost_usd': 0.5, 'result': 'ok', 'usage': {'input_tokens': 4}})
        for profile, billed in (('primary', None), ('api-key', 0.5)):
            job = {'id': 'a', 'sent': 'x', 'events': [], 'answers': [], 'dispatches': [{'profile': profile}],
                   'view': type('V', (), {'answer': lambda *_: None, 'note': lambda *_: None})()}
            runner.event(job, result)
            self.assertEqual(job.get('cost'), billed)
            self.assertEqual(job['tokens'], 4)

    def test_day_total_in_usage(self):
        today = time.strftime('%Y-%m-%d')
        chat.record_spend({'engine': 'auto', 'provider': 'openrouter', 'cost': 0.001, 'tokens': 100})
        chat.record_spend({'engine': 'auto', 'provider': 'openrouter', 'cost': 0.002, 'tokens': 50})
        chat.record_spend({'engine': 'codex', 'tokens': 7})
        with open(self.log.with_name('costs.jsonl'), 'a') as stream:
            stream.write('{"at": "1999-01-01 00:00:00", "tool": "codex", "cost": 9, "tokens": 9}\nnot json\n')
        with patch.object(cli, 'LOG_FILE', self.log):
            totals = cli.spend_today(today)
        self.assertEqual(set(totals), {'openrouter', 'codex'})
        self.assertAlmostEqual(totals['openrouter'][0], 0.003)
        self.assertEqual(totals['openrouter'][1:], (150, 2))
        self.assertEqual(totals['codex'], (0.0, 7, 1))  # yesterday's row and the broken line are ignored

    def test_saved_rows_have_no_prompt_or_answer(self):
        chat.record_spend({'engine': 'auto', 'provider': 'grok', 'cost': 0.1, 'tokens': 1, 'prompt': 'secret', 'answers': ['secret']})
        self.assertNotIn('secret', self.log.with_name('costs.jsonl').read_text())


if __name__ == '__main__':
    unittest.main()
