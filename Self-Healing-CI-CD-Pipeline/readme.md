# BharatCode Self-Healing CI/CD Agent

This service monitors failed GitHub Actions workflow runs, analyzes their logs with the BharatCode model API, and either suggests a manual fix or attempts a repository change on an isolated `AI_FIX-*` branch.

The service is built with FastAPI and LangGraph. Its repair agent uses the OpenAI-compatible BharatCode API with the `qwen-3.8-27b` model.

## Deployed Service

- Render service: <https://self-healing-ci-cd-bharatcode.onrender.com>
- FastAPI documentation: <https://self-healing-ci-cd-bharatcode.onrender.com/docs>
- GitHub webhook URL: <https://self-healing-ci-cd-bharatcode.onrender.com/webhook>

The app exposes a `POST /webhook` endpoint. The service root (`/`) is not an application page; use `/docs` to view the generated API documentation.

## What It Does

For a completed, failed `workflow_run` event, the service:

1. Ignores other events and non-failed workflow runs.
2. Uses the GitHub App to access the repository and confirm the run is the latest run for that workflow and branch.
3. Downloads and summarizes the workflow logs.
4. Classifies the failure as `auto`, `manual`, or `unknown`.
5. For an `auto` failure, creates a new `AI_FIX-*` branch or reuses the current `AI_FIX` branch, then asks the repair agent to inspect and make targeted file changes.
6. Commits changes to that fix branch when files were modified.
7. For `manual` or `unknown` failures, generates a suggested fix instead of changing files.

The service does not automatically open a pull request or merge changes. Review any `AI_FIX-*` branch before using it.

## Architecture

```mermaid
flowchart TD
    START --> download_logs
    download_logs --> summarize_logs
    summarize_logs --> fetch_tree
    fetch_tree --> classify_error
    classify_error -->|manual or unknown| suggest_fix
    suggest_fix --> END
    classify_error -->|auto, existing AI_FIX branch| set_branch
    classify_error -->|auto, other branch| create_branch
    set_branch --> solve_CICD
    create_branch --> solve_CICD
    solve_CICD --> commitMsg
    commitMsg --> commit
    commit --> END
```

The repair agent can read one or more files, search code on the repository's default branch, create a missing file, and make targeted edits to a file it has read. The code search tool searches the default branch; file reads and edits use the run's pinned branch revision.

## Repository Layout

```text
Self-Healing-CI-CD-Pipeline/
├── readme.md
└── Agent/
    ├── main.py           # FastAPI webhook endpoint
    ├── graph.py          # LangGraph workflow
    ├── function.py       # GitHub operations and workflow nodes
    ├── agent.py          # BharatCode-backed repair agent
    ├── agent_tools.py    # Repository read and edit tools
    ├── state.py          # Shared workflow state and output schemas
    └── requirements.txt  # Python dependencies
```

## Requirements

- Python 3.10 or later
- A BharatCode API key
- A GitHub App installed on each repository the agent should process
- A public deployment URL for GitHub to deliver webhook events

The GitHub App needs repository permissions:

- **Actions: Read** to inspect workflow runs and download logs
- **Contents: Read and write** to inspect files, create fix branches, and commit changes
- **Metadata: Read** to resolve repository details

Install the GitHub App on the target repository and configure its webhook as described below.

## Configuration

The Python code loads `.env` through `python-dotenv`. For local development, create `Agent/.env` with these settings:

```env
BHARATCODE_API_KEY=your_rotated_bharatcode_api_key
GITHUB_APP_ID=your_github_app_id
GITHUB_PRIVATE_KEY_PATH="-----BEGIN RSA PRIVATE KEY-----\nYOUR_PRIVATE_KEY_CONTENT\n-----END RSA PRIVATE KEY-----"

# Optional LangSmith tracing
LANGCHAIN_TRACING_V2=true
LANGCHAIN_API_KEY=your_langsmith_api_key
LANGCHAIN_PROJECT=self-healing-cicd
```

Despite its name, the current code passes `GITHUB_PRIVATE_KEY_PATH` directly to PyGithub as the private-key value. Set it to the PEM key contents, not a path to a local file. Keep all keys private; do not commit `.env` or publish private keys. Use a rotated BharatCode key if a key has been exposed.

For Render, add these values under the service's environment settings. Store the GitHub private key as a protected secret value containing the PEM text.

## Deploying on Render

Configure the Render web service with:

- **Root Directory:** `Agent`
- **Build Command:** `pip install -r requirements.txt`
- **Start Command:** `uvicorn main:app --host 0.0.0.0 --port $PORT`

Add `BHARATCODE_API_KEY`, `GITHUB_APP_ID`, and `GITHUB_PRIVATE_KEY_PATH` in the Render environment settings. Add the optional LangSmith variables only if tracing is enabled. Redeploy after changing environment variables.

## GitHub Webhook Setup

In the target GitHub repository, open **Settings → Webhooks → Add webhook** and configure:

- **Payload URL:** `https://self-healing-ci-cd-bharatcode.onrender.com/webhook`
- **Content type:** `application/json`
- **Events:** select **Workflow runs and Pushes** only

The handler processes `workflow_run` events only when `action` is `completed` and the run's `conclusion` is `failure`. Push events are not processed. After saving, GitHub's webhook page can show delivery attempts and response details.

## Run Locally

From the repository root in PowerShell:

```powershell
cd .\Agent
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python -m uvicorn main:app --reload
```

Create `Agent/.env` before starting the service. Open <http://127.0.0.1:8000/docs> to confirm the app started. The webhook endpoint is <http://127.0.0.1:8000/webhook>.

To test the BharatCode API independently from GitHub, run this from the `Agent` directory after installing dependencies and configuring `BHARATCODE_API_KEY`:

```powershell
python -c "import os; from dotenv import load_dotenv; from langchain_openai import ChatOpenAI; load_dotenv(); client=ChatOpenAI(model='qwen-3.8-27b', api_key=os.getenv('BHARATCODE_API_KEY'), base_url='https://bharatcode.ai/api/model/v1'); print(client.invoke('Reply exactly: BharatCode API works').content)"
```

## Operational Notes and Security

- Fixes are committed to an `AI_FIX-*` branch, not directly to the triggering branch. No pull request is created automatically.
- The webhook handler checks that an incoming run is still the latest run for its workflow and branch. It also ignores an `AI_FIX` branch after it is more than 10 commits ahead of the repository's default branch.
- Duplicate deliveries are tracked in an in-memory set. That tracking resets when the service restarts.
- **Webhook signature verification is not currently implemented.** The handler does not validate `X-Hub-Signature-256`; do not rely on a GitHub webhook secret to authenticate requests until signature validation is added to the application.
- Automated edits can be wrong. Inspect the fix branch and run the repository's checks before merging.
- The log summarizer sends workflow log content to the configured BharatCode model API. Ensure logs do not contain secrets or sensitive data before sending them to an external model provider.

## Current Limitations

- No automatic pull request creation or merge.
- No automatic workflow rerun after committing a fix.
- Email notification functions are placeholders and do not send messages.
- The in-memory duplicate-delivery tracking is not shared across service instances or preserved across restarts.
