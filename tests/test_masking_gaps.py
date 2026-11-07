"""Common personal data and credentials are masked; ordinary text and code are left alone."""
import isolation  # noqa: F401  (first: keeps the tests away from real data)
import json
import time
import unittest
from unittest.mock import patch

from omni_ai import grok
from omni_ai.pii import PIIMasker

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
]


class Masks(unittest.TestCase):
    def test_sensitive_text_does_not_go_out(self):
        for text, secret in SENSITIVE:
            with self.subTest(text=text):
                masked, mapping = PIIMasker().mask(text)
                self.assertNotIn(secret, masked)
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
