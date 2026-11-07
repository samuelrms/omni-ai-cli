"""GitHub releases: fetch, translate titles, and detect a newer published version."""
import json
import re
import threading
import time

from .. import config as config_tool
from .. import ui
from ..osutils import REPO, clean_terminal
from .i18n import tr, display, LANG, LANG_NAMES
from .spend import costs_file
RELEASES = 'https://api.github.com/repos/samuelrms/omni-ai-cli/releases?per_page=15'
COMMITS = 'https://api.github.com/repos/samuelrms/omni-ai-cli/commits?sha=main&per_page=40'
CONVENTIONAL = re.compile(r'^(feat|fix|perf)(\([^)]*\))?!?:\s*(.+)')
CHANGE = re.compile(r'^\s*[*-]\s+(?:\*\*[^*]+\*\*:?\s*)?(.+?)(?:\s+\(\[[0-9a-f]{7}\].*)?$')

def github(url):
    import urllib.request
    request = urllib.request.Request(url, headers={'Accept': 'application/vnd.github+json', 'User-Agent': 'omni-ai-cli'})
    with urllib.request.urlopen(request, timeout=10) as resp:
        return json.loads(resp.read().decode())


def releases():
    """Published releases; before the first one, each feat/fix merged on main stands in as an entry."""
    items = [r for r in github(RELEASES) if not r.get('draft')]
    if items:
        return items
    changes = []
    for item in github(COMMITS):
        subject, _, body = item['commit']['message'].partition('\n')
        match = CONVENTIONAL.match(subject)
        if match:
            changes.append({'tag_name': item['sha'][:7], 'name': match.group(3), 'commit': True, 'published_at': item['commit']['committer']['date'],
                            'body': f'* {match.group(3)}\n\n{body.strip()}'})
    return changes

def translations_file():
    return costs_file().with_name('translations.json')


def translate(texts, lang):
    """Release titles in the chat's language, through the free classifiers; cached, English kept on any failure."""
    if lang not in LANG_NAMES or not texts:
        return list(texts)
    path = translations_file()
    try:
        cache = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        cache = {}
    known = cache.setdefault(lang, {})
    todo = [text for text in dict.fromkeys(texts) if text not in known]
    if todo:
        from .. import cli
        numbered = '\n'.join(f'{i}. {text}' for i, text in enumerate(todo, 1))
        messages = [{'role': 'system', 'content': f'Translate each numbered line of software release notes into {LANG_NAMES[lang]}. '
                     'Keep the numbering, one line each, plain words for end users, keep command names like /usage as they are. '
                     'Each line is a short sentence in the present tense saying what the product now does, starting with a capital letter. '
                     'Reply with the numbered lines only.'},
                    {'role': 'user', 'content': numbered}]
        for provider, url, extra in cli.free_classifiers():
            reply = next((cli.ask_text(url, key, dict(extra, messages=messages, temperature=0, max_tokens=2000), timeout=20)
                          for _, key, _ in config_tool.stored_keys(provider)), None)
            lines = [re.sub(r'^\s*\d+[.)]\s*', '', line).strip() for line in (reply or '').splitlines() if re.match(r'^\s*\d+[.)]', line)]
            if len(lines) == len(todo) and all(lines):
                known.update(zip(todo, lines))
                try:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text(json.dumps(cache, ensure_ascii=False), encoding='utf-8')
                except OSError:
                    pass
                break
    return [known.get(text, text) for text in texts]


def installed_version():
    try:
        return json.loads((REPO / 'package.json').read_text(encoding='utf-8')).get('version', '')
    except (OSError, ValueError):
        return ''


def newer_release():
    """The latest published release when it is newer than this install; checked at most once a day."""
    current = installed_version()
    if not re.fullmatch(r'\d+\.\d+\.\d+', current):  # a git checkout or a dev build: nothing to compare
        return None
    stamp = costs_file().with_name('release_check.json')
    try:
        seen = json.loads(stamp.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        seen = {}
    if time.time() - seen.get('at', 0) < 86400:
        latest = seen.get('release')
    else:
        try:
            latest = next((r for r in github(RELEASES) if not r.get('draft') and not r.get('prerelease')), None)
        except (OSError, ValueError):
            return None
        try:
            stamp.parent.mkdir(parents=True, exist_ok=True)
            stamp.write_text(json.dumps({'at': time.time(), 'release': latest}), encoding='utf-8')
        except OSError:
            pass
    if not latest:
        return None
    version = lambda tag: tuple(int(x) for x in re.findall(r'\d+', tag)[:3])
    return latest if version(latest.get('tag_name', '')) > version(current) else None


def announce_release():
    """One line under the welcome when a newer version is out (runs in the background, never delays the chat)."""
    def check():
        latest = newer_release()
        if latest:
            latest = _clean_release(latest)
            changes = [m.group(1) for m in map(CHANGE.match, (latest.get('body') or '').splitlines()) if m]
            summary = translate(changes[:1], LANG[0])[0] if changes else ''
            ui.out(ui.yellow(tr('new_version', v=latest['tag_name'], s=summary)))
    threading.Thread(target=check, daemon=True).start()


def _clean_release(release):
    """Release text comes from the network and goes to the terminal."""
    return {key: clean_terminal(value) if isinstance(value, str) else value for key, value in release.items()}


def show_versions(argument):
    """Timeline like a changelog page: simple view of every release, the technical note only for the one asked."""
    try:
        items = [_clean_release(item) for item in releases()]
    except (OSError, ValueError):
        display(tr('no_versions'))
        return
    if argument:
        chosen = next((r for i, r in enumerate(items, 1) if argument in (str(i), r.get('tag_name'), r.get('tag_name', '').lstrip('v'))), None)
        if not chosen:
            raise ValueError(tr('no_versions'))
        ui.out(ui.bold(translate([chosen.get('name') or chosen['tag_name']], LANG[0])[0]) + ui.dim(' · ' + chosen.get('published_at', '')[:10]))
        ui.out(ui.markdown(chosen.get('body') or ''))
        return
    ui.out(ui.dim(tr('versions_title')))
    for i, item in enumerate(items, 1):
        # simple view: the change titles without commit links and scopes
        changes = [item['name']] if item.get('commit') else \
            [m.group(1) for m in map(CHANGE.match, (item.get('body') or '').splitlines()) if m]
        ui.out(f"\n{ui.gradient(ui.MARK)} {ui.bold(item.get('tag_name', ''))} {ui.dim(item.get('published_at', '')[:10] + f' · {i}')}")
        for change in translate(changes[:4], LANG[0]):
            ui.out('  ' + ui.short(change, ui.width() - 4))
        if len(changes) > 4:
            ui.out(ui.dim(f'  … +{len(changes) - 4}'))
