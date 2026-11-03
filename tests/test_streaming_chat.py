import os
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from omni_ai import chat  # noqa: E402


class FakeView:
    def __init__(self):
        self.lines = []
        self.answers = []
        self.notes = []

    def stream_line(self, line):
        self.lines.append(line)

    def answer(self, text):
        self.answers.append(text)

    def note(self, text):
        self.notes.append(text)


class StreamingChatTests(unittest.TestCase):
    """OpenRouter deltas stream line by line; the final answer is not printed twice."""

    def job(self):
        return {'id': 'agente-1', 'events': [], 'answers': [], 'view': FakeView()}

    def test_deltas_stream_line_by_line_and_answer_not_duplicated(self):
        runner = chat.Runner('.')
        job = self.job()
        runner.event(job, '{"type": "smart.delta", "text": "first line\\nsecond "}')
        runner.event(job, '{"type": "smart.delta", "text": "half"}')
        runner.event(job, '{"type": "smart.answer", "content": "first line\\nsecond half", "model": "m:free"}')
        view = job['view']
        self.assertEqual(view.lines, ['first line', 'second half'])  # last line flushed on answer
        self.assertEqual(view.answers, [])                            # streamed, so answer() not called again
        self.assertEqual(job['answers'], ['first line\nsecond half'])  # full answer kept for the session

    def test_non_streaming_answer_still_prints_once(self):
        runner = chat.Runner('.')
        job = self.job()
        runner.event(job, '{"type": "smart.answer", "content": "whole answer", "model": "m:free"}')
        self.assertEqual(job['view'].answers, ['whole answer'])
        self.assertEqual(job['view'].lines, [])


if __name__ == '__main__':
    unittest.main()
