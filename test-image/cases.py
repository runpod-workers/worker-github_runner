"""Test-suite parsing and result checks for the test-image action.

Reads the Hub test format (`.runpod/tests.json`):

    {"tests": [{"name": "basic", "input": {...}, "timeout": 10000}],
     "config": {"gpuTypeId": "NVIDIA GeForce RTX 4090",
                "allowedCudaVersions": ["12.8", "13.0"],
                "env": [{"key": "K", "value": "V"}]}}

`timeout` is in milliseconds, as on the Hub. The lowest entry of
`allowedCudaVersions` becomes the endpoint's minimum CUDA version.

In server mode each test's input is an HTTP request instead of a job input:
    {"method": "POST", "path": "/v1/completions", "body": {...}, "status": 200}
"""

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

DEFAULT_TIMEOUT_S = 300.0
MS_PER_S = 1000.0
# endpoint names carry a "dev-" prefix, a resource name and a hash suffix,
# so the app name gets a conservative share of the length.
MAX_APP_NAME = 48


class SuiteError(ValueError):
    """The tests file is missing or malformed."""


@dataclass(frozen=True)
class Case:
    name: str
    input: Dict[str, Any]
    timeout_s: float


@dataclass(frozen=True)
class Suite:
    cases: List[Case]
    gpu: Optional[str] = None
    env: Dict[str, str] = field(default_factory=dict)
    min_cuda: Optional[str] = None


def load_suite(path: Path) -> Suite:
    path = Path(path)
    if not path.is_file():
        raise SuiteError(f"tests file not found: {path}")
    data = json.loads(path.read_text())

    raw_cases = data.get("tests") or []
    if not raw_cases:
        raise SuiteError(f"{path} defines no tests")

    cases = []
    for index, raw in enumerate(raw_cases):
        if "name" not in raw or "input" not in raw:
            raise SuiteError(f"{path}: test #{index} needs both 'name' and 'input'")
        timeout_ms = raw.get("timeout")
        timeout_s = timeout_ms / MS_PER_S if timeout_ms else DEFAULT_TIMEOUT_S
        cases.append(Case(name=raw["name"], input=raw["input"], timeout_s=timeout_s))

    config = data.get("config") or {}
    env = _parse_env(path, config.get("env") or [])
    return Suite(
        cases=cases,
        gpu=config.get("gpuTypeId"),
        env=env,
        min_cuda=_lowest_version(config.get("allowedCudaVersions") or []),
    )


def _parse_env(path: Path, entries: Any) -> Dict[str, str]:
    """Read config.env, a list of {"key": ..., "value": ...} entries."""
    if not isinstance(entries, list):
        raise SuiteError(f"{path}: config.env must be a list of key/value entries")
    env = {}
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict) or "key" not in entry or "value" not in entry:
            raise SuiteError(
                f"{path}: config.env entry #{index} needs both 'key' and 'value'"
            )
        env[entry["key"]] = str(entry["value"])
    return env


def _lowest_version(versions: List[str]) -> Optional[str]:
    if not versions:
        return None
    return min(versions, key=lambda v: tuple(int(part) for part in v.split(".")))


def http_request(case_input: Dict[str, Any]) -> Dict[str, Any]:
    """Normalize a server-mode test input into method, path, body and expected status."""
    path = case_input.get("path")
    if not isinstance(path, str) or not path.startswith("/"):
        raise SuiteError(
            f"server-mode test input needs an absolute 'path', got {path!r}"
        )
    return {
        "method": str(case_input.get("method", "GET")).upper(),
        "path": path,
        "body": case_input.get("body"),
        "status": int(case_input.get("status", 200)),
    }


def app_name(repository: str, run_id: str, attempt: str) -> str:
    """A Flash app name unique to one workflow run attempt."""
    repo = repository.rsplit("/", 1)[-1].lower()
    repo = re.sub(r"[^a-z0-9]+", "-", repo).strip("-")
    suffix = f"-{run_id}-{attempt}"
    budget = MAX_APP_NAME - len("ci-") - len(suffix)
    return f"ci-{repo[:budget].rstrip('-')}{suffix}"


def check_output(result: Dict[str, Any]) -> Optional[str]:
    """Return why a handler result fails the test, or None if it passes."""
    output = result.get("output")
    if output is None:
        return "handler returned no output"
    if isinstance(output, dict) and "error" in output:
        return f"handler returned an error: {output['error']}"
    return None


def check_http_output(result: Dict[str, Any], expected_status: int) -> Optional[str]:
    """Return why a server response fails the test, or None if it passes."""
    output = result.get("output")
    if output is None:
        return "server returned no response"
    if output["status"] != expected_status:
        return (
            f"expected HTTP {expected_status}, got {output['status']}: {output['body']}"
        )
    return None
