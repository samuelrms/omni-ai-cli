#!/usr/bin/env python3
"""Add the `## Release notes` section of every pull request in a release to its GitHub page. Usage: release_notes.py vX.Y.Z"""
import json
import re
import subprocess
import sys

HEADING = '### What changes for you'


def section(pr_body):
    """The list under `## Release notes`, without template comments; empty when there is none."""
    found = re.search(r'^## Release notes[ \t]*\r?\n(.*?)(?=^## |\Z)', pr_body or '', re.M | re.S)
    return re.sub(r'<!--.*?-->', '', found.group(1), flags=re.S).strip() if found else ''


def merged(release_body, notes):
    """The release body with the notes after its title; unchanged without notes or when already added."""
    if not notes or HEADING in release_body:
        return release_body
    title, _, rest = release_body.partition('\n\n')
    return f'{title}\n\n{HEADING}\n\n{notes}\n\n{rest}'.rstrip() + '\n'


def pull_number(subject):
    found = re.search(r'\(#(\d+)\)$', subject.strip())
    return found.group(1) if found else None


def gh(*args):
    return subprocess.run(['gh', *args], check=True, capture_output=True, text=True).stdout


def git(*args):
    return subprocess.run(['git', *args], check=True, capture_output=True, text=True).stdout


def pull_numbers(tag):
    """Oldest first. Merges close together share one release, so its last commit is not enough."""
    previous = git('describe', '--tags', '--abbrev=0', '--match', 'v[0-9]*', f'{tag}^').strip()
    subjects = git('log', '--reverse', '--format=%s', f'{previous}..{tag}').splitlines()
    return [number for number in map(pull_number, subjects) if number]


def main(tag):
    numbers = pull_numbers(tag)
    if not numbers:
        print('No pull request in the commit titles; keeping the generated notes.')
        return 0
    sections = [section(json.loads(gh('pr', 'view', number, '--json', 'body'))['body']) for number in numbers]
    current = json.loads(gh('release', 'view', tag, '--json', 'body'))['body']
    updated = merged(current, '\n'.join(filter(None, sections)))
    if updated == current:
        print('Nothing to add.')
        return 0
    gh('release', 'edit', tag, '--notes', updated)
    print(f'Added the notes from {", ".join("#" + number for number in numbers)} to {tag}.')
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1]))
