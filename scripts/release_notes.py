#!/usr/bin/env python3
"""Add the merged pull request's `## Release notes` section to a GitHub release. Usage: release_notes.py vX.Y.Z"""
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


def main(tag):
    subject = subprocess.run(['git', 'log', '-1', '--format=%s'], check=True, capture_output=True, text=True).stdout
    number = pull_number(subject)
    if not number:
        print('No pull request in the commit title; keeping the generated notes.')
        return 0
    body = json.loads(gh('pr', 'view', number, '--json', 'body'))['body']
    current = json.loads(gh('release', 'view', tag, '--json', 'body'))['body']
    updated = merged(current, section(body))
    if updated == current:
        print('Nothing to add.')
        return 0
    gh('release', 'edit', tag, '--notes', updated)
    print(f'Added the notes from #{number} to {tag}.')
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1]))
