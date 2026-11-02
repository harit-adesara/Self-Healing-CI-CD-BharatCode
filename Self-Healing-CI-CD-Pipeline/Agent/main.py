from fastapi import FastAPI, Request
from function import get_github_client, get_installation_id
from graph import workflow
from state import Data
import traceback

app = FastAPI()

processed_runs = set()


@app.post("/webhook")
async def github_webhook(request: Request):

    print("\n========== WEBHOOK RECEIVED ==========", flush=True)

    try:
        # 1. Read webhook payload
        payload = await request.json()

        event = request.headers.get("X-GitHub-Event")
        action = payload.get("action")

        workflow_run = payload.get("workflow_run", {})
        conclusion = workflow_run.get("conclusion")

        branch = workflow_run.get("head_branch", "")
        run_id = workflow_run.get("id")

        print(f"Event: {event}", flush=True)
        print(f"Action: {action}", flush=True)
        print(f"Conclusion: {conclusion}", flush=True)
        print(f"Branch: {branch}", flush=True)
        print(f"Run ID: {run_id}", flush=True)

        # 2. Filter failed workflow runs only
        if (
            event != "workflow_run"
            or action != "completed"
            or conclusion != "failure"
        ):
            print("❌ EVENT FILTER FAILED - Ignoring webhook", flush=True)

            run_key = f"{run_id}-{workflow_run.get('run_attempt', 1)}"


            return {
                "status": "ignored",
                "reason": (
                    f"event={event}, "
                    f"action={action}, "
                    f"conclusion={conclusion}"
                ),
            }

        print("✅ EVENT FILTER PASSED", flush=True)

        run_key = f"{run_id}-{workflow_run.get('run_attempt', 1)}"

        if run_key in processed_runs:
            print("❌ DUPLICATE DELIVERY - Ignoring", flush=True)
            return {"status": "ignored", "reason": "duplicate_run"}

        processed_runs.add(run_key)
        # 3. Get repository information
        repo_info = payload.get("repository", {})

        owner = repo_info.get("owner", {}).get("login")
        repo_name = repo_info.get("name")

        workflow_id = workflow_run.get("workflow_id")

        print(f"Owner: {owner}", flush=True)
        print(f"Repository: {repo_name}", flush=True)
        print(f"Workflow ID: {workflow_id}", flush=True)

        if not owner or not repo_name:
            return {
                "status": "error",
                "message": "Missing repository owner or name",
            }

        # 4. Get GitHub App installation ID
        installation = payload.get("installation")

        if installation and "id" in installation:
            installation_id = installation["id"]
        else:
            print("⚠️ Installation missing in payload, looking it up...", flush=True)
            try:
                installation_id = get_installation_id(owner, repo_name)
            except Exception as e:
                print("❌ Installation lookup failed:", repr(e), flush=True)

                return {
                    "status": "error",
                    "message": (
                        "Could not find installation_id "
                        f"(is the app installed on {owner}/{repo_name}?): {e}"
                    ),
                }

        print(f"Installation ID: {installation_id}", flush=True)

        # 5. Authenticate GitHub App
        print("🔐 Authenticating GitHub App...", flush=True)

        try:
            github = get_github_client(installation_id)
            repo = github.get_repo(f"{owner}/{repo_name}")

            print("✅ GitHub authentication successful", flush=True)

        except Exception as e:
            print("❌ GitHub authentication failed:", repr(e), flush=True)

            return {
                "status": "error",
                "message": f"Could not authenticate/resolve repo: {e}",
            }

        # 6. Get latest workflow run
        print("🔍 Checking latest workflow run...", flush=True)

        try:
            gh_workflow = repo.get_workflow(workflow_id)

            runs = gh_workflow.get_runs(branch=branch)

            latest_run = runs[0] if runs.totalCount > 0 else None

            print(
                f"Latest Run ID: {latest_run.id if latest_run else None}",
                flush=True,
            )

        except Exception as e:
            print("❌ Could not resolve latest run:", repr(e), flush=True)

            return {
                "status": "error",
                "message": f"Could not resolve latest run: {e}",
            }

        # 7. Ignore stale workflow runs
        if latest_run is None or latest_run.id != run_id:

            print("❌ STALE WORKFLOW RUN - Ignoring", flush=True)

            return {
                "status": "ignored",
                "reason": "stale_run",
                "webhook_run_id": run_id,
                "latest_run_id": latest_run.id if latest_run else None,
            }

        print("✅ Latest workflow run verified", flush=True)

        # 8. Get current branch HEAD
        print("🔍 Resolving live branch HEAD...", flush=True)

        try:
            live_branch_ref = repo.get_branch(branch)
            live_sha = live_branch_ref.commit.sha

            print(f"Live SHA: {live_sha}", flush=True)

        except Exception as e:
            print("❌ Could not resolve branch HEAD:", repr(e), flush=True)

            return {
                "status": "error",
                "message": f"Could not resolve live branch head: {e}",
            }

        # 9. Check AI_FIX branch commit limit
        if branch.startswith("AI_FIX"):

            print("🔍 AI_FIX branch detected", flush=True)

            try:
                # commits added on top of the default branch (not full history)
                total_commits = repo.compare(
                    repo.default_branch, live_sha
                ).ahead_by

                print(
                    f"Branch: {branch} | Total commits: {total_commits}",
                    flush=True,
                )

                if total_commits > 10:

                    print(
                        "❌ AI_FIX branch exceeded 10 commits",
                        flush=True,
                    )

                    return {
                        "status": "ignored",
                        "reason": (
                            "AI_FIX branch exceeded 10 commits. "
                            "Manual inspection needed"
                        ),
                        "branch": branch,
                        "total_commits": total_commits,
                        "limit": 10,
                    }

            except Exception as e:

                print("❌ Commit check failed:", repr(e), flush=True)

                return {
                    "status": "error",
                    "message": f"Could not check AI_FIX commits: {e}",
                }

        # 10. Prepare LangGraph state
        print("📝 Preparing LangGraph state...", flush=True)

        state: Data = {
            "owner": owner,
            "repo": repo_name,
            "installation_id": installation_id,
            "workflow_run_id": run_id,
            "workflow_name": workflow_run.get("name", ""),
            "workflow_file": "",
            "branch": branch,
            "commit_sha": live_sha,
            "working_sha": live_sha,
            "error_type": "",
            "fixability": "",
            "reason": "",
            "confidence": 0.0,
            "logs": "",
            "repository_tree": [],
            "file_contents": {},
            "root_cause": None,
            "suggested_changes": None,
            "modified_files": {},
            "branch_name": None,
            "commitMsg": "",
            "commit_sha_new": None,
            "success": False,
            "messages": [],
        }

        print("✅ LangGraph state prepared", flush=True)

        # 11. Execute LangGraph workflow
        try:

            print("\n🚀 STARTING LANGGRAPH WORKFLOW", flush=True)

            result = workflow.invoke(state)

            print("\n✅ LANGGRAPH WORKFLOW COMPLETED", flush=True)

            return {
                "status": "received",
                "result": result,
            }

        except Exception as e:

            print("\n❌ LANGGRAPH WORKFLOW FAILED", flush=True)
            print("Error:", repr(e), flush=True)
            traceback.print_exc()

            return {
                "status": "error",
                "message": str(e),
            }

    except Exception as e:

        print("\n❌ WEBHOOK HANDLER FAILED", flush=True)
        print("Error:", repr(e), flush=True)
        traceback.print_exc()

        return {
            "status": "error",
            "message": str(e),
        }