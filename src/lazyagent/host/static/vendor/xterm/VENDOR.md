# Vendored xterm.js

Served by `lazyagent host`; no CDN at runtime.

| File | Package | Version | Source in the npm tarball |
|---|---|---|---|
| `xterm.mjs` | `@xterm/xterm` | 6.0.0 | `lib/xterm.mjs` |
| `xterm.css` | `@xterm/xterm` | 6.0.0 | `css/xterm.css` |
| `LICENSE` | `@xterm/xterm` | 6.0.0 | `LICENSE` (MIT) |
| `addon-fit.mjs` | `@xterm/addon-fit` | 0.11.0 | `lib/addon-fit.mjs` |
| `LICENSE.addon-fit` | `@xterm/addon-fit` | 0.11.0 | `LICENSE` (MIT) |
| `addon-webgl.mjs` | `@xterm/addon-webgl` | 0.19.0 | `lib/addon-webgl.mjs` |
| `LICENSE.addon-webgl` | `@xterm/addon-webgl` | 0.19.0 | `LICENSE` (MIT) |

Tarball sha256 (`npm pack`):

- `xterm-xterm-6.0.0.tgz`: `908e66e04af6c8dc6b00dd3b54de088e2e81e5ed866284fd6c2fb3c2d1c7a3f6`
- `xterm-addon-fit-0.11.0.tgz`: `26003b4517a132b64e4ff228fd88a5fda3fff5e606c76093f6dcff772e9ecec0`
- `xterm-addon-webgl-0.19.0.tgz`: `b51c9ca08dfd1b35e1f1b833334497a9a8412110569b26e03cb6e1ab7341fb61`

The only change: the trailing `//# sourceMappingURL=…` line of the `.mjs`
files is removed (the maps are not shipped).

To update: `npm pack @xterm/xterm@X @xterm/addon-fit@Y @xterm/addon-webgl@Z`, copy the files
above, drop the source-map line, and update this table.
