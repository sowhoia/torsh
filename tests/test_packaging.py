"""Packaging invariants — the release bugs you only notice after publishing.

Every check here stands in for a mistake that is invisible in a dev checkout
(where torsh is installed editable and every file is simply "there") and
only surfaces once someone runs ``pip install torsh``.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover - exercised only on 3.10
    import tomli as tomllib

import torsh

ROOT = Path(__file__).resolve().parent.parent
PACKAGE = Path(torsh.__file__).parent


@pytest.fixture(scope="module")
def pyproject() -> dict:
    return tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))


def _normalise(spec: str) -> str:
    # Environment markers are quote-agnostic; TOML and pip files disagree.
    return spec.strip().replace('"', "'")


def _requirements(name: str) -> list[str]:
    lines = (ROOT / name).read_text(encoding="utf-8").splitlines()
    return [_normalise(line) for line in lines if line.strip() and not line.startswith("#")]


def test_version_is_declared_once(pyproject):
    """torsh/__init__.py is the only place the version is written down.

    A literal `version = "…"` in pyproject.toml is the classic way to ship a
    wheel whose metadata disagrees with what `torsh --version` prints.
    """
    assert "version" not in pyproject["project"], "version must stay dynamic"
    assert pyproject["project"]["dynamic"] == ["version"]
    assert pyproject["tool"]["setuptools"]["dynamic"]["version"] == {"attr": "torsh.__version__"}
    assert re.fullmatch(r"\d+\.\d+\.\d+", torsh.__version__), torsh.__version__


def test_homebrew_formula_tracks_the_version():
    """The formula pins a version by hand; it must not lag behind a release."""
    formula = (ROOT / "packaging" / "homebrew" / "torsh.rb").read_text(encoding="utf-8")
    match = re.search(r'^\s*version\s+"([^"]+)"', formula, re.MULTILINE)
    assert match, "formula has no version line"
    assert match.group(1) == torsh.__version__


def test_requirements_files_mirror_pyproject(pyproject):
    """Two dependency lists is one too many — so they must agree exactly."""
    declared = [_normalise(spec) for spec in pyproject["project"]["dependencies"]]
    assert _requirements("requirements.txt") == declared
    dev = [_normalise(spec) for spec in pyproject["project"]["optional-dependencies"]["dev"]]
    assert _requirements("requirements-dev.txt") == dev


def test_package_data_is_declared_and_present(pyproject):
    """A stylesheet that never ships makes the TUI crash on first launch."""
    declared = pyproject["tool"]["setuptools"]["package-data"]
    assert "py.typed" in declared["torsh"]
    assert "styles.tcss" in declared["torsh.tui"]
    assert (PACKAGE / "py.typed").is_file()
    assert (PACKAGE / "tui" / "styles.tcss").is_file()


def test_console_script_points_at_a_callable(pyproject):
    module, _, attribute = pyproject["project"]["scripts"]["torsh"].partition(":")
    imported = __import__(module, fromlist=[attribute])
    assert callable(getattr(imported, attribute))


def test_supported_pythons_match_the_classifiers(pyproject):
    """`requires-python` and the trove classifiers are read by different
    tools; a mismatch silently misinforms one of them."""
    project = pyproject["project"]
    floor = re.fullmatch(r">=(\d+\.\d+)", project["requires-python"])
    assert floor, project["requires-python"]
    classified = {
        line.rsplit(" :: ", 1)[1]
        for line in project["classifiers"]
        if line.startswith("Programming Language :: Python :: 3.")
    }
    assert floor.group(1) in classified
    assert classified == {f"3.{minor}" for minor in range(int(floor.group(1).split(".")[1]), 14)}
