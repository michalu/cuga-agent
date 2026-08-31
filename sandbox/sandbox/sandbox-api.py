"""Sandbox control-plane API.

Exposes management operations for the OpenShell + execd sandbox deployment:
sandbox status, restart, installed-package listing, and active-thread listing.

Authentication: every request must carry the sandbox API key in the
``X-API-Key`` header.  The key is generated once by ``setup.sh`` and stored at
``/var/lib/openshell/sandbox-api-key``; it is injected into this process as the
``SANDBOX_API_KEY`` environment variable.

This service is the control plane.  The data plane (code execution) is execd on
port 44772 — agents call it directly and do not go through this service.

Endpoints
---------
GET  /ping
GET  /status
POST /restart
GET  /packages
GET  /threads
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
from typing import Any, Optional

import httpx
from fastapi import FastAPI, Header, HTTPException, Query
from fastapi.responses import JSONResponse

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

SANDBOX_API_KEY: str = os.environ["SANDBOX_API_KEY"]
EXECD_URL: str = os.environ.get("EXECD_URL", "http://localhost:44772").rstrip("/")
EXEC_SANDBOX_NAME: str = os.environ.get("EXEC_SANDBOX_NAME", "code-exec")
EXEC_SANDBOX_PORT: int = int(os.environ.get("EXEC_SANDBOX_PORT", "44772"))
# Path to the policy file and build-context dir used when re-creating the
# sandbox on restart.  Both are present inside this container (copied at build
# time) so the restart endpoint is self-contained.
EXECD_POLICY_PATH: str = os.environ.get("EXECD_POLICY_PATH", "/opt/sandbox/execd-policy.yaml")
EXECD_BUILD_CONTEXT: str = os.environ.get("EXECD_BUILD_CONTEXT", "/opt/sandbox/build-context")

# ---------------------------------------------------------------------------
# App + auth
# ---------------------------------------------------------------------------

app = FastAPI(title="Sandbox management API", version="0.1.0")


def _verify(x_api_key: str = Header(..., alias="X-API-Key")) -> None:
    """Dependency: reject any request that does not carry the correct key."""
    if x_api_key != SANDBOX_API_KEY:
        raise HTTPException(status_code=401, detail="invalid API key")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _openshell(*args: str, check: bool = False) -> subprocess.CompletedProcess:
    """Run an openshell CLI command and return the result."""
    return subprocess.run(
        ["openshell", *args],
        capture_output=True,
        text=True,
        check=check,
    )


async def _execd_command(
    command: str,
    *,
    env: Optional[dict[str, str]] = None,
    timeout: float = 30.0,
) -> tuple[str, str, int]:
    """Run a shell command inside the execd sandbox via POST /command.

    Returns (stdout, stderr, exit_code).  The response is a newline-delimited
    JSON stream; events of type ``stdout`` / ``stderr`` carry text, and the
    final ``status`` event carries the exit code.
    """
    payload: dict[str, Any] = {"command": command}
    if env:
        payload["env"] = env

    stdout_parts: list[str] = []
    stderr_parts: list[str] = []
    exit_code: int = 0

    async with httpx.AsyncClient(timeout=timeout) as client:
        async with client.stream(
            "POST", f"{EXECD_URL}/command", json=payload
        ) as response:
            if response.status_code != 200:
                raise HTTPException(
                    status_code=502,
                    detail=f"execd returned {response.status_code}",
                )
            async for line in response.aiter_lines():
                line = line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                t = event.get("type")
                if t == "stdout":
                    stdout_parts.append(event.get("text", ""))
                elif t == "stderr":
                    stderr_parts.append(event.get("text", ""))
                elif t == "status":
                    exit_code = int(event.get("exit_code", 0))
                elif t == "error":
                    exit_code = int((event.get("error") or {}).get("exit_code", 1))

    return "".join(stdout_parts), "".join(stderr_parts), exit_code


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.get("/ping", tags=["health"])
async def ping() -> dict:
    """Liveness probe — no auth required."""
    return {"status": "ok"}


@app.get("/status", tags=["management"], dependencies=[])
async def status(x_api_key: str = Header(..., alias="X-API-Key")) -> JSONResponse:
    """Return the status of the OpenShell gateway and the execd sandbox.

    Combines:
    - ``openshell status``  — gateway connectivity
    - ``openshell sandbox list``  — sandbox lifecycle state
    - ``GET execd/ping``  — whether execd is accepting requests
    """
    _verify(x_api_key)

    gateway = _openshell("status")
    sandboxes = _openshell("sandbox", "list")

    execd_ok: bool = False
    execd_error: str = ""
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            r = await client.get(f"{EXECD_URL}/ping")
            execd_ok = r.status_code == 200
    except Exception as exc:
        execd_error = str(exc)

    return JSONResponse({
        "gateway": {
            "connected": gateway.returncode == 0,
            "output": gateway.stdout.strip(),
        },
        "sandboxes": {
            "raw": sandboxes.stdout.strip(),
        },
        "execd": {
            "reachable": execd_ok,
            "url": EXECD_URL,
            **({"error": execd_error} if execd_error else {}),
        },
    })


@app.post("/restart", tags=["management"])
async def restart(x_api_key: str = Header(..., alias="X-API-Key")) -> JSONResponse:
    """Tear down and re-create the execd sandbox.

    Replays the same ``openshell sandbox create`` call that ``setup.sh``
    performs.  Active kernels are lost; workspaces on disk survive because
    ``/workspace`` is a volume.

    Does **not** expose an interactive shell.
    """
    _verify(x_api_key)

    _openshell("forward", "stop", str(EXEC_SANDBOX_PORT), EXEC_SANDBOX_NAME)
    _openshell("sandbox", "delete", EXEC_SANDBOX_NAME)

    create = _openshell(
        "sandbox", "create",
        "--name", EXEC_SANDBOX_NAME,
        "--from", EXECD_BUILD_CONTEXT,
        "--forward", str(EXEC_SANDBOX_PORT),
        "--policy", EXECD_POLICY_PATH,
        "--no-auto-providers",
        "--detach",
        "--", "/usr/local/bin/execd-entrypoint",
    )

    if create.returncode != 0:
        raise HTTPException(
            status_code=500,
            detail=f"sandbox create failed: {create.stderr.strip()}",
        )

    return JSONResponse({"status": "restarting", "sandbox": EXEC_SANDBOX_NAME})


@app.get("/threads", tags=["management"])
async def threads(x_api_key: str = Header(..., alias="X-API-Key")) -> JSONResponse:
    """List threads that have an on-disk workspace inside the sandbox.

    A thread appears here as soon as its bootstrap code creates
    ``/workspace/<thread>``.  Threads that hold only a live kernel but have
    written nothing yet are not visible (execd does not expose a context list).
    """
    _verify(x_api_key)

    stdout, stderr, code = await _execd_command(
        "find /workspace -mindepth 1 -maxdepth 1 -type d -printf '%f\\n'"
    )

    if code != 0:
        raise HTTPException(status_code=502, detail=stderr.strip() or "find failed")

    thread_ids = [t for t in stdout.splitlines() if t.strip()]
    return JSONResponse({"threads": thread_ids, "count": len(thread_ids)})


@app.get("/packages", tags=["management"])
async def packages(
    x_api_key: str = Header(..., alias="X-API-Key"),
    thread_id: Optional[str] = Query(
        None,
        description=(
            "Thread ID whose virtualenv to inspect.  "
            "Omit to list packages installed in the sandbox base image."
        ),
    ),
) -> JSONResponse:
    """List Python packages installed in a thread's virtualenv or the base image.

    Uses ``uv pip list --format=json`` inside the sandbox via POST /command so
    the result reflects exactly what is reachable inside the execution environment.
    """
    _verify(x_api_key)

    if thread_id:
        # Sanitise: same rules as ExecdExecutor._workspace_name
        safe = "".join(c if (c.isalnum() or c in "-_") else "-" for c in thread_id)[:64]
        venv = f"/workspace/{safe}/.venv"
        command = f"VIRTUAL_ENV={venv} PATH={venv}/bin:$PATH uv pip list --format=json"
    else:
        command = "uv pip list --format=json"

    stdout, stderr, code = await _execd_command(command)

    if code != 0:
        raise HTTPException(
            status_code=502,
            detail=stderr.strip() or "uv pip list failed",
        )

    try:
        pkg_list = json.loads(stdout.strip())
    except json.JSONDecodeError:
        raise HTTPException(status_code=502, detail=f"unexpected output: {stdout[:200]}")

    return JSONResponse({
        "thread_id": thread_id,
        "packages": pkg_list,
        "count": len(pkg_list),
    })


@app.get("/info", tags=["management"])
async def info(x_api_key: str = Header(..., alias="X-API-Key")) -> JSONResponse:
    """Return sandbox connection information for consumers (CUGA, BYOA agents).

    This is the canonical reference for how to connect to this sandbox instance.
    BYOA developers call it once — after receiving ``SANDBOX_URL`` and
    ``SANDBOX_API_KEY`` from the Sovereign Core service binding — to verify
    their credentials and discover the exact URLs and headers required for code
    execution.

    Response fields
    ---------------
    execd_url
        URL for the **data plane**.  Send ``POST /code`` and ``POST /command``
        here to execute Python code and shell commands.  Include
        ``X-API-Key: <SANDBOX_API_KEY>`` in every request when
        ``auth_required`` is true.
    management_url
        URL for this **control plane** (sandbox-api).  Used for admin
        operations: ``GET /status``, ``POST /restart``, ``GET /packages``,
        ``GET /threads``.  Always requires ``X-API-Key``.
    auth_required
        True when the data plane (execd) is behind an Ingress that validates
        ``X-API-Key`` — i.e. in a catalog/multi-tenant deployment.  False in
        a local or cluster-internal setup where network isolation is
        sufficient.  The control plane always requires the key regardless.
    auth_header
        The header name: ``X-API-Key``.
    byoa_instructions
        Plain-text summary of what a BYOA developer must do.
    example_code_request
        A ready-to-run ``curl`` invocation that executes ``print("hello")``
        in a new execd context, with actual URLs and headers filled in.
    """
    _verify(x_api_key)

    loopback_prefixes = (
        "http://localhost", "http://127.", "http://host.openshell", "http://host.docker"
    )
    data_plane_auth_required = not any(EXECD_URL.startswith(p) for p in loopback_prefixes)

    management_url = os.environ.get(
        "SANDBOX_MANAGEMENT_URL",
        f"http://localhost:{os.environ.get('SANDBOX_API_PORT', '8090')}",
    )

    auth_fragment = '-H "X-API-Key: $SANDBOX_API_KEY" ' if data_plane_auth_required else ""
    example = (
        f'curl -s {auth_fragment}'
        f'-H "Content-Type: application/json" '
        f'''-d '{{"context": {{"id": "my-context", "language": "python"}}, '''
        f'''"code": "print(\\"hello\\")"}}' '''
        f'"{EXECD_URL}/code"'
    )

    return JSONResponse({
        "execd_url": EXECD_URL,
        "management_url": management_url,
        "auth_required": data_plane_auth_required,
        "auth_header": "X-API-Key",
        "byoa_instructions": (
            "1. Read SANDBOX_URL (= execd_url) and SANDBOX_API_KEY from the "
            "Sovereign Core service binding. "
            "2. Send POST /code or POST /command to execd_url with "
            "'X-API-Key: <SANDBOX_API_KEY>' when auth_required is true. "
            "3. Use management_url for admin operations (status, restart, "
            "packages, threads) — always requires X-API-Key. "
            "Both URLs accept the same key."
        ),
        "example_code_request": example,
    })


# ---------------------------------------------------------------------------
# Entry point (for local debugging without uvicorn CLI)
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("SANDBOX_API_PORT", "8090")))
