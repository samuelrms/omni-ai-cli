"""Regression tests for the hardening fixes from the code review.

Each test pins a specific bug that was fixed, so a future change that reintroduces it fails here.
"""
import io
import unittest
from unittest.mock import patch

import tempfile
from pathlib import Path

from omni_ai import cli, config, export, skills, usage
from omni_ai.pii import PIIMasker


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


class PiiIpMasking(unittest.TestCase):
    def test_public_ips_are_masked(self):
        m = PIIMasker()
        for ip in ('10.0.0.1', '8.8.8.8', '1.2.3.4', '192.168.1.1'):
            masked, _ = m.mask(f'host {ip} here')
            self.assertNotIn(ip, masked, ip)

    def test_localhost_is_kept(self):
        masked, _ = PIIMasker().mask('bind 127.0.0.1 now')
        self.assertIn('127.0.0.1', masked)

    def test_report_keeps_full_category(self):
        report = PIIMasker().get_report('mail a@b.com')
        self.assertIn('EMAIL', report['categories'])


class SkillsArgSubstitution(unittest.TestCase):
    def test_ten_plus_args_not_mangled(self):
        entry = {'codex': ('command', __file__, 'usuário')}
        with patch.object(skills.Path, 'read_text', return_value='run $10 and $1'):
            text, _ = skills.build_prompt('x', ' '.join(str(i) for i in range(1, 12)), 'codex', entry)
        # $10 -> the 10th arg ("10"), not arg1 + "0"
        self.assertIn('run 10 and 1', text)


class UpdateJsonLocked(unittest.TestCase):
    def test_read_modify_write(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / 'state.json'
            config.update_json(p, lambda data: data.setdefault('a', {}).__setitem__('x', 1))
            config.update_json(p, lambda data: data.setdefault('a', {}).__setitem__('y', 2))
            self.assertEqual(config.read_json(p, {}), {'a': {'x': 1, 'y': 2}})

    def test_concurrent_writers_keep_all(self):
        import threading
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / 's.json'
            config.write_json(p, {'n': {}})

            def worker(i):
                config.update_json(p, lambda data: data['n'].__setitem__(str(i), i))
            threads = [threading.Thread(target=worker, args=(i,)) for i in range(20)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            self.assertEqual(len(config.read_json(p, {})['n']), 20)


class UsageNumberCoercion(unittest.TestCase):
    def test_f_tolerates_bad_values(self):
        self.assertEqual(usage._f('50'), 50.0)
        self.assertEqual(usage._f(None), 0.0)
        self.assertEqual(usage._f('abc', 9.0), 9.0)


if __name__ == '__main__':
    unittest.main()
