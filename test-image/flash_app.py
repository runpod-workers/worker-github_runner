"""Flash app that tests a worker image on a temporary live endpoint.

Run by the test-image action as `rp flash dev --once flash_app.py`. The dev
session creates a live endpoint from the worker image, runs `main`, and
deletes the endpoint when `main` returns or raises.

A live endpoint replaces the image's start command with the Flash runtime,
so the image's own entrypoint never runs. `run_case` drives the worker from
inside the image instead, in one of two modes:

- handler mode: load the handler file, capture the handler it passes to
  `runpod.serverless.start(...)`, and call it with each test input.
- server mode (load-balancer workers): start the image's HTTP server once,
  wait for its health path to return 200, and send each test's request to it.

Configured through environment variables (set by action.yml):
    RP_TEST_IMAGE          worker image to test (required)
    RP_TEST_FILE           Hub-format tests file (required)
    RP_TEST_APP_NAME       Flash app name, unique per run (required)
    RP_TEST_HANDLER        handler file inside the image (default /handler.py)
    RP_TEST_START_COMMAND  server command; setting it selects server mode
    RP_TEST_PORT           port the server listens on, passed as PORT (default 8000)
    RP_TEST_HEALTH_PATH    server health path (default /ping)
    RP_TEST_GPU            GPU type; defaults to config.gpuTypeId in the tests file
    RP_TEST_MIN_CUDA       minimum CUDA; defaults to the lowest allowedCudaVersions
    RP_TEST_STARTUP_S      seconds allowed for the worker or server to start (default 900)
"""

import asyncio
import inspect
import json
import os
import runpy
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import runpod

DEFAULT_HANDLER = "/handler.py"
DEFAULT_PORT = 8000
DEFAULT_HEALTH_PATH = "/ping"
DEFAULT_STARTUP_S = 900.0
# one worker at most; a CI endpoint never needs to scale
CI_WORKERS = (0, 1)
CI_IDLE_TIMEOUT_S = 5
HEALTH_POLL_S = 1.0
HEALTH_PROBE_TIMEOUT_S = 2.0
SERVER_LOG_TAIL_LINES = 50
# Flash execs this whole module on the worker too. The endpoint env carries
# this marker plus the values below, so the worker can rebuild the app
# without the tests file or cases.py, which exist only on the CI runner.
WORKER_MARKER = "RP_TEST_ON_WORKER"
FORWARDED = ("RP_TEST_IMAGE", "RP_TEST_GPU", "RP_TEST_APP_NAME")


def _required(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"{name} must be set")
    return value


if os.environ.get(WORKER_MARKER):
    SUITE = None
    IMAGE, GPU, APP_NAME = (_required(name) for name in FORWARDED)
    MIN_CUDA = os.environ.get("RP_TEST_MIN_CUDA") or None
    WORKER_ENV = {}
else:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from cases import check_http_output, check_output, http_request, load_suite

    SUITE = load_suite(Path(_required("RP_TEST_FILE")))
    IMAGE = _required("RP_TEST_IMAGE")
    APP_NAME = _required("RP_TEST_APP_NAME")
    GPU = os.environ.get("RP_TEST_GPU") or SUITE.gpu
    if not GPU:
        raise RuntimeError("set RP_TEST_GPU or config.gpuTypeId in the tests file")
    MIN_CUDA = os.environ.get("RP_TEST_MIN_CUDA") or SUITE.min_cuda
    WORKER_ENV = {
        **SUITE.env,
        WORKER_MARKER: "1",
        "RP_TEST_IMAGE": IMAGE,
        "RP_TEST_GPU": GPU,
        "RP_TEST_APP_NAME": APP_NAME,
        **({"RP_TEST_MIN_CUDA": MIN_CUDA} if MIN_CUDA else {}),
    }

HANDLER = os.environ.get("RP_TEST_HANDLER") or DEFAULT_HANDLER
START_COMMAND = os.environ.get("RP_TEST_START_COMMAND") or None
STARTUP_S = float(os.environ.get("RP_TEST_STARTUP_S") or DEFAULT_STARTUP_S)
SERVER = {
    "command": START_COMMAND,
    "port": int(os.environ.get("RP_TEST_PORT") or DEFAULT_PORT),
    "health_path": os.environ.get("RP_TEST_HEALTH_PATH") or DEFAULT_HEALTH_PATH,
    "startup_s": STARTUP_S,
}

app = runpod.App(APP_NAME)


# ---- worker side: handler mode ---------------------------------------------


async def _drain(result):
    if inspect.isawaitable(result):
        result = await result
    if inspect.isasyncgen(result):
        return [chunk async for chunk in result]
    return result


def _run_handler(handler_path: str, job: dict) -> dict:
    import runpod.serverless

    captured = {}
    original_start = runpod.serverless.start
    handler_dir = os.path.dirname(os.path.abspath(handler_path))
    original_cwd = os.getcwd()
    runpod.serverless.start = captured.update
    sys.path.insert(0, handler_dir)
    os.chdir(handler_dir)
    try:
        # run_name="__main__" also covers handlers that start the worker
        # under an `if __name__ == "__main__":` guard.
        runpy.run_path(handler_path, run_name="__main__")
        handler = captured.get("handler")
        if handler is None:
            raise RuntimeError(f"{handler_path} never called runpod.serverless.start")

        result = handler(job)
        if inspect.isgenerator(result):
            result = list(result)
        elif inspect.isawaitable(result) or inspect.isasyncgen(result):
            result = asyncio.run(_drain(result))
        return {"output": result}
    finally:
        runpod.serverless.start = original_start
        sys.path.remove(handler_dir)
        os.chdir(original_cwd)


# ---- worker side: server mode ----------------------------------------------


def _server_files(port: int):
    # Keyed by port and kept on disk, so a server started by one job is
    # found again by the next even if the runtime re-execs this module.
    return f"/tmp/rp-test-server-{port}.pid", f"/tmp/rp-test-server-{port}.log"


def _exit_code(pid: int):
    """The server's exit code if it has exited, else None."""
    try:
        done, status = os.waitpid(pid, os.WNOHANG)
    except ChildProcessError:  # not our child; fall back to a liveness probe
        try:
            os.kill(pid, 0)
            return None
        except ProcessLookupError:
            return -1
    return os.waitstatus_to_exitcode(status) if done else None


def _log_tail(log_file: str) -> str:
    try:
        with open(log_file, errors="replace") as f:
            return "".join(f.readlines()[-SERVER_LOG_TAIL_LINES:])
    except FileNotFoundError:
        return ""


def _health_status(port: int, path: str):
    try:
        url = f"http://127.0.0.1:{port}{path}"
        with urllib.request.urlopen(url, timeout=HEALTH_PROBE_TIMEOUT_S) as response:
            return response.status
    except urllib.error.HTTPError as exc:
        return exc.code
    except OSError:  # not listening yet
        return None


def _ensure_server(server: dict) -> None:
    port = server["port"]
    pid_file, log_file = _server_files(port)
    pid = int(Path(pid_file).read_text()) if os.path.exists(pid_file) else None
    if pid is None or _exit_code(pid) is not None:
        with open(log_file, "w") as log:
            process = subprocess.Popen(
                server["command"],
                shell=True,
                env={**os.environ, "PORT": str(port)},
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        pid = process.pid
        Path(pid_file).write_text(str(pid))

    deadline = time.monotonic() + server["startup_s"]
    while True:
        code = _exit_code(pid)
        if code is not None:
            os.remove(pid_file)
            raise RuntimeError(
                f"server command exited with code {code} before becoming healthy:\n"
                f"{_log_tail(log_file)}"
            )
        if _health_status(port, server["health_path"]) == 200:
            return
        if time.monotonic() > deadline:
            os.killpg(pid, signal.SIGTERM)
            os.remove(pid_file)
            raise TimeoutError(
                f"server was not healthy within {server['startup_s']:g}s:\n"
                f"{_log_tail(log_file)}"
            )
        time.sleep(HEALTH_POLL_S)


def _send(port: int, request: dict) -> dict:
    data = None if request["body"] is None else json.dumps(request["body"]).encode()
    http_request = urllib.request.Request(
        f"http://127.0.0.1:{port}{request['path']}",
        data=data,
        method=request["method"],
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(
            http_request, timeout=request["timeout_s"]
        ) as response:
            status, raw = response.status, response.read()
    except urllib.error.HTTPError as exc:
        status, raw = exc.code, exc.read()
    text = raw.decode(errors="replace")
    try:
        body = json.loads(text)
    except ValueError:
        body = text
    return {"status": status, "body": body}


@app.queue(
    name="worker",
    image=IMAGE,
    gpu=GPU,
    workers=CI_WORKERS,
    idle_timeout=CI_IDLE_TIMEOUT_S,
    env=WORKER_ENV,
    min_cuda_version=MIN_CUDA,
)
def run_case(task: dict) -> dict:
    """Run one test on the worker. `task` is either
    {"handler": path, "job": {...}} or {"server": {...}, "request": {...}}."""
    if "server" in task:
        _ensure_server(task["server"])
        return {"output": _send(task["server"]["port"], task["request"])}
    return _run_handler(task["handler"], task["job"])


# ---- CI runner side ----------------------------------------------------------


def _task_and_check(case):
    """The worker task for a test case and the function that checks its result."""
    if START_COMMAND:
        request = http_request(case.input)
        task = {
            "server": SERVER,
            "request": {**request, "timeout_s": case.timeout_s},
        }
        return task, lambda result: check_http_output(result, request["status"])
    task = {"handler": HANDLER, "job": {"id": f"ci-{case.name}", "input": case.input}}
    return task, check_output


@runpod.local_entrypoint
async def main():
    failures = []
    for case in SUITE.cases:
        task, check = _task_and_check(case)
        budget_s = STARTUP_S + case.timeout_s
        print(f"[{case.name}] submitting", flush=True)
        try:
            # without an explicit limit the endpoint's default execution
            # timeout can kill long tests (model downloads, training)
            caller = run_case.with_options(execution_timeout=int(budget_s * 1000))
            job = await caller.spawn.aio(task)
            result = await job.result.aio(timeout=budget_s)
        except Exception as exc:  # the job, the worker, or the wait failed
            failures.append(f"{case.name}: {type(exc).__name__}: {exc}")
            print(f"[{case.name}] FAILED: {exc}", flush=True)
            continue

        reason = check(result)
        if reason:
            failures.append(f"{case.name}: {reason}")
            print(f"[{case.name}] FAILED: {reason}", flush=True)
        else:
            print(f"[{case.name}] passed", flush=True)

    print(
        f"{len(SUITE.cases) - len(failures)}/{len(SUITE.cases)} tests passed",
        flush=True,
    )
    if failures:
        raise RuntimeError("worker tests failed:\n" + "\n".join(failures))
