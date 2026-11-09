import importlib
import json
import sys
import textwrap
from pathlib import Path

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


def handler_task(handler, job):
    return {"handler": handler, "job": job}


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
        handler_task(handler, {"id": "ci-basic", "input": {"text": "hi"}})
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

    assert flash_app.run_case.local(
        handler_task(handler, {"id": "x", "input": {}})
    ) == {"output": "ok"}


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

    assert flash_app.run_case.local(
        handler_task(handler, {"id": "x", "input": {}})
    ) == {"output": 42}


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

    assert flash_app.run_case.local(
        handler_task(handler, {"id": "x", "input": {}})
    ) == {"output": [1, 2]}


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

    assert flash_app.run_case.local(
        handler_task(handler, {"id": "x", "input": {}})
    ) == {"output": "async ok"}


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

    assert flash_app.run_case.local(
        handler_task(handler, {"id": "x", "input": {}})
    ) == {"output": ["a", "b"]}


def test_fails_when_handler_file_never_starts_a_worker(flash_app, tmp_path):
    handler = write_handler(tmp_path, "def handler(job):\n    return 1\n")

    with pytest.raises(RuntimeError, match="never called runpod.serverless.start"):
        flash_app.run_case.local(handler_task(handler, {"id": "x", "input": {}}))


def test_restores_serverless_start_after_the_call(flash_app, tmp_path):
    import runpod.serverless

    original = runpod.serverless.start
    handler = write_handler(
        tmp_path,
        "import runpod\nrunpod.serverless.start({'handler': lambda job: 1})\n",
    )

    flash_app.run_case.local(handler_task(handler, {"id": "x", "input": {}}))

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

    assert namespace["run_case"].local(
        handler_task(handler, {"id": "x", "input": {}})
    ) == {"output": "shipped"}


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


# ---- server mode ------------------------------------------------------------

SERVER = """
import json, os, sys
from http.server import BaseHTTPRequestHandler, HTTPServer

with open(os.environ["STARTS_FILE"], "a") as f:
    f.write("start\\n")

class Handler(BaseHTTPRequestHandler):
    def _send(self, code, body):
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == "/ping":
            return self._send(200, {"status": "healthy"})
        self._send(404, {"detail": "not found"})

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length) or b"null")
        if self.path == "/fail":
            return self._send(500, {"detail": "boom"})
        self._send(200, {"echo": body})

    def log_message(self, *args):
        pass

HTTPServer(("127.0.0.1", int(os.environ["PORT"])), Handler).serve_forever()
"""


def free_port():
    import socket

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def server(tmp_path, monkeypatch):
    script = tmp_path / "server.py"
    script.write_text(SERVER)
    starts = tmp_path / "starts.txt"
    monkeypatch.setenv("STARTS_FILE", str(starts))
    port = free_port()
    spec = {
        "command": f"{sys.executable} {script}",
        "port": port,
        "health_path": "/ping",
        "startup_s": 20,
    }
    yield spec, starts
    import os
    import signal

    pid_file = f"/tmp/rp-test-server-{port}.pid"
    if os.path.exists(pid_file):
        try:
            os.killpg(int(Path(pid_file).read_text()), signal.SIGTERM)
        except ProcessLookupError:
            pass
        os.remove(pid_file)


def server_task(spec, method="GET", path="/ping", body=None, timeout_s=10):
    return {
        "server": spec,
        "request": {
            "method": method,
            "path": path,
            "body": body,
            "timeout_s": timeout_s,
        },
    }


def test_server_mode_starts_server_and_returns_response(flash_app, server):
    spec, _ = server

    result = flash_app.run_case.local(
        server_task(spec, "POST", "/echo", {"prompt": "hi"})
    )

    assert result == {"output": {"status": 200, "body": {"echo": {"prompt": "hi"}}}}


def test_server_mode_returns_error_statuses_instead_of_raising(flash_app, server):
    spec, _ = server

    result = flash_app.run_case.local(server_task(spec, "POST", "/fail", {}))

    assert result == {"output": {"status": 500, "body": {"detail": "boom"}}}


def test_server_mode_reuses_a_running_server(flash_app, server):
    spec, starts = server

    flash_app.run_case.local(server_task(spec))
    flash_app.run_case.local(server_task(spec))

    assert starts.read_text().count("start") == 1


def test_server_mode_starts_a_server_over_an_unreadable_pid_file(flash_app, server):
    spec, starts = server
    Path(f"/tmp/rp-test-server-{spec['port']}.pid").write_text("")

    result = flash_app.run_case.local(server_task(spec))

    assert result["output"]["status"] == 200
    assert starts.read_text().count("start") == 1


def test_server_mode_reports_a_crashed_server_with_its_log(flash_app, tmp_path):
    crash = tmp_path / "crash.py"
    crash.write_text(
        "raise ImportError('attempted relative import with no known parent package')\n"
    )
    spec = {
        "command": f"{sys.executable} {crash}",
        "port": free_port(),
        "health_path": "/ping",
        "startup_s": 20,
    }

    with pytest.raises(
        RuntimeError, match="exited with code 1(.|\\n)*attempted relative import"
    ):
        flash_app.run_case.local(server_task(spec))


def test_server_mode_times_out_when_never_healthy(flash_app, tmp_path):
    idle = tmp_path / "idle.py"
    idle.write_text("import time\ntime.sleep(60)\n")
    port = free_port()
    spec = {
        "command": f"{sys.executable} {idle}",
        "port": port,
        "health_path": "/ping",
        "startup_s": 2,
    }

    try:
        with pytest.raises(TimeoutError, match="not healthy within 2"):
            flash_app.run_case.local(server_task(spec))
    finally:
        import os
        import signal

        pid_file = f"/tmp/rp-test-server-{port}.pid"
        if os.path.exists(pid_file):
            os.killpg(int(Path(pid_file).read_text()), signal.SIGTERM)
            os.remove(pid_file)


def test_awaits_async_handler_when_called_inside_a_running_loop(flash_app, tmp_path):
    # The Flash runtime calls run_case from inside its own event loop.
    import asyncio

    handler = write_handler(
        tmp_path,
        """
        import runpod

        async def handler(job):
            return "inside loop ok"

        runpod.serverless.start({"handler": handler})
        """,
    )

    async def runtime():
        return flash_app.run_case.local(handler_task(handler, {"id": "x", "input": {}}))

    assert asyncio.run(runtime()) == {"output": "inside loop ok"}
