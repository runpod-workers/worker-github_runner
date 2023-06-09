'''
Starts a GitHub runner.
'''

import os
import requests
import subprocess

import runpod

RUNNER_NAME = os.environ.get("RUNPOD_POD_ID", "serverless-runpod-runner")


def get_token(pat, org):
    '''
    - Obtains registration token from GitHub
    '''
    headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {pat}",
        "X-GitHub-Api-Version": "2022-11-28"
    }

    url = f"https://api.github.com/orgs/{org}/actions/runners/registration-token"
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

    event_input = event.get('input')

    # Get PAT
    if event_input.get('github_pat', None) is not None:
        pat = event_input.get('github_pat')
        event_input.pop('github_pat', None)
    elif os.environ.get("GITHUB_PAT", None) is not None:
        pat = os.environ.get("GITHUB_PAT")
    else:
        raise Exception("Missing GitHub Personal Access Token")

    # Get ORG
    if event_input.get('github_org', None) is not None:
        org = event_input.get('github_org')
        event_input.pop('github_org', None)
    elif os.environ.get("GITHUB_ORG", None) is not None:
        org = os.environ.get("GITHUB_ORG")
    else:
        raise Exception("Missing GitHub Organization")

    # Configure runner
    config_cmd = f'./actions-runner/config.sh --url https://github.com/{org} --token {get_token(pat, org)} --name {RUNNER_NAME} --work _work --labels runpod,{RUNNER_NAME}'
    run_command(config_cmd)

    # Remove unwanted environment variables
    runner_env = dict(os.environ)
    unwated_env_vars = ['RUNPOD_WEBHOOK_GET_JOB', 'RUNPOD_POD_ID', 'RUNPOD_WEBHOOK_GET_JOB',
                        'RUNPOD_WEBHOOK_POST_OUTPUT', 'RUNPOD_WEBHOOK_POST_STREAM', 'RUNPOD_WEBHOOK_PING', 'RUNPOD_AI_API_KEY']

    for var in unwated_env_vars:
        runner_env.pop(var, None)

    runner_env['JOB_INPUT'] = str(event_input)

    # Start runner
    start_cmd = './actions-runner/run.sh --once'
    run_command(start_cmd, env=runner_env)

    # Remove runner
    remove_cmd = f'./actions-runner/config.sh remove --token {get_token(pat, org)}'
    run_command(remove_cmd)

    return "Runner Exited"


runpod.serverless.start({"handler": handler, "refresh_worker": True})
