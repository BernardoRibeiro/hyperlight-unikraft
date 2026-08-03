#!/usr/bin/env python3
"""SWE-style agent using a REMOTE OpenAI model inside Hyperlight.

Architecture A: the agent runs in a Hyperlight micro-VM; the SWE-bench testbed
and issue live on the host and are mounted at /host (pyhl --mount workspace).

Tools: read_file, list_dir, write_file, str_replace, bash.
Paths stay under /host. The `bash` tool is proxied to the host via
workspace/.bash_rpc/ (see host_bash_proxy.py) — the guest has no real shell.

Uses a synchronous urllib OpenAI client and steps coroutines without
asyncio.run() (guest event loops need socket.socketpair()).
"""

import json
import logging
import os
import time
import urllib.error
import urllib.request
import warnings
from collections.abc import Awaitable, Coroutine, Mapping, Sequence
from typing import Annotated, Any

# Keep the demo output clean.
warnings.filterwarnings("ignore")
logging.getLogger("agent_framework").setLevel(logging.ERROR)

from agent_framework import (  # noqa: E402
    Agent,
    BaseChatClient,
    ChatResponse,
    Message,
    tool,
)
from pydantic import Field  # noqa: E402

BASE_URL = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
ENDPOINT = os.environ.get("OPENAI_ENDPOINT", f"{BASE_URL}/chat/completions")
MODEL = os.environ.get("OPENAI_MODEL", "gpt-5-mini")
MAX_COMPLETION_TOKENS = int(
    os.environ.get(
        "OPENAI_MAX_COMPLETION_TOKENS",
        "2048" if MODEL.startswith(("o1", "o3", "o4", "gpt-5")) else "1024",
    )
)
REASONING_EFFORT = os.environ.get(
    "OPENAI_REASONING_EFFORT",
    "minimal" if MODEL.startswith("gpt-5") else "",
).strip()
MAX_TOOL_ROUNDS = int(os.environ.get("OPENAI_MAX_TOOL_ROUNDS", "40"))
MAX_READ_CHARS = int(os.environ.get("OPENAI_MAX_READ_CHARS", "24000"))
BASH_POLL_SEC = float(os.environ.get("OPENAI_BASH_POLL_SEC", "0.2"))
BASH_WAIT_SLACK_SEC = float(os.environ.get("OPENAI_BASH_WAIT_SLACK_SEC", "15"))

WORKSPACE = "/host" if os.path.isdir("/host") else "/tmp/agent-work"
ISSUE_PATH = os.path.join(WORKSPACE, "issue.md")
TESTBED_PATH = os.path.join(WORKSPACE, "testbed")
BASH_RPC_PENDING = os.path.join(WORKSPACE, ".bash_rpc", "pending")
BASH_RPC_DONE = os.path.join(WORKSPACE, ".bash_rpc", "done")


def _resolve_under_workspace(path: str) -> str:
    """Resolve path under WORKSPACE; reject escapes outside the mount."""
    raw = (path or "").strip()
    if not raw:
        raise ValueError("path must be non-empty")
    if raw.startswith("/host"):
        candidate = raw
    elif raw.startswith("/"):
        # Allow absolute paths only under /host.
        candidate = raw
    else:
        candidate = os.path.join(WORKSPACE, raw)
    real_workspace = os.path.realpath(WORKSPACE)
    real_path = os.path.realpath(candidate)
    if real_path != real_workspace and not real_path.startswith(real_workspace + os.sep):
        raise ValueError(f"path escapes workspace: {path!r} -> {real_path}")
    return real_path


def _numbered(text: str, start_line: int = 1) -> str:
    lines = text.splitlines()
    return "\n".join(f"{start_line + i:>6}→{line}" for i, line in enumerate(lines))


@tool(
    name="read_file",
    description=(
        "Read a UTF-8 text file under /host. Optionally return a line window. "
        "Paths may be absolute (/host/...) or relative to /host."
    ),
    approval_mode="never_require",
)
def read_file(
    path: Annotated[str, Field(description="File path under /host, e.g. issue.md or testbed/README.md")],
    offset: Annotated[int, Field(description="1-based start line (default 1)")] = 1,
    limit: Annotated[int, Field(description="Max lines to return (default 200)")] = 200,
) -> str:
    try:
        resolved = _resolve_under_workspace(path)
    except ValueError as exc:
        return f"Error: {exc}"
    if not os.path.isfile(resolved):
        return f"Error: not a file: {resolved}"
    try:
        with open(resolved, encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
    except OSError as exc:
        return f"Error reading {resolved}: {exc}"
    start = max(1, int(offset))
    lim = max(1, int(limit))
    chunk = lines[start - 1 : start - 1 + lim]
    body = "".join(chunk)
    if len(body) > MAX_READ_CHARS:
        body = body[:MAX_READ_CHARS] + "\n...[truncated]..."
    numbered = _numbered(body.rstrip("\n"), start_line=start)
    total = len(lines)
    end = min(total, start - 1 + len(chunk))
    print(f"[tool] read_file {resolved} lines {start}-{end}/{total}")
    return f"{resolved} (lines {start}-{end} of {total})\n{numbered}"


@tool(
    name="list_dir",
    description="List entries in a directory under /host (names only, sorted).",
    approval_mode="never_require",
)
def list_dir(
    path: Annotated[str, Field(description="Directory path under /host, e.g. testbed or testbed/pyupgrade")],
) -> str:
    try:
        resolved = _resolve_under_workspace(path)
    except ValueError as exc:
        return f"Error: {exc}"
    if not os.path.isdir(resolved):
        return f"Error: not a directory: {resolved}"
    try:
        names = sorted(os.listdir(resolved))
    except OSError as exc:
        return f"Error listing {resolved}: {exc}"
    rows = []
    for name in names[:200]:
        full = os.path.join(resolved, name)
        kind = "dir" if os.path.isdir(full) else "file"
        rows.append(f"{kind}\t{name}")
    extra = f"\n... ({len(names) - 200} more)" if len(names) > 200 else ""
    print(f"[tool] list_dir {resolved} ({len(names)} entries)")
    return f"{resolved}\n" + "\n".join(rows) + extra


@tool(
    name="write_file",
    description="Create or overwrite an entire UTF-8 text file under /host.",
    approval_mode="never_require",
)
def write_file(
    path: Annotated[str, Field(description="File path under /host")],
    content: Annotated[str, Field(description="Full new file contents")],
) -> str:
    try:
        resolved = _resolve_under_workspace(path)
    except ValueError as exc:
        return f"Error: {exc}"
    parent = os.path.dirname(resolved)
    try:
        os.makedirs(parent, exist_ok=True)
        with open(resolved, "w", encoding="utf-8") as f:
            f.write(content)
    except OSError as exc:
        return f"Error writing {resolved}: {exc}"
    print(f"[tool] write_file {resolved} ({len(content)} bytes)")
    return f"Wrote {resolved} ({len(content)} bytes)"


@tool(
    name="str_replace",
    description=(
        "Replace exactly one occurrence of old_string with new_string in a file "
        "under /host. old_string must match uniquely."
    ),
    approval_mode="never_require",
)
def str_replace(
    path: Annotated[str, Field(description="File path under /host")],
    old_string: Annotated[str, Field(description="Exact text to find (must appear once)")],
    new_string: Annotated[str, Field(description="Replacement text")],
) -> str:
    try:
        resolved = _resolve_under_workspace(path)
    except ValueError as exc:
        return f"Error: {exc}"
    if not os.path.isfile(resolved):
        return f"Error: not a file: {resolved}"
    try:
        with open(resolved, encoding="utf-8") as f:
            text = f.read()
    except OSError as exc:
        return f"Error reading {resolved}: {exc}"
    count = text.count(old_string)
    if count == 0:
        return f"Error: old_string not found in {resolved}"
    if count > 1:
        return f"Error: old_string found {count} times in {resolved}; make it unique"
    updated = text.replace(old_string, new_string, 1)
    try:
        with open(resolved, "w", encoding="utf-8") as f:
            f.write(updated)
    except OSError as exc:
        return f"Error writing {resolved}: {exc}"
    print(f"[tool] str_replace {resolved}")
    return f"Updated {resolved}"


@tool(
    name="bash",
    description=(
        "Run a bash command on the HOST (proxied via /host/.bash_rpc). "
        "cwd must be under /host (default /host/testbed). Prefer "
        "`python3 -m pytest -q …` (venv is on PATH). Also useful for "
        "git diff / ls. Requires host_bash_proxy.py + just deps."
    ),
    approval_mode="never_require",
)
def bash(
    command: Annotated[str, Field(description="Shell command to run with bash -lc")],
    cwd: Annotated[
        str,
        Field(description="Working directory under /host (default /host/testbed)"),
    ] = "/host/testbed",
    timeout_sec: Annotated[
        int,
        Field(description="Timeout in seconds (default 120, max 600)"),
    ] = 120,
) -> str:
    command = (command or "").strip()
    if not command:
        return "Error: command must be non-empty"
    try:
        timeout_sec = max(1, min(int(timeout_sec), 600))
    except (TypeError, ValueError):
        timeout_sec = 120

    os.makedirs(BASH_RPC_PENDING, exist_ok=True)
    os.makedirs(BASH_RPC_DONE, exist_ok=True)
    req_id = f"{int(time.time() * 1000)}-{os.getpid()}-{len(command) % 997}"
    # Two-phase publish: write body to .req, then create .ready.
    # Hostfs has no atomic rename; the proxy must not read .req until .ready exists
    # (otherwise it can observe an empty/partial file and delete it — race).
    req_path = os.path.join(BASH_RPC_PENDING, f"{req_id}.req")
    ready_path = os.path.join(BASH_RPC_PENDING, f"{req_id}.ready")
    done_path = os.path.join(BASH_RPC_DONE, f"{req_id}.json")
    payload = {
        "id": req_id,
        "command": command,
        "cwd": cwd or "/host/testbed",
        "timeout_sec": timeout_sec,
    }
    try:
        with open(req_path, "w", encoding="utf-8") as f:
            json.dump(payload, f)
            f.flush()
        with open(ready_path, "w", encoding="utf-8") as f:
            f.write("1")
            f.flush()
    except OSError as exc:
        return f"Error: failed to publish bash request: {exc}"
    print(f"[tool] bash id={req_id} cwd={cwd!r} cmd={command!r}")

    deadline = time.time() + timeout_sec + BASH_WAIT_SLACK_SEC
    while time.time() < deadline:
        if os.path.isfile(done_path):
            try:
                with open(done_path, encoding="utf-8") as f:
                    text = f.read()
                if not text.strip():
                    time.sleep(BASH_POLL_SEC)
                    continue
                result = json.loads(text)
            except json.JSONDecodeError:
                # Host may still be writing the done file.
                time.sleep(BASH_POLL_SEC)
                continue
            except OSError as exc:
                return f"Error: failed to read bash result: {exc}"
            try:
                os.remove(done_path)
            except OSError:
                pass
            code = result.get("exit_code", 1)
            out = result.get("stdout") or ""
            err = result.get("stderr") or ""
            print(f"[tool] bash id={req_id} exit={code}")
            parts = [f"exit_code={code}"]
            if out:
                parts.append(f"stdout:\n{out}")
            if err:
                parts.append(f"stderr:\n{err}")
            return "\n".join(parts)
        time.sleep(BASH_POLL_SEC)

    return (
        f"Error: timed out waiting for host bash proxy (id={req_id}). "
        "Is `python3 host_bash_proxy.py --workspace workspace` running?"
    )


TOOLS = [read_file, list_dir, write_file, str_replace, bash]


def _tool_openai_schema(ai_tool: Any) -> dict[str, Any]:
    """Build an OpenAI tools[] entry from an agent_framework tool."""
    params = getattr(ai_tool, "parameters", None)
    schema: dict[str, Any]
    if callable(params):
        schema = dict(params())
    elif isinstance(params, dict):
        schema = dict(params)
    else:
        input_model = getattr(ai_tool, "input_model", None)
        if hasattr(input_model, "model_json_schema"):
            schema = input_model.model_json_schema()
        else:
            schema = {"type": "object", "properties": {}}
    schema.pop("title", None)
    return {
        "type": "function",
        "function": {
            "name": ai_tool.name,
            "description": ai_tool.description or "",
            "parameters": schema,
        },
    }


def _tools_by_name() -> dict[str, Any]:
    return {t.name: t for t in TOOLS}


def _message_to_openai(message: Message) -> dict[str, Any] | None:
    """Convert an agent_framework Message to an OpenAI chat message dict."""
    role = str(getattr(message.role, "value", message.role))
    tool_calls: list[dict[str, Any]] = []
    text_parts: list[str] = []
    tool_call_id: str | None = None
    tool_result: str | None = None

    for content in getattr(message, "contents", []) or []:
        ctype = getattr(content, "type", None)
        if ctype == "text" or isinstance(content, str):
            text = content if isinstance(content, str) else getattr(content, "text", "") or ""
            if text:
                text_parts.append(text)
        elif ctype == "function_call":
            args = content.arguments
            if not isinstance(args, str):
                args = json.dumps(args or {})
            tool_calls.append(
                {
                    "id": content.call_id,
                    "type": "function",
                    "function": {"name": content.name, "arguments": args},
                }
            )
        elif ctype == "function_result":
            tool_call_id = content.call_id
            result = content.result
            tool_result = result if isinstance(result, str) else json.dumps(result)

    if role == "tool" or tool_call_id is not None:
        return {
            "role": "tool",
            "tool_call_id": tool_call_id or "",
            "content": tool_result if tool_result is not None else "".join(text_parts),
        }

    if tool_calls:
        msg: dict[str, Any] = {"role": "assistant", "tool_calls": tool_calls}
        if text_parts:
            msg["content"] = "".join(text_parts)
        return msg

    text = "".join(text_parts) or (message.text or "")
    if not text and role != "assistant":
        return None
    return {"role": role, "content": text}


def _openai_post(messages: list[dict[str, Any]], *, with_tools: bool) -> dict[str, Any]:
    token = os.environ.get("OPENAI_API_KEY", "").strip()
    if not token:
        raise RuntimeError(
            "OPENAI_API_KEY must be set on the host and passed with pyhl run --env OPENAI_API_KEY"
        )
    payload: dict[str, Any] = {
        "model": MODEL,
        "messages": messages,
        "max_completion_tokens": MAX_COMPLETION_TOKENS,
    }
    if REASONING_EFFORT:
        payload["reasoning_effort"] = REASONING_EFFORT
    if with_tools:
        payload["tools"] = [_tool_openai_schema(t) for t in TOOLS]
        payload["tool_choice"] = "auto"
    req = urllib.request.Request(
        ENDPOINT,
        data=json.dumps(payload).encode(),
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=180) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace").strip()
        try:
            message = json.loads(body)["error"]["message"]
        except Exception:
            message = body or exc.reason
        raise RuntimeError(f"OpenAI API returned HTTP {exc.code} for {MODEL}: {message}") from exc


def _invoke_tool(name: str, arguments: str) -> str:
    tool_map = _tools_by_name()
    fn = tool_map.get(name)
    if fn is None:
        return f'Error: unknown tool "{name}"'
    try:
        args = json.loads(arguments or "{}")
        if not isinstance(args, dict):
            return "Error: tool arguments must be a JSON object"
    except json.JSONDecodeError as exc:
        return f"Error: invalid tool arguments JSON: {exc}"
    result = fn(**args)
    return result if isinstance(result, str) else json.dumps(result)


class OpenAIChatClient(BaseChatClient):
    """Chat client that calls the OpenAI API synchronously via urllib."""

    def _inner_get_response(
        self,
        *,
        messages: Sequence[Message],
        stream: bool = False,
        options: Mapping[str, Any],
        **kwargs: Any,
    ) -> Awaitable[ChatResponse]:
        openai_messages: list[dict[str, Any]] = []
        for message in messages:
            converted = _message_to_openai(message)
            if converted is not None:
                openai_messages.append(converted)

        final_text = ""
        final_model = MODEL

        for round_i in range(MAX_TOOL_ROUNDS):
            data = _openai_post(openai_messages, with_tools=True)
            final_model = data.get("model", MODEL)
            choice = data["choices"][0]
            msg = choice.get("message") or {}
            tool_calls = msg.get("tool_calls") or []
            content = (msg.get("content") or "").strip()

            if tool_calls:
                print(f"[agent] tool round {round_i + 1}: {len(tool_calls)} call(s)")
                openai_messages.append(
                    {
                        "role": "assistant",
                        "content": msg.get("content"),
                        "tool_calls": tool_calls,
                    }
                )
                for call in tool_calls:
                    fn = call.get("function") or {}
                    name = fn.get("name") or ""
                    arguments = fn.get("arguments") or "{}"
                    call_id = call.get("id") or name
                    result = _invoke_tool(name, arguments)
                    openai_messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": call_id,
                            "content": result,
                        }
                    )
                continue

            final_text = content
            break
        else:
            raise RuntimeError(
                f"OpenAI tool loop exceeded {MAX_TOOL_ROUNDS} rounds for {MODEL}"
            )

        if not final_text:
            raise RuntimeError(
                f"OpenAI API returned an empty response for {MODEL}; "
                "increase OPENAI_MAX_COMPLETION_TOKENS or lower OPENAI_REASONING_EFFORT"
            )

        response = ChatResponse(
            messages=[Message(role="assistant", contents=[final_text])],
            model=final_model,
        )

        async def _get() -> ChatResponse:
            return response

        return _get()


def run_sync(coro: Coroutine[Any, Any, Any]) -> Any:
    """Run a coroutine that performs no real async I/O, without an event loop."""
    try:
        while True:
            pending = coro.send(None)
            if pending is not None:
                raise RuntimeError(f"coroutine requires an event loop (awaited {pending!r})")
    except StopIteration as exc:
        return exc.value


INSTRUCTIONS = """\
You are a software-engineering agent running inside a Hyperlight micro-VM.
The host has mounted a SWE-bench workspace at /host:
  /host/issue.md     — GitHub issue / bug report
  /host/testbed/     — repository checked out at the buggy base commit

You have tools: read_file, list_dir, write_file, str_replace, bash.
All file paths must stay under /host. The bash tool runs on the HOST with
cwd under /host (default /host/testbed). A project venv is on PATH, so prefer:
  python3 -m pytest -q …
Do NOT use bare `python` (often missing); use `python3`.

CRITICAL: Existing tests may already pass while the bug in issue.md is still
present. Passing pytest on the current suite is NOT success. You must:
  - Reproduce the incorrect behavior described in the issue (e.g. run pyupgrade
    on the example, or add a failing test that encodes the issue).
  - Change production code to fix that behavior.
  - Add a regression test that would fail without your fix.
  - Re-run tests and confirm they pass.

Workflow (you MUST make code edits; do not only advise):
1. Read /host/issue.md thoroughly.
2. Explore /host/testbed: open the matching plugin under pyupgrade/_plugins/
   (for this class of bug: shlex_join.py) and its tests.
3. Reproduce: show that the bad transformation still happens (bash demo or a
   new failing test). Do not conclude "already fixed" just because old tests pass.
4. Implement a minimal fix with str_replace (or write_file).
5. Add/adjust tests under tests/features/ for the issue cases.
6. Run python3 -m pytest -q on the relevant tests. Claim pass only from output.
7. Summarize: root cause, files changed, how you reproduced, test result.
"""

QUERY = """\
Fix the issue in /host/issue.md inside /host/testbed.

The bug: pyupgrade turns non-space separators like "garbage".join(shlex.quote(...))
into shlex.join(...), which is wrong. Existing shlex_join tests may still pass —
that does NOT mean the issue is fixed.

You must:
1. Open pyupgrade/_plugins/shlex_join.py and tests/features/shlex_join_test.py.
2. Reproduce the bug (e.g. python3 -c / pyupgrade on the issue example, or add
   a failing test for a non-space separator).
3. Fix the plugin so the rewrite only applies when the join separator is a
   single space " ".
4. Add regression tests for non-space separators (e.g. "garbage", ", ").
5. Run: python3 -m pytest -q tests/features/shlex_join_test.py
6. Summarize changes and test output.
"""


async def main() -> None:
    if not os.path.isfile(ISSUE_PATH):
        raise RuntimeError(f"missing {ISSUE_PATH}; complete Phase 1 workspace layout")
    if not os.path.isdir(TESTBED_PATH):
        raise RuntimeError(f"missing {TESTBED_PATH}; complete Phase 1 workspace layout")

    agent = Agent(
        client=OpenAIChatClient(),
        name="HyperlightSWEAgent",
        instructions=INSTRUCTIONS,
        tools=TOOLS,
    )
    print(f"User:  {QUERY.strip().splitlines()[0]} ...")
    print(f"Workspace: {WORKSPACE}")
    print(f"Model: {MODEL}  max_tool_rounds={MAX_TOOL_ROUNDS}")
    result = await agent.run(QUERY)
    print(f"Agent: {result.messages[0].text}")


if __name__ == "__main__":
    run_sync(main())
