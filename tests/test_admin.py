import isolation  # noqa: F401  (first: keeps the tests away from real data)
import json
import subprocess
import sys
from unittest.mock import patch
import unittest

from test_server import ROOT, ServerCase

sys.path.insert(0, str(ROOT))
from omni_ai.server import admin  # noqa: E402

ADMIN_KEY = 'omni-admin-synthetic'


class AdminCase(ServerCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.admin_env = {**cls.env, 'SMART_AI_ADMIN_KEY': ADMIN_KEY}
        cls.admin_server, cls.admin_port, _ = cls.start('--enable-admin', env=cls.admin_env)

    @classmethod
    def tearDownClass(cls):
        cls.admin_server.terminate()
        cls.admin_server.wait(timeout=10)
        cls.admin_server.stdout.close()
        super().tearDownClass()

    def admin(self, method, path, body=None, key=ADMIN_KEY, headers=None):
        response, text = self.call(method, path, body, key=key, port=self.admin_port, headers=headers)
        return response.status, json.loads(text) if text else None


class Access(AdminCase):
    def test_disabled_by_default(self):
        response, _ = self.call('GET', '/admin/health')
        self.assertEqual(response.status, 404)
        response, _ = self.call('GET', '/admin/health', key=ADMIN_KEY)
        self.assertEqual(response.status, 401)

    def test_regular_key_never_opens_admin(self):
        for method, path in (('GET', '/admin/health'), ('GET', '/admin/config'), ('PUT', '/admin/config'), ('GET', '/admin/keys'),
                             ('POST', '/admin/keys'), ('DELETE', '/admin/keys/openrouter'), ('GET', '/admin/api-keys')):
            self.assertEqual(self.admin(method, path, {}, key=self.key)[0], 401, path)

    def test_admin_key_never_opens_v1(self):
        self.assertEqual(self.admin('GET', '/v1/models')[0], 401)

    def test_no_key(self):
        self.assertEqual(self.admin('GET', '/admin/health', key='wrong')[0], 401)

    def test_refuses_to_start_without_its_own_key(self):
        out = subprocess.run([sys.executable, '-m', 'omni_ai.cli', 'serve', '--port', '0', '--enable-admin'], env=self.env,
                             capture_output=True, text=True, timeout=20)
        self.assertEqual(out.returncode, 2)
        self.assertIn('admin-keys add', out.stderr)

    def test_refuses_a_key_shared_with_v1(self):
        env = {**self.env, 'SMART_AI_ADMIN_KEY': self.key}
        out = subprocess.run([sys.executable, '-m', 'omni_ai.cli', 'serve', '--port', '0', '--enable-admin'], env=env,
                             capture_output=True, text=True, timeout=20)
        self.assertEqual(out.returncode, 2)

    def test_tools_never_see_the_admin_key(self):
        from omni_ai.server.execution import Execution
        with patch.dict('os.environ', {'SMART_AI_ADMIN_KEY': 'x', 'SMART_AI_ADMIN_KEY_FILE': 'y'}):
            env = Execution('codex', 'p', 'p')._env()
        self.assertNotIn('SMART_AI_ADMIN_KEY', env)
        self.assertNotIn('SMART_AI_ADMIN_KEY_FILE', env)


class CommandLine(AdminCase):
    def test_admin_keys_are_stored_apart_and_private(self):
        out = subprocess.run([sys.executable, '-m', 'omni_ai.cli', 'serve', 'admin-keys', 'add', 'ops'], env=self.env,
                             capture_output=True, text=True, timeout=20)
        self.assertIn('omni-admin-', out.stdout)
        path = self.root / 'cfg/admin_keys.json'
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        value = out.stdout.strip().splitlines()[-1]
        self.assertEqual(self.admin('GET', '/admin/health', key=value)[0], 200)
        self.assertEqual(self.admin('GET', '/v1/models', key=value)[0], 401)
        subprocess.run([sys.executable, '-m', 'omni_ai.cli', 'serve', 'admin-keys', 'remove', 'ops'], env=self.env, capture_output=True)
        self.assertEqual(self.admin('GET', '/admin/health', key=value)[0], 401)


class Https(unittest.TestCase):
    def check(self, address, proto='', allow=False):
        class Handler:
            client_address = (address, 1)
            headers = {'X-Forwarded-Proto': proto}
            class server:
                admin_allow_http = allow
        admin.require_https(Handler)

    def test_local_is_allowed(self):
        self.check('127.0.0.1')
        self.check('::ffff:127.0.0.1')

    def test_remote_needs_https(self):
        with self.assertRaises(admin.RequestError) as caught:
            self.check('10.0.0.5')
        self.assertEqual(caught.exception.status, 403)
        self.check('10.0.0.5', 'https')

    def test_explicit_opt_out(self):
        self.check('10.0.0.5', allow=True)


class Config(AdminCase):
    def test_read(self):
        status, body = self.admin('GET', '/admin/config')
        self.assertEqual(status, 200)
        self.assertIn('codex', body['models'])
        self.assertEqual(body['fallback_order'][0]['name'], 'main')

    def test_change_reaches_a_running_server(self):
        status, body = self.admin('PUT', '/admin/config', {'models': {'codex': 'other-model'}})
        self.assertEqual((status, body['models']['codex']), (200, 'other-model'))
        response, _ = self.call('POST', '/v1/chat/completions', {'model': 'omni/codex', 'messages': [{'role': 'user', 'content': 'hi'}]},
                                key=self.key, port=self.admin_port)
        self.assertEqual(response.status, 200)
        self.assertIn('other-model', self.log.read_text())

    def test_fallback_order_and_routing(self):
        status, body = self.admin('PUT', '/admin/config', {'fallback_order': {'main': {'priority': 5, 'enabled': False}},
                                                           'routing': {'usage_skip_percent': 90}})
        self.assertEqual(status, 200)
        self.assertEqual(body['fallback_order'][0], {'name': 'main', 'priority': 5, 'enabled': False})
        self.assertEqual(body['routing']['usage_skip_percent'], 90)
        self.admin('PUT', '/admin/config', {'fallback_order': {'main': {'priority': 1, 'enabled': True}}})

    def test_protected_fields(self):
        for body in ({'mode': 'yolo'}, {'permission_mode': 'yolo'}, {'codex_bin': '/x'}, {'config_dir': '/x'},
                     {'models': {'config_dir': '/x'}}, {'models': {'codex_bin': '/x'}}, {'routing': {'mode': 'yolo'}},
                     {'fallback_order': {'main': {'config_dir': '/tmp/x'}}},
                     {'fallback_order': {'main': {'credentials_file': '/tmp/x'}}}):
            self.assertEqual(self.admin('PUT', '/admin/config', body)[0], 403, body)
        self.assertNotIn('config_dir', json.loads((self.root / 'profiles.json').read_text())['profiles']['main'])

    def test_a_bad_field_changes_nothing(self):
        before = self.admin('GET', '/admin/config')[1]
        status, _ = self.admin('PUT', '/admin/config', {'models': {'codex': 'changed'}, 'routing': {'usage_skip_percent': 500}})
        self.assertEqual(status, 400)
        self.assertEqual(self.admin('GET', '/admin/config')[1], before)

    def test_invalid_values(self):
        for body in ({'nothing': 1}, {'models': []}, {'models': {'codex': 1}}, {'models': {'codex': 'a b'}}, {'models': {'nope': 'x'}},
                     {'fallback_order': {'ghost': {'priority': 1}}}, {'fallback_order': {'main': {'priority': 'x'}}},
                     {'fallback_order': {'main': {'enabled': 1}}}, {'routing': {'usage_skip_percent': True}}):
            self.assertEqual(self.admin('PUT', '/admin/config', body)[0], 400, body)

    def test_paid_model_needs_the_switch(self):
        self.assertEqual(self.admin('PUT', '/admin/config', {'models': {'openrouter': 'openai/gpt-x'}})[0], 400)
        status, _ = self.admin('PUT', '/admin/config', {'models': {'openrouter': 'openai/gpt-x', 'openrouter_paid': 'on'}})
        self.assertEqual(status, 200)
        self.admin('PUT', '/admin/config', {'models': {'openrouter': 'openrouter/free', 'openrouter_paid': 'off'}})

    def test_malformed_body(self):
        response, _ = self.call('PUT', '/admin/config', 'not json', key=ADMIN_KEY, port=self.admin_port)
        self.assertEqual(response.status, 400)


class ProviderKeys(AdminCase):
    SECRET = 'sk-or-abcdefghijklmnop1234'

    def tearDown(self):
        for key in ('openrouter', 'openrouter.work'):
            self.admin('DELETE', f'/admin/keys/{key}')

    def test_value_only_goes_in(self):
        status, body = self.admin('POST', '/admin/keys', {'provider': 'openrouter', 'value': self.SECRET})
        self.assertEqual((status, body['ending']), (201, '1234'))
        self.assertNotIn(self.SECRET, json.dumps(body))
        listed = self.admin('GET', '/admin/keys')[1]
        self.assertNotIn(self.SECRET, json.dumps(listed))
        self.assertEqual([k['id'] for k in listed['data'] if k['provider'] == 'openrouter'], ['openrouter'])

    def test_extra_key_and_delete(self):
        self.admin('POST', '/admin/keys', {'provider': 'openrouter', 'value': self.SECRET})
        self.assertEqual(self.admin('POST', '/admin/keys', {'provider': 'openrouter', 'name': 'work', 'value': self.SECRET})[0], 201)
        self.assertEqual(self.admin('DELETE', '/admin/keys/openrouter.work')[0], 200)
        self.assertEqual(self.admin('DELETE', '/admin/keys/openrouter.work')[0], 404)

    def test_existing_key_is_not_replaced(self):
        self.admin('POST', '/admin/keys', {'provider': 'openrouter', 'value': self.SECRET})
        self.assertEqual(self.admin('POST', '/admin/keys', {'provider': 'openrouter', 'value': self.SECRET})[0], 409)

    def test_invalid(self):
        for body in ({'provider': 'nope', 'value': self.SECRET}, {'provider': 'openrouter', 'value': 'wrong-prefix'},
                     {'provider': 'openrouter', 'value': 'sk-or-a b'}, {'provider': 'openrouter', 'value': 1},
                     {'provider': 'openrouter', 'value': self.SECRET, 'name': '../../evil'},
                     {'provider': 'openrouter', 'value': self.SECRET, 'path': '/x'}):
            self.assertIn(self.admin('POST', '/admin/keys', body)[0], (400, 403), body)
        self.assertFalse(list(self.root.rglob('evil')))

    def test_delete_cannot_escape(self):
        for key in ('..%2f..%2fprofiles', 'openrouter.', 'openrouter.a.b', 'nope'):
            self.assertEqual(self.admin('DELETE', f'/admin/keys/{key}')[0], 404, key)

    def test_saved_file_is_private(self):
        self.admin('POST', '/admin/keys', {'provider': 'openrouter', 'value': self.SECRET})
        path = self.root / 'cfg/keys/openrouter'
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)


class ClientKeys(AdminCase):
    def test_lifecycle(self):
        status, body = self.admin('POST', '/admin/api-keys', {'name': 'partner'})
        self.assertEqual(status, 201)
        value = body['key']
        response, _ = self.call('GET', '/v1/models', key=value, port=self.admin_port)
        self.assertEqual(response.status, 200)
        listed = self.admin('GET', '/admin/api-keys')[1]
        self.assertIn('partner', [k['name'] for k in listed['data']])
        self.assertNotIn(value, json.dumps(listed))
        self.assertEqual(self.admin('POST', '/admin/api-keys', {'name': 'partner'})[0], 409)
        self.assertEqual(self.admin('DELETE', '/admin/api-keys/partner')[0], 200)
        response, _ = self.call('GET', '/v1/models', key=value, port=self.admin_port)
        self.assertEqual(response.status, 401)

    def test_invalid(self):
        self.assertEqual(self.admin('POST', '/admin/api-keys', {'name': 'BAD NAME'})[0], 400)
        self.assertEqual(self.admin('POST', '/admin/api-keys', {})[0], 400)
        self.assertEqual(self.admin('DELETE', '/admin/api-keys/ghost')[0], 404)


class Status(AdminCase):
    def test_health(self):
        status, body = self.admin('GET', '/admin/health')
        self.assertEqual((status, body['status'], body['mode']), (200, 'ok', 'plan'))

    def test_accounts_hide_local_paths(self):
        status, body = self.admin('GET', '/admin/accounts')
        self.assertEqual(status, 200)
        self.assertEqual(body['data'][0]['name'], 'main')
        self.assertNotIn('config_dir', json.dumps(body))

    def test_main_logins_are_listed(self):
        from omni_ai.server import admin as module
        with patch.object(module, 'installed', lambda tool: tool == 'grok'), patch('omni_ai.onboarding.logged_in', lambda tool: True), \
                patch.object(module.config_tool, 'profiles', lambda: {'profiles': {}, 'accounts': {}}):
            data = module.accounts()['data']
        self.assertEqual([(a['tool'], a['id'], a['logged_in']) for a in data], [('grok', 'grok', True)])

    def test_unknown_route(self):
        self.assertEqual(self.admin('GET', '/admin/nope')[0], 404)
        self.assertEqual(self.admin('PUT', '/admin/health', {})[0], 404)


class Logs(unittest.TestCase):
    def test_query_never_reaches_the_log(self):
        from omni_ai.server import handler as module
        with self.assertLogs(module.REQUEST_LOG, 'INFO') as seen:
            fake = type('H', (), {'log_date_time_string': lambda self: 't'})()
            module.Handler.log_message(fake, '"%s" %s', 'GET /admin/x?token=secret HTTP/1.1', '200')
        self.assertNotIn('secret', seen.output[0])


if __name__ == '__main__':
    unittest.main()
