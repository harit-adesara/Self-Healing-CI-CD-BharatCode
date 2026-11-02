from typing import List, Dict, Optional, Literal, TypedDict, Annotated
from pydantic import BaseModel, Field
from langgraph.graph.message import add_messages


class Data(TypedDict):
    messages: Annotated[list, add_messages]
    owner: str
    repo: str
    installation_id: int
    workflow_run_id: int
    workflow_name: str
    workflow_file: str
    branch: str
    commit_sha: str
    working_sha: Optional[str]
    error_type: str
    fixability: str
    reason: str
    confidence: Optional[float]
    logs: str
    repository_tree: List[str]
    file_contents: Dict[str, str]
    root_cause: Optional[str]
    suggested_changes: Optional[str]
    modified_files: Dict[str, str]
    branch_name: Optional[str]
    commitMsg: str
    commit_sha_new: Optional[str]
    success: bool


class ErrorClassification(BaseModel):
    error_type: str = Field(description="The type of error that caused the workflow failure (e.g., 'DependencyError', 'SyntaxError', 'PermissionError').")
    fixability: Literal["auto", "manual", "unknown"] = Field(
        description=(
            "Whether the CI/CD failure can be fixed automatically by modifying "
            "repository files. "
            "'auto' = the issue can likely be resolved by editing files in the repository "
            "(e.g., source code, package.json, requirements.txt, Dockerfile, or GitHub workflow files). "
            "'manual' = the issue requires human intervention because it cannot be fixed "
            "by modifying repository files (e.g., missing GitHub Secrets, environment variables, "
            "cloud credentials, permissions, or external infrastructure). "
            "'unknown' = there is not enough information to determine whether the issue is auto-fixable."
        )
    )
    confidence: int = Field(
        description="Confidence score for the classification, ranging from 0 to 100.",
        ge=0, le=100
    )
    reason: str = Field(
        description="The actual error that caused the workflow failure. Do not return generic messages like 'Job failed' or 'Process exited with code 1'. Explain error in details why it caused and where it caused."
    )


class CommitMessage(BaseModel):
    commit_message: str = Field(
        description="A concise Git commit message (maximum 72 characters)."
    )