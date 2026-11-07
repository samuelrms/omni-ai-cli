import isolation  # noqa: F401  (first: keeps the tests away from real data)
import io
import os
import unittest
from unittest.mock import patch

from omni_ai import server
from omni_ai.server import auth

KEY = 'omni-' + 'k' * 43


class CreatedKey(unittest.TestCase):
    def setUp(self):
        self.addCleanup(lambda: auth.created_key_path().unlink(missing_ok=True))

    def shown(self, inside, **patches):
        out = io.StringIO()
        with patch.object(server, 'in_container', return_value=inside), patch('sys.stdout', out):
            server._show_created(KEY)
        return out.getvalue()

    def test_a_container_keeps_the_key_out_of_its_log(self):
        text = self.shown(True)
        self.assertNotIn(KEY, text)
        self.assertIn(f'docker exec omni cat {auth.created_key_path()}', text)
        self.assertEqual(auth.created_key_path().read_text(encoding='utf-8'), KEY + '\n')
        if os.name != 'nt':
            self.assertEqual(auth.created_key_path().stat().st_mode & 0o777, 0o600)

    def test_a_key_that_cannot_be_saved_is_still_not_logged(self):
        with patch.object(server, 'private_file', side_effect=PermissionError(13, 'Permission denied')):
            text = self.shown(True)
        self.assertNotIn(KEY, text)
        self.assertIn('ai serve keys add', text)

    def test_outside_a_container_it_is_shown_once_as_before(self):
        self.assertIn(KEY, self.shown(False))
        self.assertFalse(auth.created_key_path().exists())


if __name__ == '__main__':
    unittest.main()
