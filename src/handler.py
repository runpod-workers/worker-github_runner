'''
Starts a GitHub runner.
'''

import os
import requests
import subprocess

import runpod

GITHUB_TOKEN = os.environ.get("GITHUB_PAT")
ORG = os.environ.get("GITHUB_ORG", "runpod-workers")

RUNNER_NAME = os.environ.get("RUNPOD_POD_ID", "serverless-runpod-runner")


if not GITHUB_TOKEN:
    raise Exception("Missing GITHUB_PAT environment variable")


def get_token():
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

    return registration_token


def handler(event):
    '''
    - Obtains registration token from GitHub
    - Starts a runner
    '''
    # Configure runner
    cmd = f'./actions-runner/config.sh --url https://github.com/{ORG} --token {get_token()} --name {RUNNER_NAME} --work _work --labels runpod'
    process = subprocess.Popen(cmd, shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    stdout, stderr = process.communicate()

    if process.returncode != 0:
        print(f'Subprocess ended with an error: {stderr.decode()}')
    else:
        print(f'Subprocess output: {stdout.decode()}')

    # Remove unwanted environment variables
    runner_env = dict(os.environ)
    unwated_env_vars = ['RUNPOD_WEBHOOK_GET_JOB', 'RUNPOD_POD_ID', 'RUNPOD_WEBHOOK_GET_JOB',
                        'RUNPOD_WEBHOOK_POST_OUTPUT', 'RUNPOD_WEBHOOK_POST_STREAM', 'RUNPOD_WEBHOOK_PING', 'RUNPOD_AI_API_KEY']

    for var in unwated_env_vars:
        runner_env.pop(var, None)

    # Start runner
    start_cmd = './actions-runner/run.sh --once'
    start_process = subprocess.Popen(
        start_cmd, shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=runner_env)
    start_stdout, start_stderr = start_process.communicate()

    if start_process != 0:
        print(f'Subprocess ended with an error: {start_stderr.decode()}')
    else:
        print(f'Subprocess output: {start_stdout.decode()}')

    # Remove runner
    remove_cmd = f'./actions-runner/config.sh remove --token {get_token()}'
    remove_process = subprocess.Popen(
        remove_cmd, shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    remove_stdout, remove_stderr = remove_process.communicate()

    if remove_process != 0:
        print(f'Subprocess ended with an error: {remove_stderr.decode()}')
    else:
        print(f'Subprocess output: {remove_stdout.decode()}')

    return "Runner Exited"


runpod.serverless.start({"handler": handler, "refresh_worker": True})
