# lazyagent desktop

A native desktop client for the lazyagent engine, in Rust: see
[docs/design/desktop-app.md](../docs/design/desktop-app.md).

```sh
cd desktop
cargo test                      # unit + golden fixtures + real-engine tests
cargo test -- --ignored         # benches (engine echo, …)
```

The real-engine tests need a Python that imports lazyagent: the repo's
`.venv` (`uv sync`) or `LAZYAGENT_PYTHON`. Without one they are skipped,
unless `LAZYAGENT_REQUIRE_ENGINE=1`.
