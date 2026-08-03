# agent-framework-remote-openai on Hyperlight

Run a [Microsoft Agent Framework](https://github.com/microsoft/agent-framework)
agent that calls a **remote OpenAI model** (or any OpenAI-compatible endpoint)
over the network from inside a
[Hyperlight](https://github.com/hyperlight-dev/hyperlight) micro-VM.

This example uses the **python-agent-driver / `pyhl` stack**: a warmed CPython
interpreter that is snapshotted once and then restored per run (~2–3 s/run, no
kernel boot). The shipped driver kernel already includes **host-proxied
networking + hostfs**, so there is no custom kernel to build. The rootfs is kept
small (~64 MB) by taking only the shipped `hl_pydriver` interpreter shim plus
`agent-framework-core` — not the ~1 GB preloaded data-science stack the
general-purpose pyhl image carries.

`agent.py` is a real `agent_framework` `Agent` backed by a custom `BaseChatClient`
that POSTs to the OpenAI Chat Completions API. **Phase 3:** the agent is a SWE
coding agent with `read_file` / `list_dir` / `write_file` / `str_replace` / `bash`
tools. File tools are confined to `/host`. The guest has no real shell — `bash`
is proxied to the host by `host_bash_proxy.py` over `workspace/.bash_rpc/` (cwd
under the mount). The API key is provided at run time through
`pyhl run --env OPENAI_API_KEY`. Edits and command side-effects appear under
`./workspace/` via `--mount`.

For the GitHub Models / Copilot variant, see
[`../agent-framework-remote`](../agent-framework-remote).

## Prerequisites

- Rust/Cargo, so the Justfile can run the in-repo **`pyhl`** with `cargo run`:

  ```sh
  cargo build --manifest-path ../../host/Cargo.toml --bin pyhl
  ```

  To use an installed `pyhl` instead, point the Justfile at it:

  ```sh
  export PYHL=pyhl
  ```

  (The published GHCR driver image is version-matched to released `pyhl` builds.)
- An [OpenAI API key](https://platform.openai.com/api-keys) in `$OPENAI_API_KEY`:

  ```sh
  export OPENAI_API_KEY=sk-...
  ```

## Workspace layout (Phase 1)

Mount `./workspace` at guest `/host`. For the first SWE task (`CLI_Tools_Easy` /
`asottile__pyupgrade-939`) expect:

```text
workspace/
  issue.md
  testbed/          # contents of the SWE image /testbed
```

`just clean` removes build/snapshot artifacts only — it does **not** delete
`./workspace`.

## Run

```sh
just build      # fetch the shipped driver kernel from GHCR
just rootfs     # build a ~64 MB initrd: shipped hl_pydriver + agent-framework-core
just setup      # one-time: warm up + persist a Python snapshot (~24 s), mounts ./workspace
just smoke      # Phase 2: verify guest can read/write /host (no API key)
just deps       # host venv in workspace/testbed/.venv (pytest + editable install)
just test-host  # optional: run shlex_join tests on the host via that venv
just run        # Phase 3/4: SWE agent + host CPU/RSS sampling (needs OPENAI_API_KEY + deps)
just eval       # Phase 5: host score (diff + reproduce + focused pytest)
# just eval-full  # same + full pytest suite
```

`just run` wraps `pyhl` with `monitor_resources.py`, which samples the **host**
process tree for Hyperlight (`pyhl` / `cargo run` + children) and the bash
proxy every `MONITOR_INTERVAL` seconds (default `0.5`). Guest sandbox memory
shows up in that Hyperlight RSS. Output: `results/resources_<timestamp>.json`
(per-sample series + min/avg/max summary for CPU% and RSS MiB).

Phase 5 writes artifacts under `results/eval_<timestamp>/` (`results.json`,
`git_diff.patch`, `pytest_focused.txt`, `reproduce.txt`). Success means the
space-separator guard is present, the `"garbage".join(...)` example is not
rewritten to `shlex.join`, and focused tests pass — scored on the **host**,
not inside the unikernel.

The bash tool runs on the **host**. System `python3` alone is not enough if
`pytest` is not installed — `just deps` creates `workspace/testbed/.venv` and
the bash proxy puts `.venv/bin` on `PATH`. Prefer agent commands like
`python3 -m pytest -q …` (not bare `python`).

Example Phase 3 output (shape):

```
User:  Fix the issue described in /host/issue.md ...
Workspace: /host
[tool] read_file /host/issue.md ...
[tool] list_dir /host/testbed ...
[tool] str_replace /host/testbed/pyupgrade/_plugins/shlex_join.py
Agent: Root cause: ... Files changed: ...
```

Edits land under `workspace/testbed/` on the host. Raise tool budget if needed:
`OPENAI_MAX_TOOL_ROUNDS=50 just run`.

To use a different model, set `OPENAI_MODEL` (default `gpt-5-mini`):

```sh
OPENAI_MODEL=gpt-4o just run
```

The example uses `max_completion_tokens` for compatibility with newer models.
For `gpt-5*`, it defaults to `OPENAI_MAX_COMPLETION_TOKENS=1024` and
`OPENAI_REASONING_EFFORT=minimal`; set either variable before `just run` to
override those defaults.

### OpenAI-compatible providers

Point at another base URL (Azure OpenAI, local proxies, etc.):

```sh
export OPENAI_BASE_URL=https://your-endpoint.example/v1
export OPENAI_API_KEY=...
export OPENAI_NET_ALLOW=your-endpoint.example   # egress allowlist host
just run
```

Or set a full chat-completions URL with `OPENAI_ENDPOINT`.

## How it works

- **Shipped driver stack, minimal rootfs.** The `python-agent-driver` kernel
  published to GHCR enables networking (`CONFIG_LIBPOSIX_SOCKET` +
  `CONFIG_LIBHOSTSOCK`) and hostfs. `just build` pulls the kernel; `just rootfs`
  extracts just the shipped `hl_pydriver` (so it stays version-matched to that
  kernel) and lays it onto `python-base` with `agent-framework-core` added
  (pydantic, an agent-framework dependency, is already in `python-base`). This
  keeps the initrd ~64 MB instead of the ~1 GB general-purpose pyhl image.
- **`--net` + egress.** The guest has no network unless `--net` is passed. The
  shipped rootfs already ships CA certificates + `/etc/resolv.conf` for outbound
  TLS. The Justfile uses `--net-allow api.openai.com` (override with
  `OPENAI_NET_ALLOW`) so the guest can only reach the configured endpoint host.
- **`--mount ./workspace`.** `just setup` / `smoke` / `run` mount `./workspace` at
  `/host` so SWE tools edit the host testbed. Re-run `just setup` if you
  previously snapshotted without a mount — the guest path is baked into the
  snapshot.
- **Key via `--env`.** `just run` passes `$OPENAI_API_KEY` into the guest Python
  environment with `pyhl run --env OPENAI_API_KEY`. Optional knobs
  (`OPENAI_MODEL`, `OPENAI_BASE_URL`, `OPENAI_ENDPOINT`,
  `OPENAI_MAX_COMPLETION_TOKENS`, `OPENAI_REASONING_EFFORT`,
  `OPENAI_MAX_TOOL_ROUNDS`) are forwarded the same way when set.
- **Synchronous client + local tool loop.** Blocking `urllib` + in-guest tool
  dispatch (read/list/write/str_replace under `/host`) without `asyncio.run()`.
- **Host bash proxy.** `just run` starts `host_bash_proxy.py`, which executes
  `bash -lc` for guest `bash` tool requests under `workspace/.bash_rpc/`.
  Working directories must stay inside `./workspace`. The guest publishes
  `<id>.req` then `<id>.ready` (hostfs has no atomic rename); the proxy only
  reads after `.ready` exists so it never consumes a half-written request.

## Note on the plain-`hyperlight-unikraft` alternative

The GHCR kernels for the *non-driver* examples (e.g. `python-agent-kernel`) are
built **without** networking, so a plain `hyperlight-unikraft ... -- /script.py`
launch can't open sockets there. Networking requires either this driver stack or a
kernel built with the socket libraries (as in the `networking-py` example).
