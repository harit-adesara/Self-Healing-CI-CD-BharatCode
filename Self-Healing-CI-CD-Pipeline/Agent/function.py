import os
import uuid
import io
import zipfile
import requests
from state import Data,ErrorClassification,CommitMessage
from langchain_openai import ChatOpenAI
from github import Github, GithubIntegration
from github.Auth import AppAuth
from github import InputGitTreeElement
from langchain_core.messages import SystemMessage,HumanMessage
from github.GithubException import UnknownObjectException
from dotenv import load_dotenv
from langsmith import traceable
    
load_dotenv()

# BharatCode model API (OpenAI-compatible)
model = ChatOpenAI(
    model="qwen-3.8-27b",
    api_key=os.getenv("BHARATCODE_API_KEY"),
    base_url="https://bharatcode.ai/api/model/v1",
    max_tokens=16000,
    max_retries=3,
)

def get_github_client(installation_id: int):
    APP_ID = os.getenv("GITHUB_APP_ID")

    PRIVATE_KEY=os.getenv("GITHUB_PRIVATE_KEY_PATH")

    auth = AppAuth(APP_ID, PRIVATE_KEY)
    installation_auth = auth.get_installation_auth(installation_id)

    return Github(auth=installation_auth)

def get_installation_id(owner: str, repo: str) -> int:
    """Find the GitHub App installation id for a repo (used when the webhook
    payload has no `installation` field)."""
    APP_ID = os.getenv("GITHUB_APP_ID")
    PRIVATE_KEY = os.getenv("GITHUB_PRIVATE_KEY_PATH")

    auth = AppAuth(APP_ID, PRIVATE_KEY)
    gi = GithubIntegration(auth=auth)

    return gi.get_repo_installation(owner, repo).id


def _to_text(content) -> str:
    """Gemini returns content as a list of blocks; always store plain text."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            p.get("text", "") if isinstance(p, dict) else str(p) for p in content
        )
    return str(content)


@traceable(name="Summarize logs")
def summarize_logs_node(state: Data):
    raw_logs = state['logs'] or ""

    if not raw_logs:
        return state

    system_prompt = (
        "You are analyzing CI/CD failure logs. Extract ONLY the parts "
        "that are relevant to diagnosing why the build/test failed. "
        "This includes: the actual error message(s), stack trace(s), "
        "failing test name(s), and a few lines of surrounding context "
        "immediately before/after each error. "
        "Discard: successful step logs, dependency install output, "
        "cache hits, timing/progress noise, and anything unrelated "
        "to the failure. "
        "Return only the extracted relevant log content, no commentary."
    )

    messages=[
        SystemMessage(content=system_prompt),
        HumanMessage(content=f"Here are the raw CI logs:\n\n{raw_logs}")
    ]

    result=model.invoke(messages)
    data=_to_text(result.content)

    return {
        "logs":data
    }

@traceable(name="fetch tree")
def fetch_tree(state: Data):
    """
    Fetch all file paths in the repository from the PINNED working_sha —
    the exact commit already verified as current by main.py before this
    run started.

    Deliberately does NOT re-fetch the branch's live head here. Doing so
    would risk returning a different commit than what main.py pinned (if
    a new commit lands on the branch mid-run), which would make the tree
    built here inconsistent with the commit every other node/tool in this
    run is reading from — reintroducing the exact race condition working_sha
    exists to prevent.
    """

    github = get_github_client(state["installation_id"])
    repo = github.get_repo(f"{state['owner']}/{state['repo']}")

    ref_sha = state["working_sha"]

    tree = repo.get_git_tree(ref_sha, recursive=True)

    repository_tree = [
        item.path
        for item in tree.tree
        if item.type == "blob"
    ]

    return {
        "repository_tree": repository_tree,
    }

@traceable(name="create branch")
def create_branch(state: Data):
    """
    Create a new branch from the PINNED working_sha — the same commit
    fetch_tree already built its tree from — so the tree the agent
    reasoned about and the branch it commits fixes to are guaranteed
    to be the same snapshot, regardless of what else lands on the
    source branch while this run is in progress.
    """

    github = get_github_client(state["installation_id"])
    repo = github.get_repo(f"{state['owner']}/{state['repo']}")

    ref_sha = state["working_sha"]
    branch_name = f"AI_FIX-{uuid.uuid4().hex[:8]}"

    repo.create_git_ref(
        ref=f"refs/heads/{branch_name}",
        sha=ref_sha
    )

    return {
        "branch_name": branch_name,
    }

@traceable(name="set branch")
def set_branch(state: Data):
    """
    Reused AI_FIX branch case (a workflow re-ran on a branch the agent
    already created in a previous run).

    working_sha was already pinned in main.py from the webhook's
    head_sha — which, for a re-run on an existing AI_FIX branch, IS
    that branch's tip at trigger time (a workflow only fires against a
    real commit that exists on the branch, and main.py already verified
    via the run-ID check that this is the latest run for it). No
    re-fetch needed; using the pinned value keeps this run internally
    consistent with fetch_tree and create_branch.
    """

    return {
        "branch_name": state["branch"],
    }

@traceable(name="download logs")
def download_workflow_logs(state: Data):
    """
    Download and extract GitHub Actions workflow logs.
    """

    github = get_github_client(state["installation_id"])

    token = github._Github__requester.auth.token

    url = (
        f"https://api.github.com/repos/"
        f"{state['owner']}/{state['repo']}"
        f"/actions/runs/{state['workflow_run_id']}/logs"
    )

    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
    }

    response = requests.get(url, headers=headers)
    response.raise_for_status()

    logs = ""

    with zipfile.ZipFile(io.BytesIO(response.content)) as zip_file:
        for name in zip_file.namelist():
            if name.endswith(".txt"):
                logs += f"\n\n===== {name} =====\n"
                logs += zip_file.read(name).decode("utf-8", errors="ignore")

    print("download logs")

    return {
        "logs": logs
    }

@traceable(name="check_branch")
def check_branch(state):
    """
    Nested routing after classification:
    1. If the error isn't auto-fixable, go straight to suggest_fix.
    2. If it is auto-fixable, check the branch — if already on an
       AI-generated fix branch, skip creating a new branch; otherwise
       create one first (to avoid infinite self-healing loops).
    """

    if state["fixability"] != "auto":
        return "suggest"

    branch = state["branch"].strip()

    if branch.startswith("AI_FIX"):
        return "set_branch"

    return "create_branch"

@traceable(name="classify error")
def classify_error(state:Data):
    """
    CI/CD error classification node
    """

    prompt = f"""
You are an expert CI/CD failure analysis agent.

Analyze the GitHub Actions logs and determine whether the failure can be fixed automatically.

Return exactly one value for fixability:

1. auto
The issue can likely be fixed by modifying files in the repository, such as:
- Source code
- Workflow YAML
- Dockerfile
- package.json
- requirements.txt
- Build scripts
- Configuration files

Examples:
- Dependency installation failures
- Syntax errors
- Test failures
- Build failures
- Workflow YAML mistakes
- Missing imports
- Incorrect versions

2. manual
The issue requires human intervention and cannot be fixed by editing repository files.

Examples:
- Missing GitHub Secrets
- Invalid cloud credentials
- Permission denied
- Repository settings
- Branch protection
- External service outage
- Network failures
- Expired tokens

3. unknown
There is not enough information in the logs to determine whether the issue is automatically fixable.

Also provide:
- confidence (0-100)
- reason 

Workflow Logs:
{state["logs"]}

Repository Tree:
{state["repository_tree"]}

Return only the structured output.
"""

    structure_output=model.with_structured_output(ErrorClassification, method="function_calling")
    result=structure_output.invoke(prompt)
    data=result.model_dump()

    print("classify error")

    return {
        "error_type":data["error_type"],
        "fixability":data["fixability"],
        "confidence":data["confidence"],
        "reason":data["reason"]
    }

@traceable(name="suggest fix")
def suggestFix(state:Data):
    prompt = f"For this error {state['reason']} suggest fix how to solve this"

    result=_to_text(model.invoke(prompt).content)

    print("suggest fix")
    return {
        "suggested_changes":result
    }

def sendEmail():
    return

@traceable(name="commit")
def commit(state: Data):
    """
    Commit all modified files to an existing branch.
    Assumes the branch has already been created or selected.
    """

    if not state["modified_files"]:
        print("commit skipped: no modified files")
        return {
            "success": False,
            "reason": "No modified files to commit."
        }

    github = get_github_client(state["installation_id"])

    repo = github.get_repo(
        f"{state['owner']}/{state['repo']}"
    )

    try:
        ref = repo.get_git_ref(
            f"heads/{state['branch_name']}"
        )
    except UnknownObjectException:
        return {
            "success": False,
            "reason": f"Branch '{state['branch_name']}' does not exist."
        }

    latest_commit = repo.get_git_commit(ref.object.sha)

    base_tree = repo.get_git_tree(latest_commit.tree.sha)

    tree_elements = []

    for path, content in state["modified_files"].items():
        tree_elements.append(
            InputGitTreeElement(
                path=path,
                mode="100644",
                type="blob",
                content=content,
            )
        )

    new_tree = repo.create_git_tree(
        tree=tree_elements,
        base_tree=base_tree,
    )

    new_commit = repo.create_git_commit(
        message=state.get("commitMsg") or "Fix CI/CD pipeline",
        tree=new_tree,
        parents=[latest_commit],
    )

    ref.edit(new_commit.sha)

    print(f"Committed to {state['branch_name']}")

    return {
        "commit_sha_new": new_commit.sha,
        "success": True,
    }

def successMail():
    return

@traceable(name="commit message")
def commitMsg(state:Data):

    structured_llm = model.with_structured_output(CommitMessage, method="function_calling")

    prompt=f"""
    A CI/CD pipeline failure has been fixed.

    Root Cause:
    {state["root_cause"]}

    Original Error:
    {state["reason"]}

    Files Modified:
    {list(state["modified_files"].keys())}

    Generate a concise Git commit message.

    Rules:
    - Maximum 72 characters.
    - Use imperative mood.
    - Describe the actual change.
    - Do not mention AI.
    - Do not include quotes.
    """

    result = structured_llm.invoke(prompt)
    data=result.model_dump()

    print("commitMsg")

    return {
    "commitMsg": data["commit_message"]
    }