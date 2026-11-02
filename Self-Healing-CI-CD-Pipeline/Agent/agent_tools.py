from typing import Annotated
from langgraph.prebuilt import InjectedState
from langgraph.types import Command
from langchain_core.tools import tool, InjectedToolCallId
from state import Data
from typing import Literal
from function import get_github_client
from dotenv import load_dotenv

load_dotenv()


def _resolve_ref(state: Data):
    """
    Use the ref pinned once at the start of this run (working_sha), so all
    reads within a single agent session see a consistent, unmoving snapshot
    of the repository — even if the branch receives new commits mid-run.
    Falls back to branch_name / branch only if working_sha was never set
    (should not normally happen, since main.py sets it before invoking
    the graph).
    """
    return state.get("working_sha") or state.get("branch_name") or state["branch"]


@tool
def read_file(
    state: Annotated[Data, InjectedState],
    tool_call_id: Annotated[str, InjectedToolCallId],
    file_path: str,
):
    """
    Read the complete contents of a repository file.

    If this file has already been modified during this session, this
    returns the in-progress modified version instead of the original
    GitHub content — never re-fetches a file that has already been
    edited, since that would return stale/pre-fix content.
    """
    if file_path in state.get("modified_files", {}):
        content = state["modified_files"][file_path]
        return Command(update={
            "messages": [
                {
                    "role": "tool",
                    "content": f"Contents of '{file_path}' (already modified in this session):\n\n{content}",
                    "tool_call_id": tool_call_id,
                }
            ],
        })

    try:
        github = get_github_client(state["installation_id"])
        repo = github.get_repo(f"{state['owner']}/{state['repo']}")
        file = repo.get_contents(file_path, ref=_resolve_ref(state))
        content = file.decoded_content.decode("utf-8")
    except Exception as e:
        return f"Error reading '{file_path}': {e}"

    merged = dict(state["file_contents"])
    merged[file_path] = content

    return Command(update={
        "file_contents": merged,
        "messages": [
            {
                "role": "tool",
                "content": f"Contents of '{file_path}':\n\n{content}",
                "tool_call_id": tool_call_id,
            }
        ],
    })


@tool
def read_multiple_files(
    state: Annotated[Data, InjectedState],
    tool_call_id: Annotated[str, InjectedToolCallId],
    file_paths: list[str],
):
    """
    Read the complete contents of multiple repository files at once.

    For any file already modified during this session, this returns the
    in-progress modified version instead of re-fetching the original
    from GitHub.
    """
    github = get_github_client(state["installation_id"])
    repo = github.get_repo(f"{state['owner']}/{state['repo']}")
    ref = _resolve_ref(state)
    merged = dict(state["file_contents"])
    modified = state.get("modified_files", {})
    contents_summary = []

    for path in file_paths:
        if path in modified:
            content = modified[path]
            contents_summary.append(f"--- {path} (already modified in this session) ---\n{content}")
            continue

        try:
            file = repo.get_contents(path, ref=ref)
            content = file.decoded_content.decode("utf-8")
            merged[path] = content
            contents_summary.append(f"--- {path} ---\n{content}")
        except Exception as e:
            print(f"Error reading {path}: {e}")
            contents_summary.append(f"--- {path} ---\nError reading file: {e}")

    summary_text = "\n\n".join(contents_summary) if contents_summary else "No files could be read."

    return Command(update={
        "file_contents": merged,
        "messages": [
            {
                "role": "tool",
                "content": summary_text,
                "tool_call_id": tool_call_id,
            }
        ],
    })


@tool
def search_code(state: Annotated[Data, InjectedState], query: str):
    """
    Search the DEFAULT branch of the repository to locate files, imports,
    classes, functions, workflows, or configuration.

    NOTE: This tool only searches the repository's default branch (e.g. main),
    regardless of which branch is currently being worked on. It cannot see
    changes made on AI_FIX branches. For branch-accurate results, prefer
    checking `repository_tree` (already scoped to the current branch) and
    use read_file/read_multiple_files to inspect actual current contents.
    """
    try:
        github = get_github_client(state["installation_id"])
        search_query = f"{query} repo:{state['owner']}/{state['repo']}"
        results = github.search_code(search_query)
        files = [result.path for result in results]
        return f"Found files: {files}"
    except Exception as e:
        return f"Error searching code: {e}"


@tool
def create_file(
    state: Annotated[Data, InjectedState],
    tool_call_id: Annotated[str, InjectedToolCallId],
    file_path: str,
    content: str,
):
    """
    Create a new repository file only if it does not already exist.

    Before creating dependency files such as:

    - requirements.txt
    - package.json
    - pyproject.toml
    - Pipfile

    you MUST inspect the project's source code using read_file or
    read_multiple_files to determine the actual dependencies.

    Never create dependency files from assumptions.

    Never create empty dependency files.

    Create only files required to resolve the CI/CD failure.
    """
    if (
        file_path in state["file_contents"]
        or file_path in state["repository_tree"]
        or file_path in state.get("modified_files", {})
    ):
        return f"Error: '{file_path}' already exists. Use update_file instead."

    modified = dict(state["modified_files"])
    modified[file_path] = content

    return Command(update={
        "modified_files": modified,
        "messages": [
            {
                "role": "tool",
                "content": f"Created '{file_path}' and staged it for commit.",
                "tool_call_id": tool_call_id,
            }
        ],
    })


@tool
def update_file(
    state: Annotated[Data, InjectedState],
    tool_call_id: Annotated[str, InjectedToolCallId],
    file_path: str,
    operation: Literal["replace", "insert_after", "insert_before", "delete"],
    target: str,
    content: str = "",
):
    """
    Modify an existing repository file.

    Only use after reading the file and confirming the required change.

    Make the smallest possible modification that fixes the issue.

    Never overwrite unrelated code.
    """
    if file_path in state["modified_files"]:
        file_content = state["modified_files"][file_path]
    elif file_path in state["file_contents"]:
        file_content = state["file_contents"][file_path]
    else:
        return f"Error: '{file_path}' has not been read yet. Call read_file first, or use create_file if it's a new file."

    try:
        if operation == "replace":
            if target not in file_content:
                return f"Error: target text not found in '{file_path}'. Current file content is:\n\n{file_content}"
            updated = file_content.replace(target, content, 1)
        elif operation == "delete":
            if target not in file_content:
                return f"Error: target text not found in '{file_path}'. Current file content is:\n\n{file_content}"
            updated = file_content.replace(target, "", 1)
        elif operation == "insert_after":
            if target not in file_content:
                return f"Error: target text not found in '{file_path}'. Current file content is:\n\n{file_content}"
            updated = file_content.replace(target, target + "\n" + content, 1)
        elif operation == "insert_before":
            if target not in file_content:
                return f"Error: target text not found in '{file_path}'. Current file content is:\n\n{file_content}"
            updated = file_content.replace(target, content + "\n" + target, 1)
        else:
            return "Error: invalid operation."
    except Exception as e:
        return f"Error updating '{file_path}': {e}"

    modified = dict(state["modified_files"])
    modified[file_path] = updated

    return Command(update={
        "modified_files": modified,
        "messages": [
            {
                "role": "tool",
                "content": f"Successfully updated '{file_path}'.",
                "tool_call_id": tool_call_id,
            }
        ],
    })