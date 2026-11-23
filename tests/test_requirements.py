import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_requirements_match_pyproject():
    deps = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["dependencies"]
    req = [l.strip() for l in (ROOT / "requirements.txt").read_text().splitlines() if l.strip() and not l.startswith("#")]
    assert req == deps, "requirements.txt разошёлся с pyproject.toml"
