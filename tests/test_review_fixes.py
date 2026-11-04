"""Regression tests for the hardening fixes from the code review.

Each test pins a specific bug that was fixed, so a future change that reintroduces it fails here.
"""
import io
import unittest
from unittest.mock import patch

from omni_ai import cli, export


class TailHitLimit(unittest.TestCase):
    def test_rate_limit_only_counts_in_the_tail(self):
        # A "rate limit" mention buried early in tool output must NOT trigger cooldown.
        noisy = "the review notes a rate limit in the code\n" + "\n".join(["ok"] * 10)
        self.assertFalse(cli.tail_hit_limit(noisy))

    def test_rate_limit_in_final_error_counts(self):
        output = "\n".join(["ran fine"] * 3 + ["Error: you hit your usage limit, try again at 7pm"])
        self.assertTrue(cli.tail_hit_limit(output))

    def test_empty_output(self):
        self.assertFalse(cli.tail_hit_limit(""))


class DoctorWordNotHijacked(unittest.TestCase):
    def test_doctor_inside_a_prompt_is_not_the_health_check(self):
        # `ai how to become a doctor` must go to route(), not run the doctor() health check.
        with patch.object(cli, 'doctor') as doc, patch.object(cli, 'route', return_value=0) as route:
            cli.main(['how', 'to', 'become', 'a', 'doctor'])
        doc.assert_not_called()
        route.assert_called_once()

    def test_bare_doctor_still_runs_the_health_check(self):
        with patch.object(cli, 'doctor', return_value=0) as doc:
            self.assertEqual(cli.main(['doctor']), 0)
        doc.assert_called_once()


class ExportFlagNeedsValue(unittest.TestCase):
    def test_missing_from_value_is_a_clean_error(self):
        err = io.StringIO()
        with patch('sys.stderr', err):
            self.assertEqual(export.main(['--print', '--from']), 2)
        self.assertIn('precisa de um valor', err.getvalue())


class PdfLatinWarning(unittest.TestCase):
    def test_non_latin_chars_warn(self):
        conv = export.normalize({'messages': [{'role': 'user', 'content': 'olá 🚀 世界'}]})
        err = io.StringIO()
        import tempfile, os
        out = os.path.join(tempfile.mkdtemp(), 'x.pdf')
        with patch('sys.stderr', err):
            export.to_pdf(conv, out)
        self.assertIn('Latin-1', err.getvalue())

    def test_plain_latin_does_not_warn(self):
        conv = export.normalize({'messages': [{'role': 'user', 'content': 'olá, tudo bem?'}]})
        err = io.StringIO()
        import tempfile, os
        out = os.path.join(tempfile.mkdtemp(), 'x.pdf')
        with patch('sys.stderr', err):
            export.to_pdf(conv, out)
        self.assertEqual(err.getvalue(), '')


if __name__ == '__main__':
    unittest.main()
