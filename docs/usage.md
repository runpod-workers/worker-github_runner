# Documentation

## Testing a worker image in CI

The `test-image` action runs a worker image's test cases on a temporary serverless endpoint and deletes the endpoint afterwards. Each run gets its own endpoint, so CI does not depend on a long-lived endpoint that can be paused for being idle.

It uses Flash from runpod-python (`rp flash dev --once`):

1. The CLI creates a live endpoint from your image, with at most one worker.
2. For each test in your tests file, it sends a job to that endpoint. What the job does on the worker depends on the mode:
   - **Handler mode** (default, queue workers): load your handler file, capture the handler it registers with `runpod.serverless.start(...)`, and call it with the test input.
   - **Server mode** (load-balancer workers, selected by `start-command`): start your server once with `PORT` set, wait until its health path returns 200, and send the test's HTTP request to it.
3. A handler-mode test fails if the job fails, times out, returns no output, or returns a dict with an `error` key. A server-mode test fails if the server exits or never becomes healthy, or the response status differs from the expected one.
4. The endpoint is deleted when the tests finish, fail, or the run is cancelled.

### Usage

Build and push the image first, then:

```yaml
- uses: runpod-workers/worker-github_runner/test-image@main
  with:
    image: runpod/worker-example:ci-pr-${{ github.event.number }}
    runpod-api-key: ${{ secrets.RUNPOD_API_KEY }}
    handler: /handler.py
```

| Input | Default | Description |
|---|---|---|
| `image` | (required) | Worker image to test |
| `runpod-api-key` | (required) | API key used to create and delete the endpoint |
| `tests-file` | `.runpod/tests.json` | Hub-format tests file in your checkout |
| `handler` | `/handler.py` | Path of the handler file inside the image (handler mode) |
| `start-command` | (none) | Command that starts your HTTP server; selects server mode. Runs on the worker through `sh -c` |
| `port` | `8000` | Port your server listens on, passed to it as `PORT` (server mode) |
| `health-path` | `/ping` | Path that returns 200 once the server is ready (server mode) |
| `gpu` | `config.gpuTypeId` from the tests file | GPU type for the endpoint |
| `min-cuda` | lowest `config.allowedCudaVersions` | Minimum host CUDA version for the endpoint |
| `startup-timeout` | `900` | Seconds allowed for the worker to pull the image and start, and in server mode for the server to become healthy |
| `runpod-version` | `2.1.0` | runpod-python version for the CLI and the worker |
| `runtime-package-spec` | pinned `runpod-sdk-runtime` | Flash worker runtime installed into the image |

### Tests file

The action reads the same format as the Hub:

```json
{
  "tests": [
    {"name": "basic", "input": {"prompt": "hello"}, "timeout": 10000}
  ],
  "config": {
    "gpuTypeId": "NVIDIA GeForce RTX 4090",
    "env": [{"key": "MODEL", "value": "base"}]
  }
}
```

`timeout` is in milliseconds and also sets the job's execution timeout, together with `startup-timeout`. Entries in `config.env` are set on the endpoint. The lowest entry of `config.allowedCudaVersions` becomes the endpoint's minimum CUDA version.

In server mode, each test's `input` is an HTTP request. `method` defaults to `GET` and `status` (the expected response status) to `200`:

```json
{
  "tests": [
    {
      "name": "completion",
      "input": {"method": "POST", "path": "/v1/completions", "body": {"prompt": "Hello", "max_tokens": 8}},
      "timeout": 120000
    }
  ]
}
```

```yaml
- uses: runpod-workers/worker-github_runner/test-image@main
  with:
    image: runpod/worker-example-lb:ci-pr-${{ github.event.number }}
    runpod-api-key: ${{ secrets.RUNPOD_API_KEY }}
    start-command: cd /src && python3 handler.py
```

### What can go wrong

- **The image's start command does not run.** A live endpoint starts the Flash runtime instead of the image's start command. In handler mode the action calls your handler directly, so code that runs only in the start command (for example, a shell script that runs before `python handler.py`) is not exercised. In server mode, `start-command` should match the image's start command.
- **The worker runs `runpod==<runpod-version>`.** The endpoint installs that version into the image before starting, even if the image pins another one. A handler that only works with an older SDK will fail here.
- **The image needs Python 3.10 or newer, with `pip` on its `python3`,** so the runtime can be installed. Images built on a uv-managed venv without pip need `uv pip install pip` in their Dockerfile.
- **Pull requests from forks** do not get repository secrets, so the action cannot create an endpoint for them. Skip the job for forks.
