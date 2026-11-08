import json

import pytest

from cases import Case, SuiteError, app_name, check_output, load_suite


def write_suite(tmp_path, suite):
    path = tmp_path / "tests.json"
    path.write_text(json.dumps(suite))
    return path


def test_load_suite_reads_hub_format(tmp_path):
    path = write_suite(
        tmp_path,
        {
            "tests": [{"name": "basic", "input": {"audio": "a.wav"}, "timeout": 10000}],
            "config": {"gpuTypeId": "NVIDIA GeForce RTX 4090"},
        },
    )

    suite = load_suite(path)

    assert suite.cases == [Case(name="basic", input={"audio": "a.wav"}, timeout_s=10.0)]
    assert suite.gpu == "NVIDIA GeForce RTX 4090"


def test_load_suite_reads_config_env_as_dict(tmp_path):
    path = write_suite(
        tmp_path,
        {
            "tests": [{"name": "basic", "input": {}}],
            "config": {"env": [{"key": "MODEL", "value": "turbo"}]},
        },
    )

    assert load_suite(path).env == {"MODEL": "turbo"}


def test_load_suite_defaults_timeout_and_missing_config(tmp_path):
    path = write_suite(tmp_path, {"tests": [{"name": "basic", "input": {}}]})

    suite = load_suite(path)

    assert suite.cases[0].timeout_s == 300.0
    assert suite.gpu is None
    assert suite.env == {}


@pytest.mark.parametrize(
    "suite",
    [
        {"tests": []},
        {},
        {"tests": [{"input": {}}]},
        {"tests": [{"name": "no-input"}]},
    ],
)
def test_load_suite_rejects_invalid_suites(tmp_path, suite):
    with pytest.raises(SuiteError):
        load_suite(write_suite(tmp_path, suite))


def test_load_suite_rejects_missing_file(tmp_path):
    with pytest.raises(SuiteError, match="not found"):
        load_suite(tmp_path / "absent.json")


def test_app_name_is_unique_per_run_attempt_and_api_safe():
    name = app_name("runpod-workers/worker-faster_whisper", "42459297624", "2")

    assert name == "ci-worker-faster-whisper-42459297624-2"


def test_app_name_truncates_long_repo_names_but_keeps_run_ids():
    name = app_name("org/" + "x" * 80, "123", "1")

    assert name.endswith("-123-1")
    assert len(name) <= 48


@pytest.mark.parametrize(
    ("result", "expected"),
    [
        ({"output": {"text": "four score"}}, None),
        ({"output": [1, 2]}, None),
        (
            {"output": {"error": "model missing"}},
            "handler returned an error: model missing",
        ),
        ({"output": None}, "handler returned no output"),
    ],
)
def test_check_output(result, expected):
    assert check_output(result) == expected
