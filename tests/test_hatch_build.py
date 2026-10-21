"""hatch_build.py: the desktop binary inside a platform wheel, only on request.

Builds real wheels of this tree with hatchling, in process; ``cargo`` is a
shim on PATH that records its arguments and drops a fake binary.
"""

from __future__ import annotations

import os
import stat
import sys
import zipfile
from pathlib import Path

import pytest

pytest.importorskip("hatchling")
from hatchling.builders.wheel import WheelBuilder  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import hatch_build  # noqa: E402


@pytest.fixture
def cargo(tmp_path, monkeypatch):
    """A fake cargo: logs its args, writes $CARGO_TARGET_DIR/release/lazyagent-desktop."""
    bin_dir = tmp_path / "shim"
    bin_dir.mkdir()
    log = tmp_path / "cargo.log"
    target = tmp_path / "target"
    shim = bin_dir / "cargo"
    shim.write_text(
        "#!/bin/sh\n"
        f'echo "$@" >> {log}\n'
        'mkdir -p "$CARGO_TARGET_DIR/release"\n'
        'printf "#!/bin/sh\\necho fake app\\n" > "$CARGO_TARGET_DIR/release/lazyagent-desktop"\n'
    )
    shim.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("CARGO_TARGET_DIR", str(target))
    return log


def _build(out: Path) -> Path:
    (wheel,) = WheelBuilder(str(ROOT)).build(directory=str(out), versions=["standard"])
    return Path(wheel)


def test_pure_wheel_without_the_variable(tmp_path, cargo, monkeypatch):
    monkeypatch.delenv(hatch_build.BUILD_ENV, raising=False)
    wheel = _build(tmp_path / "dist")
    assert wheel.name.endswith("-py3-none-any.whl")
    with zipfile.ZipFile(wheel) as zf:
        assert hatch_build.WHEEL_PATH not in zf.namelist()
        assert "Root-Is-Purelib: true" in zf.read(_wheel_file(zf)).decode()
    assert not cargo.exists()


def test_platform_wheel_with_the_binary(tmp_path, cargo, monkeypatch):
    monkeypatch.setenv(hatch_build.BUILD_ENV, "1")
    monkeypatch.delenv(hatch_build.PLATFORM_ENV, raising=False)
    if sys.platform != "linux":
        monkeypatch.setenv(hatch_build.PLATFORM_ENV, "manylinux_2_28_x86_64")
    wheel = _build(tmp_path / "dist")
    expected = os.environ.get(hatch_build.PLATFORM_ENV) or hatch_build.wheel_platform()
    assert wheel.name.endswith(f"-py3-none-{expected}.whl")
    assert cargo.read_text().split() == [
        "build", "--release", "--locked", "--manifest-path", str(ROOT / "desktop" / "Cargo.toml"),
    ]
    with zipfile.ZipFile(wheel) as zf:
        info = zf.getinfo(hatch_build.WHEEL_PATH)
        assert stat.S_IMODE(info.external_attr >> 16) & 0o111 == 0o111
        assert zf.read(info).startswith(b"#!/bin/sh")
        meta = zf.read(_wheel_file(zf)).decode()
        assert "Root-Is-Purelib: false" in meta
        assert f"Tag: py3-none-{expected}" in meta
        # The Python package is all there too.
        assert "lazyagent/desktop.py" in zf.namelist()


def test_platform_tag_override(tmp_path, cargo, monkeypatch):
    monkeypatch.setenv(hatch_build.BUILD_ENV, "1")
    monkeypatch.setenv(hatch_build.PLATFORM_ENV, "manylinux_2_28_aarch64")
    wheel = _build(tmp_path / "dist")
    assert wheel.name.endswith("-py3-none-manylinux_2_28_aarch64.whl")


def test_editable_builds_never_compile(cargo, monkeypatch):
    """`uv sync` / `pip install -e` stay pure even with the variable set."""
    monkeypatch.setenv(hatch_build.BUILD_ENV, "1")
    hook = hatch_build.DesktopBuildHook(str(ROOT), {}, None, None, "", "wheel")
    build_data = {"force_include": {}, "pure_python": True}
    hook.initialize("editable", build_data)
    assert build_data == {"force_include": {}, "pure_python": True}
    assert not cargo.exists()


def test_platform_defaults_to_manylinux_on_linux(monkeypatch):
    monkeypatch.setattr(hatch_build.sys, "platform", "linux")
    monkeypatch.setattr(hatch_build.platform, "machine", lambda: "aarch64")
    assert hatch_build.wheel_platform({}) == "manylinux_2_28_aarch64"
    monkeypatch.setattr(hatch_build.sys, "platform", "darwin")
    with pytest.raises(RuntimeError, match="Linux only"):
        hatch_build.wheel_platform({})
    assert hatch_build.wheel_platform({hatch_build.PLATFORM_ENV: "macosx_11_0_arm64"}) == (
        "macosx_11_0_arm64"
    )


def _wheel_file(zf: zipfile.ZipFile) -> str:
    return next(n for n in zf.namelist() if n.endswith(".dist-info/WHEEL"))
