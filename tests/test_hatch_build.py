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
        # lazyagent prompts renders from these.
        for name in ("skill.md", "pointer.md", "skill_line.md"):
            assert f"lazyagent/prompts/templates/{name}" in zf.namelist()
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
    import importlib.metadata
    from types import SimpleNamespace

    metadata = SimpleNamespace(version=importlib.metadata.version("lazyagent"))
    hook = hatch_build.DesktopBuildHook(str(ROOT), {}, None, metadata, "", "wheel")
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


def test_wheel_carries_the_version_file(tmp_path, cargo, monkeypatch):
    # lazyagent/__init__.py reads it instead of importlib.metadata.
    monkeypatch.delenv(hatch_build.BUILD_ENV, raising=False)
    wheel = _build(tmp_path / "dist")
    with zipfile.ZipFile(wheel) as zf:
        source = zf.read("lazyagent/_version.py").decode()
        (meta,) = [n for n in zf.namelist() if n.endswith(".dist-info/METADATA")]
        version = next(
            line.split(": ", 1)[1] for line in zf.read(meta).decode().splitlines()
            if line.startswith("Version: ")
        )
    namespace: dict = {}
    exec(source, namespace)
    assert namespace["__version__"] == version


def _install(target: str, venv: Path) -> Path:
    """``target`` (a wheel, or ``-e`` and a tree) into a fresh venv, without
    its dependencies: the version needs none of them."""
    import shutil
    import subprocess

    uv = shutil.which("uv")
    if uv is None:
        pytest.skip("uv is not on PATH")
    env = {**os.environ, "UV_NO_CONFIG": "1"}
    env.pop("VIRTUAL_ENV", None)
    subprocess.run([uv, "venv", "-q", "--python", sys.executable, str(venv)],
                   check=True, env=env)
    python = venv / "bin" / "python"
    subprocess.run([uv, "pip", "install", "-q", "--no-deps", "--python", str(python),
                    *target.split(" ")], check=True, env=env)
    return venv


@pytest.mark.parametrize("target", ["standard", "editable"])
def test_installed_version(tmp_path, cargo, monkeypatch, target):
    import re
    import subprocess

    monkeypatch.delenv(hatch_build.BUILD_ENV, raising=False)
    if target == "standard":
        install = _build(tmp_path / "dist").as_posix()
    else:
        # uv builds it (hatchling's editable wheels need the editables package).
        install = f"-e {ROOT}"
    venv = _install(install, tmp_path / "venv")
    expected = re.search(r'^version = "([^"]+)"', (ROOT / "pyproject.toml").read_text(),
                         re.M).group(1)
    out = subprocess.run(
        [str(venv / "bin" / "python"), "-I", "-c",
         "import lazyagent, importlib.metadata as m, sys; from lazyagent import cli;"
         "print(lazyagent.__file__, lazyagent.__version__, m.version('lazyagent'));"
         "sys.argv = ['lazyagent', '--version']; cli.main()"],
        capture_output=True, text=True, cwd=tmp_path,
    )
    path, *versions = out.stdout.split()
    assert versions == [expected, expected, "lazyagent", expected], out.stderr
    assert out.returncode == 0
    # The wheel's own copy, or the tree's.
    assert Path(path).is_relative_to(ROOT / "src") == (target == "editable")


def test_version_without_the_file(tmp_path):
    # A source tree no build ran in: importlib.metadata, as before.
    import importlib.metadata
    import shutil
    import subprocess

    package = tmp_path / "lazyagent"
    package.mkdir()
    shutil.copy(ROOT / "src" / "lazyagent" / "__init__.py", package)
    out = subprocess.run(
        [sys.executable, "-c", "import lazyagent; print(lazyagent.__file__, lazyagent.__version__)"],
        capture_output=True, text=True, cwd=tmp_path, check=True,
        env={**os.environ, "PYTHONPATH": str(tmp_path)},
    )
    path, version = out.stdout.split()
    assert path == str(package / "__init__.py")
    assert version == importlib.metadata.version("lazyagent")


def test_a_wheel_builds_from_a_read_only_tree(tmp_path, cargo, monkeypatch):
    # The platform wheels build from a read-only mount: the version file
    # goes into the wheel, never into the tree.
    import shutil

    monkeypatch.delenv(hatch_build.BUILD_ENV, raising=False)
    tree = tmp_path / "tree"
    for name in ("pyproject.toml", "hatch_build.py", "README.md", "LICENSE"):
        (tree / name).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(ROOT / name, tree / name)
    package = tree / "src" / "lazyagent"
    package.mkdir(parents=True)
    shutil.copy(ROOT / "src" / "lazyagent" / "__init__.py", package)
    for path in (package, package.parent, tree):
        path.chmod(0o555)
    try:
        (wheel,) = WheelBuilder(str(tree)).build(directory=str(tmp_path / "dist"),
                                                 versions=["standard"])
    finally:
        for path in (tree, package.parent, package):
            path.chmod(0o755)
    assert not (package / "_version.py").exists()
    with zipfile.ZipFile(wheel) as zf:
        names = zf.namelist()
        assert names.count("lazyagent/_version.py") == 1
        assert zf.read("lazyagent/_version.py").decode().startswith('__version__ = "')
