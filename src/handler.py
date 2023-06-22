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

REQUIRED_ENV_VARS = ["GITHUB_PAT", "RUNPOD_POD_ID"]

for var in REQUIRED_ENV_VARS:
    if var not in os.environ:
        raise Exception(f"Missing {var} environment variable")


def get_token():
    '''
    - Obtains registration token from GitHub
    '''
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

    return response.json()["token"]


def run_command(cmd, env=None):
    '''
    - Runs a command with subprocess and handles errors
    '''
    process = subprocess.Popen(cmd, shell=True, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, env=env)
    stdout, stderr = process.communicate()

    if process.returncode != 0:
        print(f'Subprocess ended with an error: {stderr.decode()}')
    else:
        print(f'Subprocess output: {stdout.decode()}')


def handler(event):
    '''
    - Obtains registration token from GitHub
    - Starts a runner
    '''
    token = get_token()

    # Configure runner
    config_cmd = f'./actions-runner/config.sh --url https://github.com/{ORG} --token {get_token()} --name {RUNNER_NAME} --work _work --labels runpod'
    run_command(config_cmd)

    # Remove unwanted environment variables
    runner_env = dict(os.environ)
    unwated_env_vars = ['RUNPOD_WEBHOOK_GET_JOB', 'RUNPOD_POD_ID', 'RUNPOD_WEBHOOK_GET_JOB',
                        'RUNPOD_WEBHOOK_POST_OUTPUT', 'RUNPOD_WEBHOOK_POST_STREAM', 'RUNPOD_WEBHOOK_PING', 'RUNPOD_AI_API_KEY']

    for var in unwated_env_vars:
        runner_env.pop(var, None)

    runner_env['RUNPOD_JOB_INPUT'] = event['input']

    # Start runner
    start_cmd = './actions-runner/run.sh --once'
    run_command(start_cmd, env=runner_env)

    # Remove runner
    remove_cmd = f'./actions-runner/config.sh remove --token {get_token()}'
    run_command(remove_cmd)

    return "Runner Exited"


runpod.serverless.start({"handler": handler, "refresh_worker": True})
