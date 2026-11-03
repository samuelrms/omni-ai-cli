#!/usr/bin/env bash
# Render the README flowcharts from docs/diagrams/*.mmd to PNG.
#
# npm's registry page does not render ```mermaid``` blocks, so the READMEs embed
# a pre-rendered PNG served from raw.githubusercontent instead. Run this whenever
# a .mmd source changes, then commit the updated PNGs.
set -euo pipefail

cd "$(dirname "$0")/.."
dir=docs/diagrams

# mermaid-cli drives a headless Chrome; install the shell once if it is missing.
if ! ls "$HOME"/.cache/puppeteer/chrome-headless-shell/*/chrome-headless-shell-*/chrome-headless-shell >/dev/null 2>&1; then
  npx --yes puppeteer browsers install chrome-headless-shell
fi
chrome=$(ls "$HOME"/.cache/puppeteer/chrome-headless-shell/*/chrome-headless-shell-*/chrome-headless-shell | head -1)

cfg=$(mktemp)
printf '{"executablePath":"%s","args":["--no-sandbox"]}' "$chrome" > "$cfg"
trap 'rm -f "$cfg"' EXIT

for src in "$dir"/*.mmd; do
  out="${src%.mmd}.png"
  echo "→ $out"
  npx --yes @mermaid-js/mermaid-cli@11 -p "$cfg" -i "$src" -o "$out" -b white -s 3 -t default
done
echo "Done. Commit the PNGs under $dir/."
