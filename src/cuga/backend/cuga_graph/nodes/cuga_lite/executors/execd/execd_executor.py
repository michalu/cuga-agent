"""Remote code execution against OpenSandbox's execd over HTTP.

execd is the in-sandbox execution daemon from OpenSandbox. Unlike the
OpenSandbox *server*, it runs standalone and needs no container runtime, so it
can live inside an OpenShell sandbox. Each thread gets its own execd code
context — a separate Jupyter kernel, i.e. a separate OS process with its own
Python state that persists across calls.

Only the transport differs from the E2B path: code assembly, tool
serialisation and output parsing are identical and are inherited rather than
duplicated.

Logging — what you see in ``docker logs`` / ``kubectl logs``
------------------------------------------------------------
Every significant event is logged at a consistent level with a fixed prefix so
you can grep the container output without regex gymnastics:

  grep "execd"                 — all execd events
  grep "execd:code"            — code execution events only
  grep "execd:shell"           — shell / run_command events only
  grep "execd:fs"              — filesystem tool events
  grep "execd:ctx"             — kernel lifecycle (create, evict, bootstrap)
  grep "execd:route"           — routing decision (which backend was chosen)

Log levels:
  INFO  — per-request events with timing (start, result, duration_ms)
  DEBUG — full payloads (code sent, raw output) — verbose, off by default
  WARNING — degraded paths (no key, bootstrap failed, throwaway context)
"""

import asyncio
import itertools
import json
import re
import time
from typing import Any, Callable, List, Optional

import httpx
from langchain_core.tools import StructuredTool
from loguru import logger

from cuga.backend.cuga_graph.state.agent_state import AgentState, VariablesManager
from cuga.config import settings
from ..common.run_output import format_run_command_output
from ..filesystem.paths import normalize_shell_command_paths
from ..e2b import E2BExecutor


class ExecdExecutor(E2BExecutor):
    """Executes agent code in a remote execd sandbox."""

    # thread_id -> execd context id. A context is a live Jupyter kernel, so
    # reusing it across turns is what keeps variables alive between steps.
    _contexts: dict[str, str] = {}
    _locks: dict[str, asyncio.Lock] = {}
    # Monotonic counter rather than wall-clock: only the ordering matters, and
    # it cannot go backwards.
    _last_used: dict[str, int] = {}
    _clock = itertools.count()

    @staticmethod
    def _base_url() -> str:
        url = getattr(settings.advanced_features, "execd_url", None) or "http://host.openshell.internal:44772"
        return url.rstrip("/")

    @staticmethod
    def _auth_headers() -> dict[str, str]:
        """Return the ``X-API-Key`` header for every execd request.

        **Single-tenant / local development** (``execd_api_key`` is empty):
        no header is sent.  execd is reachable only through ``openshell
        forward`` or a Kubernetes-internal Service, so network isolation
        replaces application auth.  This is the default and is correct for
        a developer workstation or a single-tenant deployment where the
        sandbox is not publicly routable.

        **Multi-tenant / BYOA / catalog deployment** (``execd_api_key`` set):
        every request carries ``X-API-Key: <key>``.  The key is the
        ``SANDBOX_API_KEY`` generated at provision time and stored in
        Sovereign Core's secrets store.  It is injected here via
        ``DYNACONF_ADVANCED_FEATURES__EXECD_API_KEY`` or ``settings.toml``.

        The same key protects the control plane (``sandbox-api``) via the
        identical header.  One secret, both planes.

        BYOA agents (LangGraph, LangFlow, custom apps) that call execd
        directly must pass the same ``X-API-Key`` header in every
        ``POST /code`` and ``POST /command`` request — there is no automatic
        injection outside of CUGA.  The key is available from Sovereign
        Core's service binding as ``SANDBOX_API_KEY``.
        """
        key = getattr(settings.advanced_features, "execd_api_key", "") or ""
        if not key:
            url = getattr(settings.advanced_features, "execd_url", "") or ""
            # Warn when the execd URL looks like a public/remote endpoint but
            # no API key is configured.  A loopback or .internal address is
            # expected to be network-isolated; anything else is likely a
            # catalog URL that should be protected.
            if url and not any(
                url.startswith(prefix)
                for prefix in ("http://localhost", "http://127.", "http://host.openshell", "http://host.docker")
            ):
                logger.warning(
                    "[ExecdExecutor] execd_url looks like a remote endpoint ({}) "
                    "but execd_api_key is not set. "
                    "In a multi-tenant or BYOA deployment every caller must authenticate "
                    "with X-API-Key. Set advanced_features.execd_api_key (or "
                    "DYNACONF_ADVANCED_FEATURES__EXECD_API_KEY) to SANDBOX_API_KEY "
                    "from the Sovereign Core service binding.",
                    url,
                )
        return {"X-API-Key": key} if key else {}

    @classmethod
    def _get_key_lock(cls, key: str) -> asyncio.Lock:
        # Safe without an outer lock: asyncio does not preempt between the
        # membership test and the assignment.
        if key not in cls._locks:
            cls._locks[key] = asyncio.Lock()
        return cls._locks[key]

    @classmethod
    async def _context_for_thread(cls, thread_id: Optional[str]) -> str:
        if not thread_id:
            # Without an identity there is nothing to isolate by, and a shared
            # fallback context would put unrelated sessions — potentially
            # different users — in one kernel with one another's variables and
            # files. A throwaway context loses state, which is the unavoidable
            # cost of having no identity, but nothing leaks sideways.
            logger.warning(
                "[execd:ctx] No thread_id supplied; using a throwaway context. "
                "State will not persist across calls."
            )
            return await cls._create_context(bootstrap_key=None)

        key = thread_id
        async with cls._get_key_lock(key):
            existing = cls._contexts.get(key)
            if existing is not None:
                cls._touch(key)
                logger.debug(f"[execd:ctx] Reusing existing kernel context_id={existing} thread={key}")
                return existing

            await cls._evict_if_needed()

            context_id = await cls._create_context(bootstrap_key=key)
            cls._contexts[key] = context_id
            cls._touch(key)
            logger.info(f"[execd:ctx] Created kernel context_id={context_id} thread={key}")
            return context_id

    @classmethod
    async def _create_context(cls, bootstrap_key: Optional[str]) -> str:
        t0 = time.monotonic()
        timeout = float(settings.advanced_features.sandbox_execution_timeout)
        async with httpx.AsyncClient(timeout=timeout, headers=cls._auth_headers()) as client:
            response = await client.post(f"{cls._base_url()}/code/context", json={"language": "python"})
            response.raise_for_status()
            context_id = response.json()["id"]

        elapsed = int((time.monotonic() - t0) * 1000)
        logger.info(
            f"[execd:ctx] POST /code/context → context_id={context_id} "
            f"key={bootstrap_key or '(throwaway)'} duration_ms={elapsed}"
        )
        await cls._bootstrap_context(context_id, bootstrap_key or context_id)
        return context_id

    @classmethod
    def _touch(cls, key: str) -> None:
        cls._last_used[key] = next(cls._clock)

    @classmethod
    async def _evict_if_needed(cls) -> None:
        """Drop least recently used contexts once the cap is reached.

        Every context is a live Jupyter kernel holding its own interpreter, so
        they accumulate memory for as long as the sandbox runs. Nothing expires
        them on the execd side, so concurrent sessions would otherwise grow
        without bound.
        """
        limit = int(getattr(settings.advanced_features, "execd_max_contexts", 0) or 0)
        if limit <= 0 or len(cls._contexts) < limit:
            return

        for key, _ in sorted(cls._last_used.items(), key=lambda kv: kv[1]):
            if key not in cls._contexts:
                cls._last_used.pop(key, None)
                continue
            logger.info(
                f"[execd:ctx] Evicting LRU kernel thread={key} "
                f"live_contexts={len(cls._contexts)} limit={limit}"
            )
            await cls.release_thread(key)
            if len(cls._contexts) < limit:
                return

    @staticmethod
    def _workspace_name(key: str) -> str:
        """Filesystem-safe directory name for a thread id.

        Thread ids reach us from callers, so anything outside this set — a
        slash or a `..` in particular — must not survive into a path.
        """
        safe = "".join(c if (c.isalnum() or c in "-_") else "-" for c in key)
        return safe[:64] or "default"

    @classmethod
    async def _bootstrap_context(cls, context_id: str, key: str) -> None:
        """Give the context its own workspace directory and virtualenv.

        All contexts share one interpreter, so isolation here is by `sys.path`
        rather than by a separate Python: the per-thread venv is prepended, so
        anything installed into it shadows the image-wide site-packages while
        the shared packages stay importable.

        Best-effort by design. A missing `uv`, a read-only mount or a failed
        venv creation degrades to the shared environment rather than failing
        the turn — the agent can still run code, just without private packages.
        """
        name = cls._workspace_name(key)
        workspace = f"/workspace/{name}"
        index_url = getattr(settings.advanced_features, "execd_package_index", "") or ""
        logger.info(
            f"[execd:ctx] Bootstrap start context_id={context_id} "
            f"workspace={workspace} "
            f"package_index={index_url or '(none — installs will fail closed)'}"
        )
        bootstrap = f"""
import os, site, subprocess, sys

_ws = os.path.join("/workspace", {name!r})
os.makedirs(_ws, exist_ok=True)
os.chdir(_ws)

_venv = os.path.join(_ws, ".venv")
if not os.path.isdir(_venv):
    try:
        subprocess.run(["uv", "venv", _venv], capture_output=True, timeout=120)
    except Exception as _exc:
        print("execd bootstrap: uv venv failed:", _exc)

_sp = os.path.join(
    _venv, "lib", "python{{}}.{{}}".format(*sys.version_info[:2]), "site-packages"
)
if os.path.isdir(_sp):
    site.addsitedir(_sp)
    # addsitedir appends; put the private packages ahead of the shared ones.
    if _sp in sys.path:
        sys.path.remove(_sp)
    sys.path.insert(0, _sp)

# Package installs land in this thread's venv, never in the image-wide
# site-packages (which is read-only under the sandbox policy anyway).
os.environ["VIRTUAL_ENV"] = _venv
os.environ["PATH"] = os.path.join(_venv, "bin") + os.pathsep + os.environ.get("PATH", "")

_index = {index_url!r}
if _index:
    os.environ["UV_INDEX_URL"] = _index
    os.environ["PIP_INDEX_URL"] = _index
    # uv resolves against this index only; nothing reaches the default PyPI
    # unless the operator configured it explicitly.
    os.environ["UV_INDEX_STRATEGY"] = "first-index"
"""
        t0 = time.monotonic()
        try:
            await cls._execute(context_id, bootstrap)
            elapsed = int((time.monotonic() - t0) * 1000)
            logger.info(
                f"[execd:ctx] Bootstrap complete context_id={context_id} "
                f"workspace={workspace} duration_ms={elapsed}"
            )
        except Exception as exc:
            elapsed = int((time.monotonic() - t0) * 1000)
            logger.warning(
                f"[execd:ctx] Bootstrap FAILED context_id={context_id} "
                f"workspace={workspace} duration_ms={elapsed} error={exc}"
            )

    @classmethod
    async def _run_remote(cls, code: str, thread_id: Optional[str]) -> str:
        """Resolve the thread's context and execute code in it."""
        context_id = await cls._context_for_thread(thread_id)
        return await cls._execute(context_id, code)

    @classmethod
    async def _execute(cls, context_id: str, code: str) -> str:
        """Send code to one execd context and return the combined output.

        The response is a newline-delimited JSON stream of typed events:
        ``init`` and ``ping`` are handshake noise, ``stdout``/``stderr`` carry
        output, ``execution_complete`` ends the run, and ``error`` carries a
        Jupyter traceback. Only the output events contribute to the result.
        """
        # The in-sandbox asyncio.wait_for already bounds the agent's own code;
        # allow headroom on the wire so a clean timeout beats a torn stream.
        timeout = float(settings.advanced_features.sandbox_execution_timeout) + 30.0
        # Truncate for readability in logs — full code at DEBUG level only
        code_preview = code[:120].replace("\n", "↵") + ("…" if len(code) > 120 else "")
        logger.info(
            f"[execd:code] → POST /code context_id={context_id} "
            f"code_len={len(code)} preview={code_preview!r}"
        )
        t0 = time.monotonic()

        async with httpx.AsyncClient(timeout=timeout, headers=cls._auth_headers()) as client:
            async with client.stream(
                "POST",
                f"{cls._base_url()}/code",
                json={"context": {"id": context_id, "language": "python"}, "code": code},
            ) as response:
                response.raise_for_status()
                chunks, _out, _err, failure = await cls._consume_stream(response)

        elapsed = int((time.monotonic() - t0) * 1000)

        if failure:
            name = failure.get("ename", "Error")
            value = failure.get("evalue", "")
            traceback = failure.get("traceback") or []
            tail = "\n".join(traceback[-3:]) if traceback else ""
            logger.warning(
                f"[execd:code] ✗ context_id={context_id} "
                f"error={name}: {value!r} duration_ms={elapsed}"
            )
            raise RuntimeError(f"{name}: {value}\n{tail}".strip())

        output_len = sum(len(c) for c in chunks)
        logger.info(
            f"[execd:code] ✓ context_id={context_id} "
            f"output_len={output_len} duration_ms={elapsed}"
        )
        logger.debug(
            f"[execd:code] full output context_id={context_id}: {''.join(chunks)[:500]}"
        )
        return "".join(chunks)

    @classmethod
    async def _consume_stream(cls, response: Any) -> tuple[list[str], list[str], list[str], Optional[dict]]:
        """Read one execd event stream into (combined, stdout, stderr, failure).

        ``/code`` and ``/command`` speak the same newline-delimited JSON: ``init``
        and ``ping`` are handshake noise, ``stdout``/``stderr`` carry output, and
        ``error`` ends the run. The two callers differ only in what they do with
        an ``error`` — a kernel traceback is a failure to raise, a non-zero exit
        status is output to report — so the split streams are returned alongside
        the interleaved one rather than merged here.
        """
        combined: list[str] = []
        out: list[str] = []
        err: list[str] = []
        failure: Optional[dict[str, Any]] = None

        async for line in response.aiter_lines():
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                logger.debug(f"[ExecdExecutor] Ignoring non-JSON stream line: {line[:200]}")
                continue

            event_type = event.get("type")
            if event_type in ("stdout", "stderr"):
                text = event.get("text", "")
                combined.append(text)
                (out if event_type == "stdout" else err).append(text)
            elif event_type == "error":
                failure = event.get("error") or {}

        return combined, out, err, failure

    @classmethod
    def _workspace_path(cls, thread_id: Optional[str]) -> str:
        """The thread's workspace directory, as seen inside the sandbox."""
        return f"/workspace/{cls._workspace_name(thread_id or '_default')}"

    @classmethod
    async def _run_shell_command(
        cls,
        command: str,
        thread_id: Optional[str],
        extra_env: Optional[dict[str, str]] = None,
    ) -> tuple[str, str, bool]:
        """Run one shell command in the thread's workspace via ``POST /command``.

        Resolving the code context first is what makes the shell and the agent's
        Python share a workspace: the context bootstrap is what creates
        ``/workspace/<thread>`` and its virtualenv. Without it a command could
        land in a directory that does not exist yet, and installs would miss the
        venv that the generated code imports from.

        A non-zero exit arrives as an ``error`` event whose ``evalue`` is the
        status. That is a result to hand back to the agent, not a transport
        failure, so it is reported rather than raised.
        """
        await cls._context_for_thread(thread_id)
        workspace = cls._workspace_path(thread_id)
        venv = f"{workspace}/.venv"
        timeout_s = float(settings.advanced_features.sandbox_execution_timeout)

        # Log the command but not the env (may contain CUGA_FS_SRC — a full
        # Python script — which would drown the log line).
        is_fs_op = extra_env and "CUGA_FS_SRC" in extra_env
        cmd_preview = command[:120].replace("\n", "↵") + ("…" if len(command) > 120 else "")
        if is_fs_op:
            op_hint = ""
            try:
                spec = json.loads(__import__("base64").b64decode(
                    extra_env.get("CUGA_FS_OP", "e30=")
                ).decode())
                op_hint = f" fs_op={spec.get('op','?')}"
            except Exception:
                pass
            logger.info(
                f"[execd:fs] → POST /command (filesystem){op_hint} "
                f"workspace={workspace} thread={thread_id}"
            )
        else:
            logger.info(
                f"[execd:shell] → POST /command cmd={cmd_preview!r} "
                f"cwd={workspace} thread={thread_id}"
            )

        t0 = time.monotonic()
        payload: dict[str, Any] = {
            "command": command,
            "cwd": workspace,
            "timeout": int(timeout_s * 1000),
            # A command is a fresh process, so nothing the context bootstrap set
            # in the kernel carries over — the venv has to be named again here
            # or `uv pip install` would write to the image-wide site-packages.
            "envs": {
                "VIRTUAL_ENV": venv,
                "PATH": f"{venv}/bin:/usr/local/bin:/usr/bin:/bin",
            },
        }
        index_url = getattr(settings.advanced_features, "execd_package_index", "") or ""
        if index_url:
            payload["envs"]["UV_INDEX_URL"] = index_url
            payload["envs"]["PIP_INDEX_URL"] = index_url
            payload["envs"]["UV_INDEX_STRATEGY"] = "first-index"
        if extra_env:
            # Callers pass operands here rather than in the command string —
            # see ExecdFilesystemBackend, which keeps file contents and paths
            # out of shell quoting entirely.
            payload["envs"].update(extra_env)

        async with httpx.AsyncClient(timeout=timeout_s + 30.0, headers=cls._auth_headers()) as client:
            async with client.stream("POST", f"{cls._base_url()}/command", json=payload) as response:
                response.raise_for_status()
                _combined, out, err, failure = await cls._consume_stream(response)

        elapsed = int((time.monotonic() - t0) * 1000)
        # Joined with newlines, unlike /code: a command emits one event per
        # output line with the terminator stripped, while a kernel sends its
        # text with the newlines already in it. Concatenating command events
        # would run every line of output together.
        stdout = "\n".join(out)
        stderr = "\n".join(err)
        if failure:
            detail = failure.get("evalue") or failure.get("ename") or "command failed"
            stderr = f"{stderr}\n[exit] {detail}".strip()

        status = "✗" if failure else "✓"
        if is_fs_op:
            logger.info(
                f"[execd:fs] {status}{op_hint} "
                f"stdout_len={len(stdout)} stderr_len={len(stderr)} duration_ms={elapsed}"
            )
        else:
            logger.info(
                f"[execd:shell] {status} cmd={cmd_preview!r} "
                f"stdout_len={len(stdout)} stderr_len={len(stderr)} duration_ms={elapsed}"
            )
            if stdout:
                logger.debug(f"[execd:shell] stdout thread={thread_id}: {stdout[:300]}")
            if stderr:
                logger.debug(f"[execd:shell] stderr thread={thread_id}: {stderr[:300]}")

        return stdout, stderr, bool(failure)

    # A bare ``/workspace`` — the shared helper only rewrites ``/workspace/...``,
    # leaving the root itself alone because under opensandbox the sandbox is
    # per thread and that root is already the right one.
    _BARE_WORKSPACE = re.compile(r"(?<![\w./])/workspace(?![\w/])")

    @classmethod
    def _confine_to_thread_workspace(cls, command: str, thread_id: Optional[str]) -> str:
        """Point a bare ``/workspace`` at this thread's directory.

        One execd container holds every thread, so ``ls /workspace`` would
        otherwise list every other thread's workspace.

        Best-effort, like the shared path normalization it follows: a command
        that builds a path at runtime can still name the shared root. It is not
        the isolation boundary — per-thread separation here is by directory, and
        anything needing a real boundary needs its own sandbox (see
        `sandbox/sandbox/README.md`). It removes the obvious accident.
        """
        return cls._BARE_WORKSPACE.sub(cls._workspace_path(thread_id), command)

    def create_run_command_tool(self, thread_id: Optional[str] = None) -> Callable:
        """Build the ``run_command`` coroutine bound to one thread."""

        async def run_command(cmd: str) -> str:
            try:
                normalized = self._confine_to_thread_workspace(normalize_shell_command_paths(cmd), thread_id)
                stdout, stderr, failed = await self._run_shell_command(normalized, thread_id)
                return format_run_command_output(stdout, stderr, failed=failed)
            except Exception as exc:
                return f"[run_command error] {exc}"

        return run_command

    def create_sandbox_tools(
        self,
        thread_id: Optional[str] = None,
        cuga_folder: Optional[str] = None,
        skills_enabled: Optional[bool] = None,
    ) -> list[StructuredTool]:
        """Return the ``run_command`` tool, matching OpenSandboxExecutor's contract.

        ``cuga_folder`` / ``skills_enabled`` are accepted so the two executors are
        interchangeable at the call site in ``build_runtime_tools``. execd has no
        per-sandbox skills upload step — skills reach the workspace through the
        filesystem tools — so they are unused here.
        """
        return [
            StructuredTool.from_function(
                coroutine=self.create_run_command_tool(thread_id),
                name="run_command",
                description=(
                    "Run a shell command inside the sandbox and return its output. "
                    "Commands run from the thread workspace with its virtual environment "
                    "activated, alongside the Python you execute. "
                    "Install with `uv pip install <pkg>` (no --target flag — the venv is already active). "
                    "Verify with `python -c \"import pkg; print('ok')\"` "
                    "or `uv pip show pkg` — not `python -m pip` or `pip show`. "
                    "Run with `python ./script.py` first; retry with `uv run --no-project ...` if that fails. "
                    "Node commands must start with plain `node ...`; npm commands must start with plain `npm ...`. "
                    "Never use `uv npm`, `uv run node`, or `uv run npm`."
                ),
            ),
        ]

    @classmethod
    async def release_thread(cls, thread_id: Optional[str] = None) -> None:
        """Delete a thread's context, freeing the kernel process behind it.

        Forgetting the id locally is not enough: the kernel keeps running and
        holding its interpreter until execd is told to drop it.
        """
        key = thread_id or "_default"
        context_id = cls._contexts.pop(key, None)
        cls._locks.pop(key, None)
        cls._last_used.pop(key, None)
        if not context_id:
            return
        logger.info(f"[execd:ctx] Releasing kernel context_id={context_id} thread={key}")
        try:
            timeout = float(settings.advanced_features.sandbox_execution_timeout)
            async with httpx.AsyncClient(timeout=timeout, headers=cls._auth_headers()) as client:
                response = await client.delete(f"{cls._base_url()}/code/contexts/{context_id}")
                response.raise_for_status()
            logger.info(f"[execd:ctx] Kernel released context_id={context_id} thread={key}")
        except Exception as exc:
            # The local mapping is already gone, so the next turn starts a fresh
            # kernel either way; a stale one is a leak, not a correctness bug.
            logger.warning(
                f"[execd:ctx] Failed to delete kernel context_id={context_id} "
                f"thread={key} error={exc}"
            )

    @staticmethod
    def _shell_tool_code(workspace_root: str, index_url: str = "") -> str:
        """Inline ``run_command`` that runs directly in the sandbox.

        Same reasoning as ``_filesystem_tools_code``: the agent's kernel already
        runs inside execd at ``workspace_root``, so a shell command belongs right
        here — no HTTP callback to CUGA and back.

        Without this the tool fell through to the generic registry stub, which is
        wrong twice over: ``run_command`` is not a registry API, and the stub is
        ``(**kwargs)`` while the prompt documents ``await run_command("cmd")``,
        so every call died with "takes 0 positional arguments but 1 was given".

        Output contract matches ``format_run_command_output``: stdout on success,
        stdout + "\n[stderr]\n" + stderr on failure.
        """
        return f'''
import asyncio as _cuga_sh_aio
import os as _cuga_sh_os

_CUGA_SH_ROOT = {workspace_root!r}
_CUGA_SH_VENV = _cuga_sh_os.path.join(_CUGA_SH_ROOT, ".venv")
_CUGA_SH_INDEX = {index_url!r}


async def run_command(cmd: str) -> str:
    """Run a shell command in the workspace and return its output."""
    _env = dict(_cuga_sh_os.environ)
    _env["VIRTUAL_ENV"] = _CUGA_SH_VENV
    _env["PATH"] = _CUGA_SH_VENV + "/bin:" + _env.get("PATH", "")
    if _CUGA_SH_INDEX:
        _env["UV_INDEX_URL"] = _CUGA_SH_INDEX
        _env["PIP_INDEX_URL"] = _CUGA_SH_INDEX
        _env["UV_INDEX_STRATEGY"] = "first-index"
    _proc = await _cuga_sh_aio.create_subprocess_shell(
        cmd,
        cwd=_CUGA_SH_ROOT,
        env=_env,
        stdout=_cuga_sh_aio.subprocess.PIPE,
        stderr=_cuga_sh_aio.subprocess.PIPE,
    )
    _out, _err = await _proc.communicate()
    _out_s = _out.decode("utf-8", "replace")
    _err_s = _err.decode("utf-8", "replace")
    if _proc.returncode != 0 and _err_s.strip():
        return _out_s + "\\n[stderr]\\n" + _err_s
    return _out_s or "(command completed with no output)"
'''

    @staticmethod
    def _filesystem_tools_code(workspace_root: str) -> str:
        """Inline Python definitions for filesystem tools that run directly in the sandbox.

        No HTTP callback to CUGA needed — the agent's kernel already runs inside
        the execd sandbox at ``workspace_root``, so Path operations work directly.
        The signatures match WorkspaceFilesystem so the agent's generated code is identical
        regardless of which backend is active.
        """
        # workspace_root is e.g. /workspace/d57c950e-...
        return f'''
import base64 as _cuga_b64
import fnmatch as _cuga_fnm
import glob as _cuga_glob
import json as _cuga_json
import os as _cuga_os
import ast as _cuga_ast
import textwrap as _cuga_tw
from pathlib import Path as _CugaPath

_CUGA_WS_ROOT = _CugaPath({workspace_root!r})

def _cuga_resolve(path):
    raw = str(path or "").strip().replace("\\\\", "/")
    if not raw or raw in ("/workspace", "."):
        return _CUGA_WS_ROOT
    for pfx in ("/workspace/", "/workspace", "/tmp/"):
        if raw.startswith(pfx):
            raw = raw[len(pfx):]
            break
    import posixpath as _pp
    rel = _pp.normpath(raw.lstrip("/"))
    if rel == ".":
        return _CUGA_WS_ROOT
    if rel == ".." or rel.startswith("../"):
        raise ValueError("path escapes workspace: " + path)
    target = (_CUGA_WS_ROOT / rel).resolve()
    base   = _CUGA_WS_ROOT.resolve()
    if target != base and base not in target.parents:
        raise ValueError("path escapes workspace: " + path)
    return target

def _cuga_public(p):
    rel = _cuga_os.path.relpath(str(p), str(_CUGA_WS_ROOT.resolve()))
    return "/workspace" if rel == "." else "/workspace/" + rel.replace(_cuga_os.sep, "/")

def _cuga_validate_py(content, path):
    try:
        _cuga_ast.parse(content)
        return None
    except SyntaxError as e:
        return str(e)

async def write_file(path, content):
    """Write text content into a file in the workspace."""
    import ast as _a, textwrap as _tw
    p = _cuga_resolve(path)
    if str(path).endswith(".py"):
        dedented = _tw.dedent(content)
        err = _cuga_validate_py(dedented, path)
        if err:
            return "[write_file error] " + err + ". Rewrite and retry."
        content = dedented
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return "File written: " + _cuga_public(p) + " (" + str(len(content)) + " chars)"

async def read_file(path, start_line=None, end_line=None, grep_pattern=None):
    """Read a text file from the workspace."""
    p = _cuga_resolve(path)
    if not p.is_file():
        return "[read_file error] File not found: " + str(path)
    text = p.read_text(encoding="utf-8", errors="replace")
    if start_line is not None or end_line is not None:
        lines = text.splitlines(keepends=True)
        s = (start_line or 1) - 1
        e = end_line or len(lines)
        text = "".join(lines[s:e])
    if grep_pattern:
        import re as _re
        try:
            pat = _re.compile(grep_pattern)
            text = "\\n".join("LINE|" + ln for ln in text.splitlines() if pat.search(ln))
        except Exception as _ge:
            return "[read_file error] invalid grep_pattern: " + str(_ge)
    return text

async def list_files(path=".", pattern="*"):
    """List files and directories in the workspace as JSON."""
    import json as _j
    p = _cuga_resolve(path)
    if p == _CUGA_WS_ROOT:
        p.mkdir(parents=True, exist_ok=True)
    if not p.exists():
        return "[list_files error] Path not found: " + str(path)
    entries = []
    for child in sorted(p.glob(pattern)):
        entries.append({{
            "name": child.name,
            "path": _cuga_public(child),
            "is_dir": child.is_dir(),
            "size_bytes": child.stat().st_size if child.is_file() else 0,
        }})
    return _j.dumps({{"sandbox_path": str(path), "entries": entries}})

async def make_directory(path):
    """Create a directory in the workspace."""
    p = _cuga_resolve(path)
    p.mkdir(parents=True, exist_ok=True)
    return "Directory created: " + _cuga_public(p)

async def move_file(source, destination):
    """Move a file within the workspace."""
    src = _cuga_resolve(source)
    dst = _cuga_resolve(destination)
    if dst.exists():
        return "[move_file error] Destination already exists: " + str(destination)
    dst.parent.mkdir(parents=True, exist_ok=True)
    _cuga_os.rename(str(src), str(dst))
    return "Moved " + _cuga_public(src) + " to " + _cuga_public(dst)

async def search_files(path, pattern, excludePatterns=None):
    """Search files in the workspace."""
    base = _cuga_resolve(path)
    exclude = excludePatterns or []
    results = []
    if "**" in pattern:
        for m in _cuga_glob.glob(str(base / pattern), recursive=True):
            rel = _cuga_os.path.relpath(m, str(base))
            if not any(_cuga_fnm.fnmatch(rel, ex) for ex in exclude):
                results.append(_cuga_public(_CugaPath(m)))
    else:
        for item in sorted(_cuga_os.listdir(str(base))):
            if _cuga_fnm.fnmatch(item, pattern) and not any(_cuga_fnm.fnmatch(item, ex) for ex in exclude):
                results.append(_cuga_public(base / item))
    return "\\n".join(results)

async def get_file_info(path):
    """Get metadata for a file in the workspace."""
    p = _cuga_resolve(path)
    st = p.stat()
    from datetime import datetime as _dt
    return "\\n".join([
        "path: " + _cuga_public(p),
        "size: " + str(st.st_size),
        "isDirectory: " + str(p.is_dir()),
        "isFile: " + str(p.is_file()),
        "modified: " + _dt.fromtimestamp(st.st_mtime).isoformat(),
    ])

async def edit_file(path, edits, dryRun=False):
    """Make exact-text edits to a file."""
    p = _cuga_resolve(path)
    if not p.is_file():
        return "[edit_file error] File not found: " + str(path)
    content = p.read_text(encoding="utf-8", errors="replace")
    new_content = content
    applied = []
    for edit in (edits or []):
        old, new = edit.get("oldText", ""), edit.get("newText", "")
        if content.count(old) != 1:
            return "[edit_file error] oldText not found exactly once: " + repr(old[:80])
        new_content = new_content.replace(old, new, 1)
        applied.append("- " + repr(old[:40]) + " → " + repr(new[:40]))
    if not dryRun and new_content != content:
        p.write_text(new_content, encoding="utf-8")
    return ("DRY RUN\\n" if dryRun else "") + "\\n".join(applied)
'''

    def _serialize_tools(
        self,
        locals_dict: dict[str, Any],
        apps_list: Optional[List[str]] = None,
    ) -> str:
        """Serialize tools for the execd sandbox.

        Filesystem tools (tagged ``_cuga_app_name='filesystem'``) are skipped here —
        they are injected as inline Python definitions via ``_filesystem_tools_code``
        in ``execute_for_cuga_lite`` and are therefore already defined in the kernel.
        All other tools go through the parent E2BExecutor serializer.
        """
        import asyncio as _asyncio
        import inspect as _inspect
        import textwrap as _textwrap

        lines = ["# Tool functions from previous execution"]

        for tool_name, tool_func in locals_dict.items():
            if not callable(tool_func) or tool_name.startswith("_"):
                continue
            if not _asyncio.iscoroutinefunction(tool_func):
                continue

            # Filesystem tools are already defined inline — skip serialization.
            if getattr(tool_func, "_cuga_app_name", None) == "filesystem":
                continue

            # Same for run_command: _shell_tool_code defines it in the kernel.
            if tool_name == "run_command":
                continue

            try:
                knowledge_scopes = getattr(tool_func, "_knowledge_allowed_scopes", None)
                if knowledge_scopes is not None:
                    lines.append(self._serialize_knowledge_tool_stub(tool_name, tool_func))
                    continue

                source = _inspect.getsource(tool_func)
                dedented = _textwrap.dedent(source)
                if f"def {tool_name}" in dedented or f"async def {tool_name}" in dedented:
                    lines.append(dedented)
                    continue

                logger.debug(f"Tool '{tool_name}' is a registry wrapper, generating call_api stub")
                sorted_apps = sorted(apps_list or [], key=len, reverse=True)
                app_name_guess = "unknown"
                for app in sorted_apps:
                    if tool_name.startswith(app + "_"):
                        app_name_guess = app
                        break
                if app_name_guess == "unknown":
                    parts = tool_name.split("_", 1)
                    if len(parts) >= 2:
                        app_name_guess = parts[0]
                lines.append(
                    f'async def {tool_name}(**kwargs):\n'
                    f'    """Registry tool: {tool_name}"""\n'
                    f'    return await call_api("{app_name_guess}", "{tool_name}", kwargs)\n'
                )

            except (OSError, TypeError) as e:
                logger.debug(f"Could not get source for tool '{tool_name}': {e}")
                lines.append(
                    f"async def {tool_name}(*args, **kwargs):\n"
                    f'    """Tool stub for {tool_name}"""\n'
                    f'    return await call_api("unknown", "{tool_name}", kwargs)\n'
                )

        return "\n".join(lines) + "\n\n" if len(lines) > 1 else ""

    async def execute_for_cuga_lite(
        self,
        wrapped_code: str,
        context_locals: dict[str, Any],
        state: AgentState,
        thread_id: Optional[str] = None,
        apps_list: Optional[List[str]] = None,
    ) -> tuple[str, dict[str, Any]]:
        from ..common import CallApiHelper

        if context_locals is None:
            context_locals = {}

        tool_names = [k for k, v in context_locals.items() if callable(v) and not k.startswith("_")]
        logger.info(
            f"[execd:route] execute_for_cuga_lite thread={thread_id} "
            f"tools={tool_names} variables={list(state.variables_manager.get_variable_names()) if state and state.variables_manager else []}"
        )

        try:
            var_manager = state.variables_manager if state else VariablesManager()
            variables_code = var_manager.get_variables_formatted()
            # Filesystem tools: inline Python definitions that work directly in the
            # execd kernel — no HTTP round-trip needed.
            has_fs_tools = any(
                getattr(v, "_cuga_app_name", None) == "filesystem"
                for v in context_locals.values()
                if callable(v)
            )
            fs_code = self._filesystem_tools_code(self._workspace_path(thread_id)) if has_fs_tools else ""
            has_shell_tool = callable(context_locals.get("run_command"))
            shell_code = (
                self._shell_tool_code(
                    self._workspace_path(thread_id),
                    getattr(settings.advanced_features, "execd_package_index", "") or "",
                )
                if has_shell_tool
                else ""
            )
            tools_code = self._serialize_tools(context_locals, apps_list=apps_list)

            function_call_url = CallApiHelper.get_function_call_url()
            trajectory_path = CallApiHelper.get_trajectory_path()
            call_api_helper = CallApiHelper.create_remote_call_api_code(function_call_url, trajectory_path)

            # int() cast: the value is substituted into a code template. A
            # non-int would inject syntactically broken Python and surface as a
            # NameError inside the sandbox instead of failing here.
            sandbox_timeout = int(settings.advanced_features.sandbox_execution_timeout)

            complete_code = f"""
import asyncio
{call_api_helper}
{fs_code}
{shell_code}
{tools_code}
{variables_code}
{wrapped_code}

# Execute and capture locals
async def main():
    __result_locals = await asyncio.wait_for(_async_main(), timeout={sandbox_timeout})
    print("!!!===!!!")
    print(__result_locals)

await main()
"""

            logger.debug(
                f"[execd:route] Assembled code for execd "
                f"vars={var_manager.get_variable_count()} tools={len(tool_names)} "
                f"code_len={len(complete_code)}"
            )

            t0 = time.monotonic()
            raw = await self._run_remote(complete_code, thread_id)
            elapsed = int((time.monotonic() - t0) * 1000)
            result, result_locals = self._parse_execution_output(raw)

            if not result_locals:
                logger.warning(
                    f"[execd:route] Execution returned no parseable locals "
                    f"thread={thread_id} duration_ms={elapsed}"
                )
            else:
                logger.info(
                    f"[execd:route] ✓ execute_for_cuga_lite thread={thread_id} "
                    f"new_vars={list(result_locals.keys())} duration_ms={elapsed}"
                )

            return result, result_locals

        except Exception as e:
            raise RuntimeError(f"execd sandbox execution failed: {e}")

    async def execute_for_code_agent(
        self,
        wrapped_code: str,
        state: AgentState,
        thread_id: Optional[str] = None,
    ) -> str:
        from ..common import CallApiHelper

        function_call_url = CallApiHelper.get_function_call_url()
        trajectory_path = CallApiHelper.get_trajectory_path()
        call_api_helper = CallApiHelper.create_remote_call_api_code(function_call_url, trajectory_path)

        variables_code = state.variables_manager.get_variables_formatted() if state.variables_manager else ""

        complete_code = f"""
import asyncio
{call_api_helper}

{variables_code}

{wrapped_code}

await _async_main()
"""

        return await self._run_remote(complete_code, thread_id)
