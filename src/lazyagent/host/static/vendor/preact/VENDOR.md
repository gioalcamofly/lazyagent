# Vendored Preact, htm and signals

Served by `lazyagent host` as ES modules; no CDN, no build step.

| File | Package | Version | Source in the npm tarball |
|---|---|---|---|
| `preact.mjs` | `preact` | 10.29.8 | `dist/preact.mjs` |
| `hooks.mjs` | `preact` | 10.29.8 | `hooks/dist/hooks.mjs` |
| `LICENSE.preact` | `preact` | 10.29.8 | `LICENSE` (MIT) |
| `signals.mjs` | `@preact/signals` | 2.11.3 | `dist/signals.mjs` |
| `LICENSE.signals` | `@preact/signals` | 2.11.3 | `LICENSE` (MIT) |
| `signals-core.mjs` | `@preact/signals-core` | 1.14.4 | `dist/signals-core.mjs` |
| `LICENSE.signals-core` | `@preact/signals-core` | 1.14.4 | `LICENSE` (MIT) |
| `htm.mjs` | `htm` | 3.1.1 | `dist/htm.mjs` |
| `LICENSE.htm` | `htm` | 3.1.1 | `LICENSE` (Apache-2.0) |

Tarball sha256 (`npm pack`):

- `preact-10.29.8.tgz`: `b18cb0a457f3d43c7bb30391a74ade7d13e03bc6e77915e061c70c0fe1123299`
- `preact-signals-2.11.3.tgz`: `045ca206a2ff8f470a3ca1156d3166e0a22a6139c49786ba82b08e572e2b718d`
- `preact-signals-core-1.14.4.tgz`: `26dab47f2289a8ae668fc0af87c86f0fb69508b11ab61e194e8f3ac45cf041a3`
- `htm-3.1.1.tgz`: `2425b9bee11409177bcabc7f32e319926fc6690c1701c0b257c88bdff2d5ba90`

Changes from the tarballs:

- Bare import specifiers are rewritten to the sibling files
  (`"preact"` → `"./preact.mjs"`, `"preact/hooks"` → `"./hooks.mjs"`,
  `"@preact/signals-core"` → `"./signals-core.mjs"`): an import map would be
  an inline script, which the page's CSP (`script-src 'self'`) refuses.
- The trailing `//# sourceMappingURL=…` comments are removed (the maps are
  not shipped).

Why Preact 10 and not 11.0.1: 11.0 was 9 days old when this was vendored;
10.29.8 is the mature line `@preact/signals` 2.11 supports (`>= 10.25.0`).

To update: `npm pack` the four packages, copy the files above, apply the two
changes, and update this table.
