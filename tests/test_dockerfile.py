import isolation  # noqa: F401  (first: keeps the tests away from real data)
from pathlib import Path
import re
import unittest

DOCKERFILE = (Path(__file__).resolve().parent.parent / 'Dockerfile').read_text(encoding='utf-8')


class Downloads(unittest.TestCase):
    def test_no_script_from_the_internet_is_piped_into_a_shell(self):
        self.assertIsNone(re.search(r'\|\s*(ba)?sh\b', DOCKERFILE))

    def test_every_download_is_checked_before_use(self):
        downloads = re.findall(r'curl\b[^&]*?-o\s+(\S+)', DOCKERFILE)
        self.assertTrue(downloads)
        for target in downloads:
            with self.subTest(file=target):
                self.assertRegex(DOCKERFILE, r'echo "\$\{\w+\}  ' + re.escape(target) + r'" \| sha256sum -c -')

    def test_downloads_only_follow_https(self):
        for line in re.findall(r'\bcurl\s+-[^\n]*', DOCKERFILE):  # the command, not the apt package
            with self.subTest(line=line):
                self.assertIn("--proto '=https'", line)

    def test_each_architecture_has_a_hash(self):
        for name in ('GROK_SHA256_AMD64', 'GROK_SHA256_ARM64', 'CURSOR_SHA256_AMD64', 'CURSOR_SHA256_ARM64'):
            with self.subTest(arg=name):
                self.assertRegex(DOCKERFILE, rf'(?m)^ARG {name}=[0-9a-f]{{64}}$')

    def test_npm_packages_have_exact_versions(self):
        for version in re.findall(r'^ARG (?:CODEX|CLAUDE)_VERSION=(\S+)$', DOCKERFILE, re.M):
            self.assertRegex(version, r'^\d+\.\d+\.\d+$')


if __name__ == '__main__':
    unittest.main()
