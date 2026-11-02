from langchain.agents import create_agent
from agent_tools import read_file, read_multiple_files, search_code, update_file,create_file
from state import Data
import os
from dotenv import load_dotenv
from langchain_openai import ChatOpenAI

load_dotenv()

# BharatCode model API (OpenAI-compatible)
llm = ChatOpenAI(
    model="qwen-3.8-27b",
    api_key=os.getenv("BHARATCODE_API_KEY"),
    base_url="https://bharatcode.ai/api/model/v1",
    max_tokens=16000,
    max_retries=3,
)
tools = [read_file, read_multiple_files, search_code, update_file,create_file]

SYSTEM_PROMPT = """
You are an expert CI/CD debugging engineer. Follow this process strictly:

0. Before proposing any fix, scan the ENTIRE log output for ALL distinct
   errors — not just the first one. A single workflow failure can surface
   multiple independent problems (e.g., a missing dependency AND a code bug
   in the same run). List every error you find before deciding on fixes.
1. Identify the exact error type from the logs (syntax error, import error,
   assertion failure, missing file, dependency conflict, etc.)
2. If the error references a specific file and line number, read that file
   FIRST using read_file before proposing any fix.
3. If the error is a test assertion failure, read both the test file and the
   code it is testing. Determine whether the fix belongs in the test or the
   source code — do not assume the test is always right.
4. Only use create_file for files that genuinely do not exist yet in the
   repository tree. Never use create_file on a file that already exists.
5. Use update_file with precise 'target' text taken verbatim from the file
   you just read via read_file — do not guess file contents.
6. If you cannot determine a safe, targeted fix after reading the relevant
   files, do NOT guess or create empty/placeholder files. Explain clearly
   why manual intervention is needed instead.
7. Prefer minimal, surgical changes over rewriting entire files.
8. When calling a tool, always produce a valid JSON tool call. Do not invent
   custom syntax.
9. Return a clear summary of what was changed and why, or why no automated
   fix was possible.
10. This is very important rule:- always try to give correct file and if any
    updates in file required then use tools and update them your task is to
    give correct ci/cd pipeline
"""

agent = create_agent(
    model=llm,
    tools=tools,
    system_prompt=SYSTEM_PROMPT,
    state_schema=Data,
)


def solveCICD(state: Data):
    logs_snippet = state["logs"][-4000:] if state["logs"] else ""
    tree_snippet = state["repository_tree"] if state["repository_tree"] else []

    user_message = {
        "role": "user",
        "content": f"""
A GitHub Actions workflow failed.

Reason:
{state["reason"]}

Error logs (most recent, truncated):
{logs_snippet}

Repository tree:
{tree_snippet}

Analyze the failure.
If you need additional information, use the available tools.
If a fix is required, suggest or perform it.
"""
    }

    try:
        result = agent.invoke({
            **state,
            "messages": [user_message],
        },
            config={"recursion_limit": 25}
        )
        print(result)

        return {
            "file_contents": result.get("file_contents", state.get("file_contents", {})),
            "modified_files": result.get("modified_files", state.get("modified_files", {})),
            "root_cause": result.get("root_cause", state.get("root_cause", "")),
            "suggested_changes": result.get("suggested_changes", state.get("suggested_changes", "")),
        }

    except Exception as e:
        print(f"⚠️ Agent failed after retries: {e}")
        return {
            "success": False,
            "root_cause": f"Agent failed during automated fix attempt: {e}",
        }