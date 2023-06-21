'''
Starts a GitHub runner.
'''

import os
import requests
import subprocess

import runpod

GITHUB_TOKEN = os.environ.get("GITHUB_PAT")
ORG = os.environ.get("GITHUB_ORG", "runpod-workers")
RUNNER_NAME = os.environ.get("RUNNER_NAME", "runpod-runner")


if not GITHUB_TOKEN:
    raise Exception("Missing GITHUB_PAT environment variable")


def handler(event):
    '''
    - Obtains registration token from GitHub
    - Starts a runner
    '''

    # Get registration token
    headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "X-GitHub-Api-Version": "2022-11-28"
    }

    url = f"https://api.github.com/orgs/{ORG}/actions/runners/registration-token"
    response = requests.post(url, headers=headers, timeout=10)

    if response.status_code != 201:
        print(f"Status code: {response.status_code}")
        raise Exception(f"Failed to get registration token: {response.text}")

    registration_token = response.json()["token"]

    # Configure runner
    cmd = f'echo {RUNNER_NAME} | ./actions-runner/config.sh --url https://github.com/{ORG} --token {registration_token}'
    process = subprocess.Popen(cmd, shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    stdout, stderr = process.communicate()

    if process.returncode != 0:
        print(f'Subprocess ended with an error: {stderr.decode()}')
    else:
        print(f'Subprocess output: {stdout.decode()}')

    # Start runner
    start_cmd = './actions-runner/run.sh --once'
    start_process = subprocess.Popen(
        start_cmd, shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    start_stdout, start_stderr = start_process.communicate()

    if start_process != 0:
        print(f'Subprocess ended with an error: {start_stderr.decode()}')
    else:
        print(f'Subprocess output: {start_stdout.decode()}')

    return "Runner Exited"


runpod.serverless.start({"handler": handler, "refresh_worker": True})
