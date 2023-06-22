# Documentation

## CI/CD with GitHub Actions and Runpod

This repository uses GitHub Actions to run a series of tests on the Runpod serverless platform. The workflow is defined in the `.github/workflows/test-worker.yml` file, with the primary purpose to deploy a worker to the Runpod platform, execute the tests, and then terminate the worker.

Here is a step-by-step breakdown of the `test-worker.yml` file:

### Triggers

This workflow runs whenever:

- A push is made to the main branch.
- A pull request is opened to the main branch.
- The workflow is manually triggered (workflow_dispatch).

### Jobs

#### `initialize_worker`

This job deploys a worker to the Runpod platform. It:

- Sends a POST request to the Runpod API to start a new job (uses `fjogeleit/http-request-action@v1`).
- Extracts the job ID from the response and sets it as a job output.

#### `run_tests`

This job runs the tests in the worker environment. It:

- Checks out the repository code.
- Sets up Python 3.11 and installs dependencies.
- Executes tests by running `handler.py` with a test input.

#### `terminate_worker`

This job terminates the worker in the Runpod platform. It:

- Sends a POST request to the Runpod API to cancel the job (uses `fjogeleit/http-request-action@v1`).

It only runs if the previous job (`initialize_worker` or `run_tests`) was cancelled or failed.

### Secrets

The workflow requires the following secrets to be set in the repository settings:

- `RUNPOD_API_KEY`: The API key to authenticate requests to the Runpod API.
- `RUNPOD_ENDPOINT`: The endpoint of the Runpod API (not including the base URL).

### Customizing the Workflow

This workflow can be customized by editing the `.github/workflows/test-worker.yml` file. For example, to change the Python version, edit the `python-version` input in the `Set up Python 3.11 & install dependencies` step. To add or modify tests, edit the `Execute Tests` step.

**Note:** Always ensure that the necessary permissions are granted when modifying GitHub workflows. It's also a good practice to validate your changes with a trusted colleague or through a pull request to prevent accidental disruptions to the workflow.
