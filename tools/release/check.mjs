// Runs the release tools the way a release does, without publishing anything, so an update that
// breaks them fails in its own pull request instead of in the next release.
// Usage: npm ci --ignore-scripts --prefix tools/release && node tools/release/check.mjs
import { readFileSync } from 'node:fs';
import { execFileSync } from 'node:child_process';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { analyzeCommits } from '@semantic-release/commit-analyzer';
import { generateNotes } from '@semantic-release/release-notes-generator';

const here = dirname(fileURLToPath(import.meta.url));
const root = join(here, '..', '..');
const config = JSON.parse(readFileSync(join(root, '.releaserc.json'), 'utf8'));
const options = (name) => {
  const entry = config.plugins.find((plugin) => (Array.isArray(plugin) ? plugin[0] : plugin) === name);
  if (!entry) throw new Error(`${name} is not in .releaserc.json`);
  return Array.isArray(entry) ? entry[1] : {};
};

const failures = [];
const expect = (ok, message) => { if (!ok) failures.push(message); };
const logger = { log() {}, warn() {}, success() {}, error: console.error };
const context = (messages) => ({
  commits: messages.map((message, i) => ({ hash: `${i}`.padStart(7, '0'), message })),
  logger,
  cwd: root,
  options: { repositoryUrl: 'https://github.com/samuelrms/omni-ai-cli' },
  lastRelease: { gitTag: 'v1.0.0', version: '1.0.0' },
  nextRelease: { gitTag: 'v1.1.0', version: '1.1.0' },
});

const analyzer = options('@semantic-release/commit-analyzer');
expect(await analyzeCommits(analyzer, context(['ci: pipeline only'])) === null, 'ci: must not release');
expect(await analyzeCommits(analyzer, context(['fix: a fix'])) === 'patch', 'fix: must be a patch');
expect(await analyzeCommits(analyzer, context(['feat: a feature', 'fix: a fix'])) === 'minor', 'feat: must be a minor');

let notes = '';
try {
  notes = await generateNotes(options('@semantic-release/release-notes-generator'),
    context(['feat(chat): a feature (#1)', 'fix: a fix (#2)']));
} catch (error) {
  failures.push(`release notes could not be written: ${error.message}`);
}
expect(notes.includes('1.1.0'), 'release notes must carry the version');
expect(/Features/.test(notes) && /Bug Fixes/.test(notes), 'release notes must list features and fixes');

const bin = (name) => join(here, 'node_modules', '.bin', name);
for (const tool of ['semantic-release', 'vsce', 'ovsx']) {
  try {
    execFileSync(bin(tool), ['--help'], { stdio: 'ignore' });
  } catch (error) {
    failures.push(`${tool} does not start: ${error.message}`);
  }
}

if (failures.length) {
  console.error(failures.map((failure) => `✗ ${failure}`).join('\n'));
  process.exit(1);
}
console.log('✓ release tools work');
