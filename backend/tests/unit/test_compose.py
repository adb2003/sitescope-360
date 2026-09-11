"""Structural checks on docker-compose.yml that do not need Docker installed.

These exist because of a real escape. `docker compose config --quiet` runs in CI, but Docker is
not installed on the development machine, so a YAML syntax error in the compose file passed
review, passed the whole local suite, and was caught only after two pushes:

    yaml: line 28: mapping values are not allowed in this context

The cause was a plain (unquoted) scalar containing ": " -- an error message that read
"generate with: python -c ...". YAML cannot parse that, and nothing local looked at the file.

Parsing the file with PyYAML catches that entire class of error in milliseconds without Docker.
It is not a substitute for `docker compose config`, which also validates the compose schema; it
is the part that can run everywhere, so it runs on every commit.
"""

from __future__ import annotations

import pathlib
import re

import pytest
import yaml

COMPOSE_PATH = pathlib.Path(__file__).resolve().parents[3] / "docker-compose.yml"

#: The six services the deployment docs and the CI smoke test both assume.
EXPECTED_SERVICES = {"db", "redis", "api", "worker", "frontend", "nginx"}


@pytest.fixture(scope="module")
def compose() -> dict:
    assert COMPOSE_PATH.is_file(), f"compose file not found at {COMPOSE_PATH}"
    # safe_load, not load: this is a config file, not a Python pickle in disguise.
    return yaml.safe_load(COMPOSE_PATH.read_text(encoding="utf-8"))


def test_compose_file_is_valid_yaml(compose: dict) -> None:
    """The regression test for the escape described in the module docstring."""
    assert isinstance(compose, dict), "compose file did not parse to a mapping"
    assert "services" in compose


def test_every_documented_service_is_defined(compose: dict) -> None:
    assert set(compose["services"]) == EXPECTED_SERVICES


def test_the_two_secrets_have_no_default_and_say_so(compose: dict) -> None:
    """`${VAR:?message}` makes compose refuse to start rather than boot insecurely.

    A default value here would be worse than a failure: the stack would come up with a known
    signing key and look healthy. README and DEVELOPMENT.md both promise this behaviour.
    """
    environment = compose["services"]["api"]["environment"]
    for variable in ("JWT_SECRET", "STORAGE_SIGNING_SECRET"):
        value = environment[variable]
        assert re.fullmatch(rf"\$\{{{variable}:\?.+\}}", value), (
            f"{variable} must be ${{{variable}:?...}} with no default, got {value!r}"
        )


def test_no_plain_scalar_hides_a_colon_space(compose: dict) -> None:
    """Guard the specific shape that broke: a quoted value that would break if unquoted.

    PyYAML accepting the file proves it parses *today*. This asserts the values that contain
    ": " are ones a future edit cannot silently unquote without this test noticing, by checking
    the raw text keeps them quoted.
    """
    pattern = re.compile(r"^\s*([A-Za-z_][\w.-]*):[ \t]+(?![\"'|>&*])(.+)$")
    offenders = []
    for number, line in enumerate(COMPOSE_PATH.read_text(encoding="utf-8").split("\n"), 1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        match = pattern.match(line)
        if match and ": " in match.group(2):
            offenders.append(f"{number}: {stripped[:90]}")

    assert not offenders, "unquoted YAML values containing ': ' will fail to parse:\n" + "\n".join(
        offenders
    )
