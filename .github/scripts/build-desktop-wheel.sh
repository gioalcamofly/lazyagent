#!/bin/sh
# Build the platform wheel (the desktop binary inside, see hatch_build.py).
# Runs inside quay.io/pypa/manylinux_2_28_<arch>, whose old glibc is what makes
# the manylinux_2_28 tag true; auditwheel then checks it.
#   /io                 the source tree (read-only is fine)
#   OUT                 where the wheel goes
#   CARGO_TARGET_DIR    cargo's build cache (kept between CI runs)
#   /root/.cargo/registry  crate downloads (kept between CI runs)
set -eu
OUT=${OUT:?}
if ! command -v cargo >/dev/null; then
    curl -sSf https://sh.rustup.rs | sh -s -- -y --profile minimal --default-toolchain stable
fi
# shellcheck source=/dev/null
. "$HOME/.cargo/env"
cargo --version
PY=/opt/python/cp312-cp312/bin/python
"$PY" -m pip install --quiet --root-user-action=ignore uv
LAZYAGENT_BUILD_DESKTOP=1 "$PY" -m uv build --wheel --out-dir "$OUT" /io
wheel=$(ls "$OUT"/*-py3-none-manylinux_2_28_*.whl)
auditwheel show "$wheel" | tee "$OUT/auditwheel.txt"
# auditwheel wraps its text; join it before looking for the tag (2_28 or older).
tr -s ' \n' '  ' <"$OUT/auditwheel.txt" |
    grep -Eq 'consistent with the following platform tag: "manylinux_2_(1[0-9]|2[0-8])_'
rm "$OUT/auditwheel.txt"
