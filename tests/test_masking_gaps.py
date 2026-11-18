"""Common personal data and credentials are masked; ordinary text and code are left alone."""
import isolation  # noqa: F401  (first: keeps the tests away from real data)
import json
import re
import time
import unittest
from unittest.mock import patch

from omni_ai import grok
from omni_ai.pii import PIIMasker
from omni_ai.pii.masker import valid_cnpj, valid_cpf

# The placeholder carries a random hex id, so a short secret such as 'abc'
# can show up inside it by chance; only the text around placeholders counts.
PLACEHOLDER = re.compile(r'\[[A-Z_]+_[0-9a-f]+_\d+\]')

# (text, what must not be sent)
SENSITIVE = [
    ('senha: x', 'x'),
    ('Senha = minhaSenhaForte', 'minhaSenhaForte'),
    ('chave: abc12345xyz', 'abc12345xyz'),
    ('password: "my long pass"', 'my long pass'),
    ('Authorization: Bearer abc.def-ghi_123', 'abc.def-ghi_123'),
    ('authorization: Basic dXNlcjpwYXNz', 'dXNlcjpwYXNz'),
    ('curl -H "Bearer abcdefgh12345678"', 'abcdefgh12345678'),
    ('{"password": "hunter2"}', 'hunter2'),
    ("{'api_key': 'abcd1234efgh'}", 'abcd1234efgh'),
    ('{"token":"abcdefgh12345678"}', 'abcdefgh12345678'),
    ('aws_secret_access_key = wJalrXUtnFEMIK7MDENGbPxRfiCYEXAMPLEKEY', 'wJalrXUtnFEMIK7MDENGbPxRfiCYEXAMPLEKEY'),
    ('my_secret_value=abcdefgh1234', 'abcdefgh1234'),
    ('key xai-abcdefghijklmnopqrstuvwxyz123456', 'xai-abcdefghijklmnopqrstuvwxyz123456'),
    ('key gsk_abcdefghijklmnopqrstuvwxyz123456', 'gsk_abcdefghijklmnopqrstuvwxyz123456'),
    ('key csk-abcdefghijklmnopqrstuvwxyz123456', 'csk-abcdefghijklmnopqrstuvwxyz123456'),
    ('key npm_abcdefghijklmnopqrstuvwxyz1234567890', 'npm_abcdefghijklmnopqrstuvwxyz1234567890'),
    ('key sk-or-v1-abcdefghijklmnopqrstuvwxyz1234567890', 'sk-or-v1-abcdefghijklmnopqrstuvwxyz1234567890'),
    ('key sk-abcdefghijklmnopqrst1234567890', 'sk-abcdefghijklmnopqrst1234567890'),
    ('key ghs_abcdefghijklmnopqrstuvwxyz1234567890', 'ghs_abcdefghijklmnopqrstuvwxyz1234567890'),
    ('key ghp_abcdefghijklmnopqrstuvwxyz1234567890', 'ghp_abcdefghijklmnopqrstuvwxyz1234567890'),
    ('key AKIAIOSFODNN7EXAMPLE', 'AKIAIOSFODNN7EXAMPLE'),
    ('key ASIAIOSFODNN7EXAMPLE', 'ASIAIOSFODNN7EXAMPLE'),
    ('-----BEGIN ENCRYPTED PRIVATE KEY-----\nabc\n-----END ENCRYPTED PRIVATE KEY-----', 'abc'),
    ('-----BEGIN PGP PRIVATE KEY BLOCK-----\nabc\n-----END PGP PRIVATE KEY BLOCK-----', 'abc'),
    ('-----BEGIN RSA PRIVATE KEY-----\nabc\ndef', 'abc'),
    ('mail joão.silva@exemplo.com.br', 'joão.silva@exemplo.com.br'),
    ('mail ana@ação.com.br', 'ana@ação.com.br'),
    ('RG: 12.345.678-9', '12.345.678-9'),
    ('RG 12345678X', '12345678X'),
    ('PIS: 123.45678.90-1', '123.45678.90-1'),
    ('call (415) 555-2671', '555-2671'),
    ('call 415-555-2671', '555-2671'),
    ('fone +44 20 7946 0958', '7946 0958'),
    ('fone +1 415 555 2671', '555 2671'),
    ('fone +351 912 345 678', '912 345 678'),
    ('card 3782 822463 10005', '822463'),
    ('card 3782-822463-10005', '822463'),
    ('ip 2001:db8::8a2e:370:7334', '2001:db8::8a2e:370:7334'),
    ('ip fe80::1', 'fe80::1'),
    ('ip 2001:0db8:85a3:0000:0000:8a2e:0370:7334', '2001:0db8:85a3:0000:0000:8a2e:0370:7334'),
    ('CPF 280.012.389-38', '280.012.389-38'),
    ('cpf 12345678909', '12345678909'),
    ('doc 111.111.111-11', '111.111.111-11'),
    ('CNPJ 90.021.382/0001-22', '90.021.382/0001-22'),
    ('cnpj 00000000000191', '00000000000191'),
    ('CNPJ 12.ABC.345/01DE-35', '12.ABC.345/01DE-35'),
    ('cnpj 12ABC34501DE35', '12ABC34501DE35'),
    ('cnpj AA345678/000A-29', 'AA345678/000A-29'),
    ('cnpj 12abc34501de35', '12abc34501de35'),
    ('cpf 280 012 389 38', '280 012 389 38'),
    ('cnpj 90.021.382\\0001-22', '90.021.382\\0001-22'),
    ('cnpj 90 021 382/0001 22', '90 021 382/0001 22'),
    ('cpf １２３.４５６.７８９-０９', '１２３.４５６.７８９-０９'),
]
# Text that must reach the model unchanged
ORDINARY = [
    'password: string',
    'password: null',
    'token: None',
    'auth: true',
    'the secret: tell me',
    'const chave = 1',
    'std::vector<int> a; a::b::c',
    'Foo::bar()',
    'at 12:34:56 it ran',
    'version 1.2.3',
    'localhost ::1 127.0.0.1',
    'bearer of bad news',
    'the sk-learn package',
    'npm_config_cache is a variable',
    'a=b c:d',
    'mac aa:bb',
    'def f(x): return x',
    'order 123.456.789-00 shipped',
    'ref 12.345.678/0001-99',
    'code 12ABC34501DE36',
    'zeros 000.000.000-00 and 00.000.000/0000-00',
    'path 12.abc.345/01de-35',
]


class Masks(unittest.TestCase):
    def test_sensitive_text_does_not_go_out(self):
        for text, secret in SENSITIVE:
            with self.subTest(text=text):
                masked, mapping = PIIMasker().mask(text)
                self.assertNotIn(secret, PLACEHOLDER.sub('', masked))
                self.assertEqual(PIIMasker().unmask(masked, mapping), text)

    def test_ordinary_text_is_unchanged(self):
        for text in ORDINARY:
            with self.subTest(text=text):
                self.assertEqual(PIIMasker().mask(text)[0], text)

    def test_brazilian_phones_stay_in_their_category(self):
        masked, _ = PIIMasker().mask('fone +55 11 91234-5678')
        self.assertIn('[PHONE_BR_', masked)

    def test_long_text_is_read_quickly(self):
        for text in ('a.' * 200_000, 'secret' * 100_000, 'a:' * 200_000, 'x@' * 200_000, '-' * 400_000):
            with self.subTest(text=text[:6]):
                started = time.monotonic()
                PIIMasker().mask(text)
                self.assertLess(time.monotonic() - started, 5)


class CheckDigits(unittest.TestCase):
    """The CPF vectors come from redactbr (github.com/GuilhermeMikin/redactbr), by the same author, who brings them
    here under MIT. The CNPJ ones are the reference vectors the Receita Federal publishes for the alphanumeric CNPJ."""

    def test_cpf(self):
        for digits in ('28001238938', '12345678909', '98765432100', '00000000191', '31415926590', '00000001406',
                       '00000000604', '00000001910', '00000001830', '11111111111', '99999999999'):
            with self.subTest(digits=digits):
                self.assertTrue(valid_cpf(digits))
        for digits in ('00000000000', '12345678919', '12345678908', '12345678990', '1234567890A', '123456789A9',
                       '123.4567890', '1234567890', '123456789090', '１２３４５６７８９０９'):
            with self.subTest(digits=digits):
                self.assertFalse(valid_cpf(digits))

    def test_cnpj(self):
        for chars in ('12ABC34501DE35', '1345C3A5000106', 'R55231B3000757', '90021382000122',
                      '90024778000123', '90025108000121', '90025255000100', '90024420000109', '90024781000147',
                      '04740714000197', '44108058000129', '90024780000100', '90024779000178', '00000000000191',
                      'ABCDEFGHIJKL80', '12345678000195', '12345678000A08', 'AA345678000A29'):
            with self.subTest(chars=chars):
                self.assertTrue(valid_cnpj(chars))
        # AA3456780003-29 is printed in the Receita's FAQ, but its algorithm gives 86 as the check digits
        for chars in ('R55231B3000700', '1345c3A5000106', '90025108000101', '00000000000192', 'ABCDEFGHIJKL81',
                      '0000000000019L', '000000000001P1', '00000000000000', 'AA345678000329'):
            with self.subTest(chars=chars):
                self.assertFalse(valid_cnpj(chars))

    def test_only_valid_numbers_take_the_category(self):
        for text, category in (('cpf 123.456.789-09', 'CPF'), ('cnpj 00000000000191', 'CNPJ'),
                               ('cnpj 12.ABC.345/01DE-35', 'CNPJ'), ('cnpj 12abc34501de35', 'CNPJ')):
            with self.subTest(text=text):
                self.assertIn(f'[{category}_', PIIMasker().mask(text)[0])
        # Until bare digits need formatting to be a phone, a number that fails the check is still masked as one
        masked = PIIMasker().mask('order 12345678901')[0]
        self.assertNotIn('[CPF_', masked)
        self.assertIn('[PHONE_BR_', masked)

    def test_an_email_name_with_a_cnpj_shape_stays_in_the_email(self):
        # pedroalves1988 has valid check digits as an alphanumeric CNPJ
        masked = PIIMasker().mask('email pedroalves1988@empresa.com.br')[0]
        self.assertIn('[EMAIL_', masked)
        self.assertNotIn('empresa.com.br', masked)


class SystemPrompt(unittest.TestCase):
    def test_the_system_prompt_is_masked_and_restored(self):
        sent = {}
        def request(_path, _key, data, **_options):
            sent.update(data)
            placeholder = data['messages'][0]['content'].split()[-1]
            return {'choices': [{'message': {'content': f'hello {placeholder}'}}], 'model': 'm', 'usage': {}}
        with patch.object(grok, 'request', request), patch('sys.stderr'), patch('sys.stdout'):
            result = grok.chat('key', 'hi', 'm', system_prompt='Write to ana@example.com')
        body = json.dumps(sent['messages'])
        self.assertNotIn('ana@example.com', body)
        self.assertIn('ana@example.com', json.dumps(result))


if __name__ == '__main__':
    unittest.main()
