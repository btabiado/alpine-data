"""A requirements pin must install on every workflow Python that installs it.

Audited against PyPI Requires-Python on 2026-10-17. Two cutoffs are above
the repo's Python 3.11 floor:

* numpy >= 2.5 requires Python >= 3.12. ``requirements.txt`` is still
  installed on 3.11, and Dependabot ignores ``>=2.5``.
* scipy >= 1.18 requires Python >= 3.12. Only crypto-odds-daily installs
  ``crypto_odds/requirements.txt``, and that workflow runs 3.12. scipy
  1.18.1 is what failed crypto-odds-daily run 39052317800 on 3.11.

Every other pin's latest release still publishes a Python 3.11 wheel, so
it is not in ``FLOORS``. ``tests.yml`` also installs the crypto-odds file
on its 3.12 leg, which is what rejects a later scipy that drops 3.12.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"

# (minimum version, minimum Python) for releases that dropped an older
# interpreter. A pin at or above the version needs that Python.
FLOORS: dict[str, list[tuple[str, tuple[int, int]]]] = {
    "numpy": [("2.5", (3, 12))],
    "scipy": [("1.18", (3, 12))],
}

_PIN = re.compile(
    r"^([A-Za-z0-9][A-Za-z0-9_.-]*)(?:\[[^\]]+\])?==([0-9][0-9A-Za-z.*+!]*)"
)
_PY_EQ = re.compile(r"matrix\.python-version\s*==\s*['\"]([^'\"]+)['\"]")
_PY_NE = re.compile(r"matrix\.python-version\s*!=\s*['\"]([^'\"]+)['\"]")


def _canonical(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _version_tuple(version: str) -> tuple[int, ...]:
    core = version.split("+", 1)[0]
    core = re.split(r"[A-Za-z]", core, maxsplit=1)[0].rstrip(".")
    return tuple(int(part) for part in core.split(".") if part != "")


def _version_ge(version: str, floor: str) -> bool:
    left, right = _version_tuple(version), _version_tuple(floor)
    width = max(len(left), len(right))
    left = left + (0,) * (width - len(left))
    right = right + (0,) * (width - len(right))
    return left >= right


def _min_python(name: str, version: str) -> tuple[int, int]:
    need = (3, 11)
    for floor, python in FLOORS.get(_canonical(name), []):
        if _version_ge(version, floor):
            need = max(need, python)
    return need


def _python_ok(workflow_python: str, need: tuple[int, int]) -> bool:
    # setup-python's "3.x" tracks the newest 3, which is past these floors.
    if workflow_python in {"3.x", "3"}:
        return True
    major, minor = (int(part) for part in workflow_python.split(".")[:2])
    return (major, minor) >= need


def _pins(path: Path) -> dict[str, str]:
    found: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        match = _PIN.match(line)
        if match:
            found[_canonical(match.group(1))] = match.group(2)
    return found


def _job_pythons(job: dict) -> list[str]:
    matrix = (job.get("strategy") or {}).get("matrix") or {}
    listed = matrix.get("python-version")
    if isinstance(listed, list):
        return [str(item) for item in listed]
    if isinstance(listed, str):
        return [listed]
    for step in job.get("steps") or []:
        if "setup-python" not in str(step.get("uses") or ""):
            continue
        raw = (step.get("with") or {}).get("python-version")
        if raw is None:
            continue
        if isinstance(raw, list):
            return [str(item) for item in raw]
        text = str(raw).strip()
        if "matrix.python-version" in text:
            raise AssertionError(f"matrix python expression without a version list: {text}")
        return [text]
    return []


def _step_pythons(step: dict, versions: list[str]) -> list[str]:
    cond = str(step.get("if") or "")
    if "python-version" in cond and not (_PY_EQ.search(cond) or _PY_NE.search(cond)):
        raise AssertionError(f"unparsed python condition: {cond}")
    matched = _PY_EQ.search(cond)
    if matched:
        return [version for version in versions if version == matched.group(1)]
    excluded = _PY_NE.search(cond)
    if excluded:
        return [version for version in versions if version != excluded.group(1)]
    return versions


def _install_lines(script: str) -> list[str]:
    lines = []
    for raw in script.splitlines():
        line = raw.split("#", 1)[0].strip()
        if "pip install" in line or "pip3 install" in line:
            lines.append(line)
    return lines


def _tokenize_install(line: str) -> tuple[list[str], list[str]]:
    """Return ``(-r`` files, named distributions) from one pip install line."""
    tokens = line.split()
    tokens = tokens[tokens.index("install") + 1 :]
    requirements: list[str] = []
    named: list[str] = []
    flags_with_value = {"-r", "--requirement", "-c", "--constraint"}
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token in flags_with_value:
            kind = token
            index += 1
            if index >= len(tokens):
                break
            if kind in {"-r", "--requirement"}:
                requirements.append(tokens[index])
            index += 1
            continue
        if token.startswith("-"):
            index += 1
            continue
        named.append(token)
        index += 1
    return requirements, named


def _installed() -> list[tuple[str, str, str, str]]:
    """``(workflow, python, kind, target)`` for each real install.

    ``kind`` is ``"file"`` (every pin in ``target``) or ``"package"``.
    """
    rows: list[tuple[str, str, str, str]] = []
    for path in sorted(WORKFLOWS.glob("*.yml")):
        document = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        for job_name, job in (document.get("jobs") or {}).items():
            versions = _job_pythons(job)
            for step in job.get("steps") or []:
                script = step.get("run") or ""
                if "pip install" not in script and "pip3 install" not in script:
                    continue
                if not versions:
                    raise AssertionError(f"{path.name} job {job_name} pip-installs without a Python")
                step_versions = _step_pythons(step, versions)
                for line in _install_lines(script):
                    requirements, named = _tokenize_install(line)
                    for version in step_versions:
                        for req in requirements:
                            rows.append((path.name, version, "file", req))
                        for package in named:
                            rows.append((path.name, version, "package", package))
    return rows


def _pin_for(package: str, files: list[Path]) -> str:
    name = _canonical(package.split("==", 1)[0].split("[", 1)[0])
    if "==" in package:
        return package.split("==", 1)[1]
    for path in files:
        pins = _pins(path)
        if name in pins:
            return pins[name]
    raise AssertionError(f"{package} is installed without a pin in {[p.name for p in files]}")


def test_every_installed_pin_supports_the_workflow_python():
    rows = _installed()
    assert rows, "no pip install steps found"
    problems: list[str] = []
    saw_scipy = False
    saw_numpy_on_311 = False
    for workflow, python, kind, target in rows:
        if kind == "file":
            path = ROOT / target
            assert path.is_file(), f"{workflow} installs missing {target}"
            checking = _pins(path).items()
        else:
            # A named package (`pip install -c requirements.txt requests`)
            # is pinned in one of the four manifests. `pip` itself is the
            # interpreter installer, not one of those pins.
            manifests = [
                ROOT / "requirements.txt",
                ROOT / "requirements-dev.txt",
                ROOT / "requirements-jobs.txt",
                ROOT / "crypto_odds" / "requirements.txt",
            ]
            name = _canonical(target.split("==", 1)[0].split("[", 1)[0])
            if name == "pip":
                continue
            version = _pin_for(target, manifests)
            checking = [(name, version)]
        for name, version in checking:
            need = _min_python(name, version)
            if name == "scipy":
                saw_scipy = True
            if name == "numpy" and python == "3.11":
                saw_numpy_on_311 = True
            if not _python_ok(python, need):
                problems.append(
                    f"{workflow} on Python {python} installs {name}=={version}, "
                    f"which needs Python >= {need[0]}.{need[1]}"
                )
    assert saw_scipy, "expected crypto-odds to install scipy"
    assert saw_numpy_on_311, "expected a 3.11 job to install numpy"
    assert not problems, "\n".join(problems)


def test_crypto_odds_daily_is_python_312():
    document = yaml.safe_load(
        (WORKFLOWS / "crypto-odds-daily.yml").read_text(encoding="utf-8")
    )
    assert _job_pythons(document["jobs"]["run"]) == ["3.12"]


def test_tests_workflow_installs_crypto_odds_requirements_on_312_only():
    rows = [
        (workflow, python, target)
        for workflow, python, kind, target in _installed()
        if kind == "file" and target == "crypto_odds/requirements.txt"
    ]
    assert ("crypto-odds-daily.yml", "3.12", "crypto_odds/requirements.txt") in rows
    assert ("tests.yml", "3.12", "crypto_odds/requirements.txt") in rows
    assert all(python != "3.11" for _workflow, python, _target in rows)


def test_dependabot_holds_numpy_and_does_not_bundle_scipy_minors():
    document = yaml.safe_load((ROOT / ".github" / "dependabot.yml").read_text(encoding="utf-8"))
    pip_entries = [entry for entry in document["updates"] if entry["package-ecosystem"] == "pip"]
    assert [entry["directory"] for entry in pip_entries] == ["/"]
    ignores = {
        item["dependency-name"]: item.get("versions") or []
        for item in pip_entries[0].get("ignore") or []
    }
    assert ">=2.5" in ignores["numpy"]
    # Ignoring scipy >=1.18 would freeze the 3.12 engine pin, including patches.
    for spec in ignores.get("scipy", []):
        if spec.startswith(">="):
            assert not _version_ge("1.18.1", spec[2:]), spec
    excluded = pip_entries[0]["groups"]["minor-updates"].get("exclude-patterns") or []
    assert "scipy" in excluded


@pytest.mark.parametrize(
    "pin,need",
    [("2.4.6", (3, 11)), ("2.5.0", (3, 12)), ("1.17.1", (3, 11)), ("1.18.1", (3, 12))],
)
def test_floor_table_matches_the_known_cutoffs(pin, need):
    name = "numpy" if pin.startswith("2.") else "scipy"
    assert _min_python(name, pin) == need
