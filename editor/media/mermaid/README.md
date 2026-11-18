# Mermaid

`mermaid.min.js` is `dist/mermaid.min.js` from the npm package
[mermaid@10.9.8](https://www.npmjs.com/package/mermaid/v/10.9.8), unchanged
(sha256 `8d607d7ef1d077a8aa202e18e62212bfa992c68bfeabc5cf45d51a128fe6675d`). The chat loads it the first time an
answer has a diagram.

Mermaid is MIT licensed: `LICENSE` is the one in
[mermaid-js/mermaid at v10.9.8](https://github.com/mermaid-js/mermaid/blob/v10.9.8/LICENSE). The file also bundles
Mermaid's dependencies, each under its own license, among them DOMPurify (MPL-2.0 or Apache-2.0), elkjs (EPL-2.0),
d3 (ISC) and d3-sankey (BSD-3-Clause). The full list is under `dependencies` in that version's `package.json`, and the
source of each one is on npm.

Dependabot does not see this file. To update it:

1. Run `npm view mermaid@<version> dist.tarball dist.integrity`.
2. Download the tarball and check its sha512 against `dist.integrity`.
3. Replace `mermaid.min.js` with the tarball's `package/dist/mermaid.min.js` and `LICENSE` with the one at the
   upstream tag, then update the version and the sha256 above.
