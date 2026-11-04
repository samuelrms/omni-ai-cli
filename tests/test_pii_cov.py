"""Branch coverage for omni_ai.pii: masking every category, unmask round-trips,
false positives, reporting and the CLI. All in-process, no files outside temp dirs.
"""
import io
import json
from pathlib import Path
import re
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from omni_ai import pii  # noqa: E402


class MaskCategoryTests(unittest.TestCase):
    def setUp(self):
        self.m = pii.PIIMasker()

    def roundtrip(self, text):
        masked, mapping = self.m.mask(text, namespace='ns')
        self.assertEqual(self.m.unmask(masked, mapping), text)
        return masked, mapping

    def test_email(self):
        masked, mapping = self.roundtrip('Fale com joao.silva@example.com agora')
        self.assertNotIn('joao.silva@example.com', masked)
        self.assertIn('EMAIL', ''.join(mapping))

    def test_api_keys_and_jwt(self):
        text = ('key sk-ant-abcdefghijklmnopqrstuvwx and ghp_' + 'a' * 36 +
                ' jwt eyJabcdefghij.klmnopqrstuv.wxyzabcdefg')
        masked, mapping = self.roundtrip(text)
        self.assertIn('API_KEY', ''.join(mapping))
        self.assertNotIn('sk-ant-abcdefghijklmnopqrstuvwx', masked)

    def test_secret_assignment(self):
        masked, mapping = self.roundtrip('password = "hunter2secret"')
        self.assertTrue(any('SECRET' in k for k in mapping))

    def test_brazilian_documents(self):
        masked, mapping = self.roundtrip('CPF 123.456.789-00 CNPJ 12.345.678/0001-99')
        keys = ''.join(mapping)
        self.assertIn('CPF', keys)
        self.assertIn('CNPJ', keys)

    def test_ssn_and_credit_card(self):
        masked, mapping = self.roundtrip('SSN 123-45-6789 card 4111 1111 1111 1111')
        keys = ''.join(mapping)
        self.assertIn('SSN', keys)
        self.assertIn('CREDIT_CARD', keys)

    def test_phone_numbers(self):
        _, mapping = self.roundtrip('ligue +55 11 98765-4321')
        self.assertTrue(any('PHONE' in k for k in mapping))

    def test_ipv4_real_and_local_false_positive(self):
        masked, mapping = self.m.mask('server 203.0.113.45 local 127.0.0.1', namespace='ns')
        self.assertIn('203.0.113.45', ' '.join(mapping.values()))
        self.assertIn('127.0.0.1', masked)  # local IP left untouched

    def test_ipv6(self):
        _, mapping = self.roundtrip('addr 2001:0db8:85a3:0000:0000:8a2e:0370:7334 ok')
        self.assertTrue(any('IPV6' in k for k in mapping))

    def test_url_with_creds_and_conn_string(self):
        text = 'visit https://user:pass@host.com/x and mongodb://u:p@db:27017/app'
        masked, mapping = self.roundtrip(text)
        keys = ''.join(mapping)
        self.assertIn('URL_WITH_CREDS', keys)
        self.assertIn('CONN_STRING', keys)

    def test_file_path_with_username(self):
        _, mapping = self.roundtrip('log at /Users/joao/project/file.txt here')
        self.assertTrue(any('FILE_PATH' in k for k in mapping))

    def test_person_name_in_context(self):
        _, mapping = self.roundtrip('nome: Maria Souza concluiu')
        self.assertTrue(any('PERSON_NAME' in k for k in mapping))

    def test_cep_and_short_number_false_positive(self):
        masked, mapping = self.m.mask('CEP 01310-100 version 1.2.3', namespace='ns')
        self.assertTrue(any('CEP' in k for k in mapping))
        self.assertIn('1.2.3', masked)

    def test_private_key_block(self):
        text = '-----BEGIN RSA PRIVATE KEY-----\nMIIabc\n-----END RSA PRIVATE KEY-----'
        _, mapping = self.roundtrip(text)
        self.assertTrue(any('PRIVATE_KEY' in k for k in mapping))


class MaskBehaviourTests(unittest.TestCase):
    def setUp(self):
        self.m = pii.PIIMasker()

    def test_namespace_is_deterministic_when_given(self):
        a, _ = self.m.mask('a@b.com', namespace='fixed')
        b, _ = self.m.mask('a@b.com', namespace='fixed')
        self.assertEqual(a, b)

    def test_auto_namespace_present(self):
        masked, mapping = self.m.mask('a@b.com')
        self.assertTrue(mapping)
        # placeholder embeds a generated namespace token
        self.assertRegex(next(iter(mapping)), r'\[EMAIL_[0-9a-f]+_1\]')

    def test_unmask_empty_mapping_returns_text(self):
        self.assertEqual(self.m.unmask('nothing here', {}), 'nothing here')

    def test_mask_for_log_is_irreversible(self):
        masked = self.m.mask_for_log('email a@b.com')
        self.assertNotIn('a@b.com', masked)

    def test_get_report_counts_categories(self):
        report = self.m.get_report('a@b.com and c@d.com and CPF 123.456.789-00')
        self.assertEqual(report['total_found'], 3)
        self.assertEqual(report['categories'].get('EMAIL'), 2)
        self.assertEqual(report['categories'].get('CPF'), 1)

    def test_extra_patterns_extend_detection(self):
        extra = [('TICKET', re.compile(r'TICK-\d+'))]
        masker = pii.PIIMasker(extra_patterns=extra)
        masked, mapping = masker.mask('open TICK-42 now', namespace='ns')
        self.assertTrue(any('TICKET' in k for k in mapping))

    def test_overlapping_matches_do_not_double_mask(self):
        # a connection string contains an embedded credential; only one placeholder wins the span
        text = 'postgres://user:password@host:5432/db'
        masked, mapping = self.m.mask(text, namespace='ns')
        self.assertEqual(self.m.unmask(masked, mapping), text)
        self.assertEqual(masked.count('['), 1)

    def test_clean_text_unchanged(self):
        text = 'just a normal sentence with no secrets'
        masked, mapping = self.m.mask(text, namespace='ns')
        self.assertEqual(masked, text)
        self.assertEqual(mapping, {})


class CliTests(unittest.TestCase):
    def run_cli(self, argv, stdin_text=''):
        out, err = io.StringIO(), io.StringIO()
        with patch.object(sys, 'argv', argv), \
                patch.object(sys, 'stdin', io.StringIO(stdin_text)), \
                patch.object(sys, 'stdout', out), patch.object(sys, 'stderr', err):
            pii.main()
        return out.getvalue(), err.getvalue()

    def test_mask_with_map_out_and_report(self):
        with tempfile.TemporaryDirectory() as d:
            mapfile = Path(d) / 'map.json'
            out, err = self.run_cli(['pii', 'mask', '--map-out', str(mapfile), '--report'],
                                     stdin_text='email a@b.com')
            self.assertNotIn('a@b.com', out)
            self.assertIn('PII Report', err)
            saved = json.loads(mapfile.read_text())
            self.assertTrue(any('EMAIL' in k for k in saved))

    def test_unmask_uses_map(self):
        with tempfile.TemporaryDirectory() as d:
            mapfile = Path(d) / 'map.json'
            mapfile.write_text(json.dumps({'[EMAIL_ns_1]': 'a@b.com'}))
            out, _ = self.run_cli(['pii', 'unmask', '--map', str(mapfile)],
                                  stdin_text='contact [EMAIL_ns_1] please')
            self.assertIn('a@b.com', out)

    def test_report_command(self):
        out, _ = self.run_cli(['pii', 'report'], stdin_text='a@b.com')
        self.assertIn('total_found', out)

    def test_no_command_prints_help(self):
        out, _ = self.run_cli(['pii'])
        self.assertIn('usage', out.lower())


if __name__ == '__main__':
    unittest.main()
