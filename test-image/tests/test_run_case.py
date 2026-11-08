import importlib
import json
import sys
import textwrap

import pytest


@pytest.fixture
def flash_app(tmp_path, monkeypatch):
    suite = tmp_path / "tests.json"
    suite.write_text(json.dumps({"tests": [{"name": "basic", "input": {}}]}))
    monkeypatch.setenv("RP_TEST_IMAGE", "runpod/worker-example:ci")
    monkeypatch.setenv("RP_TEST_FILE", str(suite))
    monkeypatch.setenv("RP_TEST_GPU", "NVIDIA GeForce RTX 4090")
    monkeypatch.setenv("RP_TEST_APP_NAME", "ci-example-1-1")
    import flash_app as module

    return importlib.reload(module)


def write_handler(tmp_path, source, name="rp_handler.py"):
    path = tmp_path / name
    path.write_text(textwrap.dedent(source))
    return str(path)


def test_runs_handler_registered_at_module_level(flash_app, tmp_path):
    handler = write_handler(
        tmp_path,
        """
        import runpod

        def handler(job):
            return {"echo": job["input"]["text"], "id": job["id"]}

        runpod.serverless.start({"handler": handler})
        """,
    )

    result = flash_app.run_case.local(
        handler, {"id": "ci-basic", "input": {"text": "hi"}}
    )

    assert result == {"output": {"echo": "hi", "id": "ci-basic"}}


def test_runs_handler_registered_under_main_guard(flash_app, tmp_path):
    handler = write_handler(
        tmp_path,
        """
        import runpod

        def handler(job):
            return "ok"

        if __name__ == "__main__":
            runpod.serverless.start({"handler": handler})
        """,
    )

    assert flash_app.run_case.local(handler, {"id": "x", "input": {}}) == {
        "output": "ok"
    }


def test_handler_can_import_sibling_modules(flash_app, tmp_path):
    write_handler(tmp_path, "VALUE = 42\n", name="predict.py")
    handler = write_handler(
        tmp_path,
        """
        import runpod
        import predict

        runpod.serverless.start({"handler": lambda job: predict.VALUE})
        """,
    )

    assert flash_app.run_case.local(handler, {"id": "x", "input": {}}) == {"output": 42}


def test_collects_generator_output(flash_app, tmp_path):
    handler = write_handler(
        tmp_path,
        """
        import runpod

        def handler(job):
            yield 1
            yield 2

        runpod.serverless.start({"handler": handler})
        """,
    )

    assert flash_app.run_case.local(handler, {"id": "x", "input": {}}) == {
        "output": [1, 2]
    }


def test_awaits_async_handler(flash_app, tmp_path):
    handler = write_handler(
        tmp_path,
        """
        import runpod

        async def handler(job):
            return "async ok"

        runpod.serverless.start({"handler": handler})
        """,
    )

    assert flash_app.run_case.local(handler, {"id": "x", "input": {}}) == {
        "output": "async ok"
    }


def test_collects_async_generator_output(flash_app, tmp_path):
    handler = write_handler(
        tmp_path,
        """
        import runpod

        async def handler(job):
            yield "a"
            yield "b"

        runpod.serverless.start({"handler": handler})
        """,
    )

    assert flash_app.run_case.local(handler, {"id": "x", "input": {}}) == {
        "output": ["a", "b"]
    }


def test_fails_when_handler_file_never_starts_a_worker(flash_app, tmp_path):
    handler = write_handler(tmp_path, "def handler(job):\n    return 1\n")

    with pytest.raises(RuntimeError, match="never called runpod.serverless.start"):
        flash_app.run_case.local(handler, {"id": "x", "input": {}})


def test_restores_serverless_start_after_the_call(flash_app, tmp_path):
    import runpod.serverless

    original = runpod.serverless.start
    handler = write_handler(
        tmp_path,
        "import runpod\nrunpod.serverless.start({'handler': lambda job: 1})\n",
    )

    flash_app.run_case.local(handler, {"id": "x", "input": {}})

    assert runpod.serverless.start is original


def test_shipped_module_loads_on_worker_without_local_files(
    flash_app, tmp_path, monkeypatch
):
    # Live endpoints exec the whole module on the worker, where cases.py,
    # the tests file, and the action's env vars do not exist. Only the env
    # the endpoint was created with is there.
    from runpod.apps.serialization import get_function_source

    source = get_function_source(flash_app.run_case._fn)
    worker_env = flash_app.run_case.spec.env
    for name in ("RP_TEST_IMAGE", "RP_TEST_FILE", "RP_TEST_GPU", "RP_TEST_APP_NAME"):
        monkeypatch.delenv(name, raising=False)
    for name, value in worker_env.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setitem(sys.modules, "cases", None)  # any import of it fails

    namespace = {"__name__": "shipped"}
    exec(compile(source, "shipped.py", "exec"), namespace)
    handler = write_handler(
        tmp_path,
        "import runpod\nrunpod.serverless.start({'handler': lambda job: 'shipped'})\n",
    )

    assert namespace["run_case"].local(handler, {"id": "x", "input": {}}) == {
        "output": "shipped"
    }


def test_worker_env_carries_suite_env(tmp_path, monkeypatch):
    suite = tmp_path / "tests.json"
    suite.write_text(
        json.dumps(
            {
                "tests": [{"name": "basic", "input": {}}],
                "config": {"env": [{"key": "MODEL", "value": "turbo"}]},
            }
        )
    )
    monkeypatch.setenv("RP_TEST_IMAGE", "runpod/worker-example:ci")
    monkeypatch.setenv("RP_TEST_FILE", str(suite))
    monkeypatch.setenv("RP_TEST_GPU", "NVIDIA GeForce RTX 4090")
    monkeypatch.setenv("RP_TEST_APP_NAME", "ci-example-1-1")
    import flash_app as module

    env = importlib.reload(module).run_case.spec.env

    assert env["MODEL"] == "turbo"
    assert env["RP_TEST_IMAGE"] == "runpod/worker-example:ci"
