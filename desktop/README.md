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

From a clone, `uv run lazyagent desktop` runs `target/release/lazyagent-desktop`
and first rebuilds it (`cargo build --release --locked`, output in the
terminal) when a source file here is newer than it, e.g. after a `git pull`.
Without `cargo` on PATH, or when that build fails, it warns with the command
and runs the previous build. In the app, *Help → Check for updates…* does the
same after pulling (*Rebuild* when the checkout was pulled by hand), and
offers the restart only once the build succeeded.
