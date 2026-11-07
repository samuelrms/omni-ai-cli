"""What `ai --update` shows after updating: the release notes between two versions."""
import json
import re
import subprocess
import urllib.request

from ..osutils import REPO, resolve_bin

RELEASES_API = 'https://api.github.com/repos/samuelrms/omni-ai-cli/releases?per_page=100'
RELEASES_PAGE = 'https://github.com/samuelrms/omni-ai-cli/releases'
MAX_VERSIONS = 5
CONTROL = re.compile(r'[\x00-\x09\x0b-\x1f\x7f-\x9f]')


def installed_version():
    """The version on disk. Git installs keep a placeholder in package.json, so they use the latest tag."""
    try:
        version = json.loads((REPO / 'package.json').read_text(encoding='utf-8')).get('version', '?')
    except (OSError, ValueError):
        version = '?'
    if parse(version) or not (REPO / '.git').is_dir() or not resolve_bin('git'):
        return version
    tag = subprocess.run(['git', '-C', str(REPO), 'describe', '--tags', '--abbrev=0', '--match', 'v[0-9]*'],
                         capture_output=True, text=True, encoding='utf-8', errors='replace').stdout.strip()
    return tag[1:] if parse(tag) else version


def parse(version):
    """(major, minor, patch) for a stable version, or None. Pre-releases don't count."""
    found = re.fullmatch(r'v?(\d+)\.(\d+)\.(\d+)', str(version).strip())
    return tuple(int(part) for part in found.groups()) if found else None


def summary(body):
    """Bullets for one release: its user-facing section, or else the generated list without links."""
    body = CONTROL.sub('', body or '')
    found = re.search(r'^### What changes for you\s*\n(.*?)(?=^#{1,3} |\Z)', body, re.M | re.S)
    lines = (found.group(1) if found else body).splitlines()
    bullets = [re.sub(r'^[*-]\s+', '', line) for line in lines if re.match(r'^[*-]\s+', line)]
    return [re.sub(r'\s*\(\[[^]]*\]\([^)]*\)\)', '', bullet).strip() for bullet in bullets]


def between(releases, before, after):
    """[(version, bullets)], newest first, for before < version <= after."""
    low, high = parse(before), parse(after)
    if not low or not high or low >= high:
        return []
    found = []
    for release in releases:
        tag = release.get('tag_name', '')
        version = parse(tag)
        if version and low < version <= high and not release.get('draft') and not release.get('prerelease'):
            found.append((version, tag.lstrip('v'), summary(release.get('body'))))
    return [(tag, bullets) for _, tag, bullets in sorted(found, reverse=True)]


def fetch():
    request = urllib.request.Request(RELEASES_API, headers={'Accept': 'application/vnd.github+json',
                                                            'User-Agent': 'omni-ai-cli'})
    with urllib.request.urlopen(request, timeout=5) as response:
        return json.load(response)


def render(before, after):
    """Lines to print after an update. An update never fails because of this."""
    if not parse(after):
        return []
    if not parse(before):
        return [f'Todas as mudanças: {RELEASES_PAGE}']  # installs that couldn't tell their version before
    if parse(before) >= parse(after):
        return []
    try:
        notes = between(fetch(), before, after)
    except Exception:
        notes = []
    lines = [f'Novidades de {before} para {after}:'] if notes else []
    for version, bullets in notes[:MAX_VERSIONS]:
        lines.append(f'  {version}')
        lines += [f'    - {bullet}' for bullet in bullets]
    if len(notes) > MAX_VERSIONS:
        lines.append(f'  e mais {len(notes) - MAX_VERSIONS} versões.')
    lines.append(f'Todas as mudanças: {RELEASES_PAGE}')
    return lines
