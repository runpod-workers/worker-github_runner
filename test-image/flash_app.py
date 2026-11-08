"""Flash app that tests a worker image on a temporary live endpoint.

Run by the test-image action as `rp flash dev --once flash_app.py`. The dev
session creates a live endpoint from the worker image, runs `main`, and
deletes the endpoint when `main` returns or raises.

A live endpoint replaces the image's start command with the Flash runtime,
so the image's own `runpod.serverless.start(...)` never runs. Instead,
`run_case` loads the worker's handler file inside the image and calls its
handler directly. The handler still runs with the image's code, models, and
libraries.

Configured through environment variables (set by action.yml):
    RP_TEST_IMAGE         worker image to test (required)
    RP_TEST_FILE          Hub-format tests file (required)
    RP_TEST_APP_NAME      Flash app name, unique per run (required)
    RP_TEST_HANDLER       handler file inside the image (default /handler.py)
    RP_TEST_GPU           GPU type; defaults to config.gpuTypeId in the tests file
    RP_TEST_STARTUP_S     seconds allowed for the first worker to start (default 900)
"""

import os
import sys
from pathlib import Path

import runpod

DEFAULT_HANDLER = "/handler.py"
DEFAULT_STARTUP_S = 900.0
# one worker at most; a CI endpoint never needs to scale
CI_WORKERS = (0, 1)
CI_IDLE_TIMEOUT_S = 5
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
    WORKER_ENV = {}
else:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from cases import check_output, load_suite

    SUITE = load_suite(Path(_required("RP_TEST_FILE")))
    IMAGE = _required("RP_TEST_IMAGE")
    APP_NAME = _required("RP_TEST_APP_NAME")
    GPU = os.environ.get("RP_TEST_GPU") or SUITE.gpu
    if not GPU:
        raise RuntimeError("set RP_TEST_GPU or config.gpuTypeId in the tests file")
    WORKER_ENV = {
        **SUITE.env,
        WORKER_MARKER: "1",
        "RP_TEST_IMAGE": IMAGE,
        "RP_TEST_GPU": GPU,
        "RP_TEST_APP_NAME": APP_NAME,
    }

HANDLER = os.environ.get("RP_TEST_HANDLER") or DEFAULT_HANDLER
STARTUP_S = float(os.environ.get("RP_TEST_STARTUP_S") or DEFAULT_STARTUP_S)

app = runpod.App(APP_NAME)


@app.queue(
    name="worker",
    image=IMAGE,
    gpu=GPU,
    workers=CI_WORKERS,
    idle_timeout=CI_IDLE_TIMEOUT_S,
    env=WORKER_ENV,
)
def run_case(handler_path: str, job: dict) -> dict:
    # Runs on the worker, so imports stay local to this function.
    import asyncio
    import inspect
    import os
    import runpy
    import sys

    import runpod.serverless

    async def _drain(result):
        if inspect.isawaitable(result):
            result = await result
        if inspect.isasyncgen(result):
            return [chunk async for chunk in result]
        return result

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


@runpod.local_entrypoint
async def main():
    failures = []
    for case in SUITE.cases:
        job_input = {"id": f"ci-{case.name}", "input": case.input}
        print(f"[{case.name}] submitting", flush=True)
        try:
            job = await run_case.spawn.aio(HANDLER, job_input)
            result = await job.result.aio(timeout=STARTUP_S + case.timeout_s)
        except Exception as exc:  # the job, the worker, or the wait failed
            failures.append(f"{case.name}: {type(exc).__name__}: {exc}")
            print(f"[{case.name}] FAILED: {exc}", flush=True)
            continue

        reason = check_output(result)
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
