# Low Level Design - CUGA Sandbox

## 1. Repository layout

```
sandbox/
├── README.md                     # Operational reference, quick start
├── Makefile                      # All deploy / teardown / smoke-test targets
├── smoke-test.sh                 # End-to-end verification script
│
├── docs/                         # Documentation
│   ├── HLD.md                    # High Level Design (this document's companion)
│   ├── LLD.md                    # This document
│   └── diagram/                  # Architecture diagrams
│       ├── architecture.drawio
│       ├── architecture.png
│       └── demo.png
│
├── cuga/                         # Role A - CUGA agent sandbox
│   ├── Dockerfile.openshell      # Builds the CUGA image (wraps cuga start demo_crm)
│   ├── cuga-entrypoint.sh        # Env wiring + TCP proxy PID for netns bridge
│   └── deploy/
│       ├── rancher/cuga-policy.yaml     # Egress allowlist (Rancher variant)
│       └── openshift/
│           ├── cuga-policy.yaml         # Egress allowlist (OpenShift variant)
│           ├── kustomization.yaml
│           └── manifests/
│               ├── cuga-deployment.yaml # Service + Route only (pod is gateway-owned)
│               └── rbac.yaml
│
└── sandbox/                      # Role B + control plane
    ├── Dockerfile.execd          # execd image (Debian/slim - Rancher)
    ├── Dockerfile.execd.ocp      # execd image (UBI9 - OpenShift)
    ├── Dockerfile.sandbox-api    # sandbox-api image (both)
    ├── Dockerfile.sandbox-api.ocp
    ├── execd-entrypoint.sh       # Starts Jupyter, waits for ready, execs execd
    ├── execd-isolation.toml      # execd hardening config (hardening disabled)
    ├── sandbox-api.py            # FastAPI management sidecar
    ├── sandbox-api.md            # curl examples for sandbox-api
    ├── sandbox-relay.py          # TCP bridge VM↔macOS (Rancher only)
    └── deploy/
        ├── rancher/
        │   ├── docker-compose.yml       # gateway + relay + sandbox-api
        │   ├── execd-policy.yaml
        │   └── gateway.toml
        └── openshift/
            ├── execd-policy.yaml
            ├── gateway.toml
            ├── kustomization.yaml
            └── manifests/
                ├── gateway-deployment.yaml   # Deployment + Service + Route
                ├── sandbox-api-deployment.yaml
                ├── sandbox-proxy.yaml        # nginx Deployment + 3 Services + ConfigMap
                ├── namespace.yaml
                ├── pvc.yaml
                └── rbac.yaml
```

---

## 2. OpenShell gateway

### Startup sequence

1. `openshell-init` initContainer runs `openshell-gateway generate-certs`
   - writes Ed25519 JWT keypair to the PVC (`/var/lib/openshell/tls/jwt/`).
   Idempotent: leaves existing keys untouched on restart.
2. Gateway container starts, reads `gateway.toml` from a ConfigMap volume at
   `/etc/openshell/gateway.toml`.
3. Gateway opens SQLite at `sqlite:/var/lib/openshell/gateway.db?mode=rwc`
   (or Postgres URL if `OPENSHELL_DB_URL` is overridden for HA).
4. Gateway binds `:8080` (gRPC proxy) and `:8081` (health `/healthz`).

### Compute driver - Kubernetes (OpenShift)

Configured in `gateway.toml`:

```toml
[openshell.drivers.kubernetes]
namespace         = "${NAMESPACE}"
grpc_endpoint     = "http://openshell-gateway.${NAMESPACE}.svc.cluster.local:8080"
```

The gateway uses its `ServiceAccount` (`openshell-gateway`) with RBAC to call
the Kubernetes API. It creates sandbox pods in the same namespace. The pod spec
sets the container command to the OpenShell supervisor binary; the gateway
mounts the policy ConfigMap as a volume.

### Compute driver - Docker (Rancher Desktop)

The gateway container mounts `/var/run/docker.sock` and calls the Docker API to
create sibling containers (not Docker-in-Docker). Policy ConfigMap is replaced
by a bind-mounted YAML file.

### Gateway database

The gateway holds `gateway.db` - a mapping of `workspace → sandbox → pod name →
exposed port`. This database is the only index of live sandbox pods. All inbound
traffic routing and lifecycle operations require it.

- **Single-replica** (current): SQLite on PVC `openshell-state`.
- **HA path**: replace `OPENSHELL_DB_URL` with a Postgres DSN.

### JWT keypair

Sandbox pods are issued a JWT signed by the gateway's Ed25519 private key. The
supervisor inside each sandbox uses the JWT to authenticate its gRPC callback to
the gateway (policy check, egress forwarding). The public key is on the PVC
alongside the private key.

---

## 3. sandbox-proxy - Host-header rewriting (OpenShift only)

### Why it exists

The gateway routes inbound traffic entirely by the HTTP `Host` header in the
format `<workspace>--<sandbox>--<service>.openshell.localhost`. HAProxy Route
(OCP ingress) cannot rewrite `Host`. Without a rewrite, every request arrives
with the public cluster hostname (e.g.
`cuga-demo-sandbox-alice.apps.cluster.example.com`) and the gateway returns 404
because no sandbox is registered under that name.

`sandbox-proxy` is an nginx Deployment that sits between the Services (which
CUGA's policy declares) and the gateway.

### nginx server blocks

Defined in ConfigMap `sandbox-proxy-conf`, mounted at
`/etc/nginx/custom/nginx.conf`:

| Listen port | Rewrites `Host` to | Services that point here |
|---|---|---|
| `7860` | `openshell--cuga-demo--ui.openshell.localhost` | `cuga-demo` (`:7860`) → OCP Route → user |
| `8001` | `openshell--cuga-demo--registry.openshell.localhost` | `cuga-demo` (`:8001`) ← execd tool callbacks |
| `44772` | `openshell--code-exec--execd.openshell.localhost` | `execd-service` (`:44772`) ← CUGA code requests |

The `${OPENSHELL_WORKSPACE}` variable (default `openshell`) is substituted by
`envsubst` during `kustomize build`.

### Proxy settings

- `proxy_buffering off` - required for Gradio SSE streams and WebSocket.
- `proxy_read_timeout 3600s` - long-lived SSE connections during LLM inference.
- `client_max_body_size 0` - no limit; generated code payloads can be large.
- All temp paths under `/tmp` - the pod runs as an arbitrary UID on OpenShift.

### Service wiring

```
cuga-demo Service  →  sandbox-proxy pod  →  openshell-gateway Service
execd-service Service  →  sandbox-proxy pod  →  openshell-gateway Service
```

Both `cuga-demo` and `execd-service` Services use `selector: app: sandbox-proxy`
- they point at the proxy pod, not at the sandbox pods directly.

---

## 4. CUGA sandbox (Role A)

### Image

Built by `cuga/Dockerfile.openshell`. Base: the standard CUGA image. Adds:
- `nsenter` (for the TCP proxy in `cuga-entrypoint.sh`)
- `cuga-entrypoint.sh` set as the container command

### Entrypoint - `cuga/cuga-entrypoint.sh`

Sets CUGA env vars, then does two things before `exec cuga start demo_crm`:

**1. TCP proxy in the main netns (lines 57–140)**

OpenShell puts the workload on `10.200.0.2` in a private veth pair. The pod's
`eth0` (cluster IP) belongs to the outer namespace - the supervisor. For
Services to reach the workload, a relay is needed between `eth0` and
`10.200.0.2`.

The entrypoint uses `nsenter -t 1 -n` to run a Python TCP proxy in the outer
(PID 1 / supervisor) netns. It binds `0.0.0.0:{7860,8001}` there and forwards
to `10.200.0.2:{7860,8001}` (the workload's actual address). This is the bridge
that makes `cuga-demo` Service traffic actually reach the Gradio UI and tool
registry.

**2. Env wiring**

| Env var set | Value | Effect |
|---|---|---|
| `DYNACONF_ADVANCED_FEATURES__SANDBOX_MODE` | `execd` | Routes generated code to execd instead of running in-process |
| `OPENAI_BASE_URL` | `https://inference.local/v1` | Virtual hostname intercepted by the gateway |
| `OPENAI_API_KEY` | `unused` | Gateway injects the real key on egress |
| `DYNACONF_ADVANCED_FEATURES__EXECD_URL` | `http://execd-service:44772` (OCP) or `http://host.openshell.internal:44772` (Rancher) | Where CUGA sends `POST /code` |
| `DYNACONF_SERVER_PORTS__FUNCTION_CALL_HOST` | `http://cuga-demo:8001` (OCP) or `http://host.openshell.internal:8001` (Rancher) | Tool registry URL injected into execd kernel context so generated code can call back |
| `HF_HUB_DISABLE_XET` | `1` | Forces classic CDN path for fastembed; Xet CAS endpoint is runtime-resolved and cannot be allowlisted |

### cuga-policy.yaml (OpenShift variant)

```yaml
network_policies:
  openshell_inference:       # inference.local:443  - /app/.venv/bin/python* only
  fastembed_model_download:  # huggingface.co + us.aws.cdn.hf.co:443
  cuga_code_sandbox:         # execd-service.<ns>.svc.cluster.local:44772
  cuga_local_services:       # 127.0.0.1:{7860,8001,8007} - intra-pod loopback
```

Filesystem: `/app` read-only; `/sandbox` and `/tmp` read-write.

### Writable paths

```
/sandbox/cuga_workspace/   - CRM demo data (contacts.txt etc.)
/tmp/cuga/dbs/             - CUGA agent databases (SQLite)
/tmp/cuga/logging/         - CUGA log file
/tmp/cuga/policies/        - CUGA policy store
/tmp/cuga/fastembed/       - embedding model cache (seeded from /app/fastembed-cache)
/tmp/cuga/hf/              - Hugging Face home
```

---

## 5. execd sandbox (Role B)

### Images

| Platform | Base | venv location |
|---|---|---|
| Rancher | `python:3.12-slim` (Debian) | `/usr/local/bin/uv`, `/usr/local/bin/python*` |
| OpenShift | `ubi9/python-312` (UBI9) | `/opt/app-root/bin/uv`, `/opt/app-root/bin/python*` |

Both images install: `execd` binary, `opensandbox-session-gate`,
`opensandbox-launcher`, Jupyter, ipykernel, uv.

### Entrypoint - `sandbox/execd-entrypoint.sh`

1. Redirects all Jupyter/IPython state to `/tmp` (arbitrary UID on OCP).
2. Generates a random 32-byte `JUPYTER_TOKEN` (lives in process env only;
   never leaves the loopback).
3. Starts `jupyter notebook --ip=127.0.0.1 --port=54321`.
4. Polls `http://127.0.0.1:54321/api` for up to 90 s; aborts if not ready.
5. Execs `execd --jupyter-host=http://127.0.0.1:54321 --port=44772
   --isolation-config=/etc/opensandbox/isolation.toml`.

### execd API

| Endpoint | What it does |
|---|---|
| `POST /code/context` | Creates a new Jupyter kernel (persistent Python process). Returns `context_id`. |
| `POST /code` | Executes a code block in an existing kernel. Streams NDJSON output. |
| `POST /command` | Runs a shell command (used for `run_command()` and `uv pip install`). Streams NDJSON. |
| `GET /ping` | Liveness check. |

### execd-isolation.toml

```toml
[hardening]
enabled = false   # disabled - OpenShell's seccomp denies memfd_create
                  # which opensandbox-launcher requires

[landlock]
enabled = true
extra_writable = ["/workspace"]
extra_readable = ["/opt/opensandbox", "/opt/app-root"]
```

Bubblewrap (`bwrap`) inner hardening is disabled because OpenShell's seccomp
policy denies `memfd_create`, which the launcher uses to load its inner policy.
OpenShell provides equivalent confinement via Landlock + private netns +
unprivileged user. Re-enable by setting `enabled = true` and adding
`memfd_create` to the gateway's seccomp allowlist.

### Per-thread isolation model

Each conversation thread gets:
- `/workspace/<thread_id>/` - writable workspace directory
- `/workspace/<thread_id>/.venv/` - isolated Python virtualenv
- A Jupyter kernel context (`context_id = thread_id`)

Bootstrap code (injected by `ExecdExecutor` on `POST /code/context`) creates the
venv and installs minimal dependencies. Kernels persist across code blocks within
a thread; they share one Python interpreter process, so `sys.modules` is shared.

### execd-policy.yaml (OpenShift variant)

```yaml
network_policies:
  execd_api:               # 127.0.0.1:{44772,54321} - loopback execd↔Jupyter
  cuga_tool_registry:      # cuga-demo.<ns>.svc.cluster.local:8001 - tool callbacks
  python_package_index:    # pypi.org:443 + files.pythonhosted.org:443
                           # binaries: uv, python*, workspace venv python*
  openshell_inference:     # inference.local:443 - only if code needs LLM directly
```

Filesystem: `/workspace` and `/tmp` read-write; `/opt/app-root`, `/opt/opensandbox`,
system paths read-only.

---

## 6. sandbox-api

FastAPI service (`sandbox/sandbox-api.py`) on `:8090`. Protected by
`X-API-Key: <SANDBOX_API_KEY>`. Not in the user request path.

| Endpoint | What it does | Implementation |
|---|---|---|
| `GET /ping` | Liveness - no auth | Returns `{"status":"ok"}` |
| `GET /status` | Gateway connectivity + sandbox list + execd reachability | `openshell status`, `openshell sandbox list`, `GET execd/ping` |
| `POST /restart` | Delete + re-create execd sandbox | `openshell forward stop`, `openshell sandbox delete`, `openshell sandbox create` |
| `GET /threads` | List on-disk thread workspaces | `POST execd/command` with `find /workspace -mindepth 1 -maxdepth 1 -type d` |
| `GET /packages` | List installed packages | `POST execd/command` with `uv pip list --format=json`; `?thread_id=` for per-thread venv |
| `GET /info` | BYOA connection info | Returns `execd_url`, `auth_required`, example curl |

The `SANDBOX_API_KEY` is generated once by `setup.sh` (`openssl rand -hex 32`)
and stored at `/var/lib/openshell/sandbox-api-key` on the PVC.

---

## 7. sandbox-relay (Rancher Desktop only)

`sandbox/sandbox-relay.py` - pure Python TCP proxy. Runs in the Rancher Desktop
VM with `--network host`.

**Problem it solves:** `openshell forward` binds on macOS. Containers in the
Rancher Desktop VM (Linux) cannot reach macOS directly. Sandboxes reach
`host.openshell.internal:{44772,8001}` (resolved to the VM bridge gateway);
the relay forwards to `host.rancher-desktop.internal:{44772,8001}` (macOS).

**Bind address:** `172.17.0.1` (VM bridge) - not `0.0.0.0`. Rancher Desktop
re-publishes `0.0.0.0` listeners onto macOS, which would cause the relay to
forward to itself.

**Ports:** `RELAY_PORTS=44772,8001` - one listener per port, threaded.

Not present on OpenShift - Kubernetes Service DNS takes its place.

---

## 8. Key configuration parameters

### Deploy-time (Makefile / env)

| Variable | Platform | Purpose |
|---|---|---|
| `OPENAI_API_KEY` | both | LLM credential - gateway only, never sandbox |
| `OPENAI_BASE_URL` | both | Real LLM endpoint the gateway calls |
| `MODEL_NAME` | both | Model id in the inference route |
| `REGISTRY` | OpenShift | Image registry for the CUGA image (e.g. `icr.io/<ns>`) |
| `ICR_API_KEY` | OpenShift | Creates the in-cluster pull secret |
| `NAMESPACE` | OpenShift | Target namespace (default `sandbox-$(whoami)`) |
| `OPENSHELL_WORKSPACE` | both | Prefix in `<workspace>--<sandbox>` pod names (default `openshell`) |

### CUGA behaviour (cuga-entrypoint.sh)

| Variable | Default | Effect |
|---|---|---|
| `CUGA_SANDBOX_MODE` | `execd` | Where generated code runs |
| `CUGA_ENABLE_SHELL_TOOL` | `true` | Enables `run_command` and package installs |
| `CUGA_EXECD_PACKAGE_INDEX` | `https://pypi.org/simple` | Index for installs; must match egress policy |
| `CUGA_AUTO_APPROVE` | `false` | Auto-approve tool calls |
| `CUGA_EXECD_URL` | `http://host.openshell.internal:44772` | Rancher default; override for OCP |
| `CUGA_FUNCTION_CALL_HOST` | `http://host.openshell.internal:8001` | Tool registry URL visible to execd |

---

## 9. Secrets lifecycle

| Secret | Created by | Stored in | Reaches |
|---|---|---|---|
| `OPENAI_API_KEY` | operator | Secret `llm-credentials` | gateway pod env only |
| `ICR_API_KEY` | IBM Cloud | `docker login` + Secret `icr-pull-secret` | image pulls |
| `SANDBOX_API_KEY` | `openssl rand -hex 32` in setup.sh | Secret `sandbox-credentials` + PVC `/var/lib/openshell/sandbox-api-key` | `sandbox-api` only |
| Gateway JWT keypair | `generate-certs` initContainer | PVC `openshell-state` under `/var/lib/openshell/tls/jwt/` | gateway ↔ sandbox supervisor |
| `JUPYTER_TOKEN` | `execd-entrypoint.sh` on every start | execd process env (loopback only) | execd ↔ Jupyter loopback |

Neither sandbox pod ever holds `OPENAI_API_KEY`. The CUGA sandbox holds
`EXECD_API_KEY` (needed by `ExecdExecutor` to authenticate `POST /code`); it
never reaches the execd pod.

---

## 10. Air-gapped operation

1. Pre-pull images before cutting network:
   ```bash
   docker pull ghcr.io/nvidia/openshell/gateway:latest
   docker pull ghcr.io/nvidia/openshell-community/sandboxes/base:latest
   docker pull python:3.12-slim          # Rancher execd base
   # or registry.access.redhat.com/ubi9/python-312  for OCP
   ```
2. Replace `python_package_index` endpoints in `execd-policy.yaml` with the
   internal mirror host. Both `pypi.org` and `files.pythonhosted.org` must be
   replaced - metadata and wheels come from different hosts.
3. Set `CUGA_EXECD_PACKAGE_INDEX` to the internal mirror URL.
4. Set `HF_HUB_OFFLINE=1` in `cuga-entrypoint.sh` to use the baked fastembed
   cache (`/app/fastembed-cache`) and skip the Hugging Face download entirely.
   The CUGA policy's `fastembed_model_download` block can then be removed.

Egress is deny-by-default - PyPI is unreachable unless explicitly listed.
Air-gapped posture is the correct starting point; allowlisting public hosts is
the exception.

---

## 11. Topology

### Rancher Desktop

```
macOS host
│
├─ Rancher Desktop VM  (Linux kernel, dockerd)
│   ├─ [container] openshell-execd-gateway   :8080 gRPC, :8081 health
│   ├─ [container] sandbox-relay             :44772 + :8001  VM ↔ macOS bridge
│   └─ [container] sandbox-api               :8090  management REST API
│
│   [OpenShell sandbox]  openshell--code-exec
│       └─ execd + Jupyter                   private netns, reachable via openshell forward
│
│   [OpenShell sandbox]  openshell--cuga-demo
│       └─ cuga start demo_crm               private netns, :7860 forwarded to macOS
│
└─ openshell forward binds on macOS:
       :44772  → execd
       :7860   → CUGA Gradio UI
       :8001   → CUGA tool-call registry  (code in execd → CUGA)
```

### OpenShift

```
namespace  sandbox-<user>
│
│  ── inbound (user traffic) ──────────────────────────────────────────────
│
│   HAProxy Route  cuga-demo-<ns>.apps.<cluster>   OCP ingress, TLS termination
│       └─→ Service cuga-demo :7860
│              └─→ [deploy] sandbox-proxy (nginx)  Host-header rewrite
│                      └─→ [deploy] openshell-gateway :8080  gRPC + policy engine
│                                 └─→ [pod] openshell--cuga-demo  (nested netns)
│                                         └─ cuga start demo_crm  :7860 :8001
│
│  ── internal (agent → execd) ────────────────────────────────────────────
│
│   Service execd-service :44772
│       └─→ sandbox-proxy ─→ gateway ─→ [pod] openshell--code-exec (nested netns)
│                                               └─ execd + Jupyter  :44772
│
│  ── control plane ───────────────────────────────────────────────────────
│
├─ [deploy] openshell-gateway    :8080 gRPC, :8081 health
├─ [deploy] sandbox-proxy        :7860 :8001 :44772  Host-header rewrite (nginx)
└─ [deploy] sandbox-api          :8090  management REST API
```

Pods are named `<workspace>--<sandbox>`. The workspace prefix comes from
`OPENSHELL_WORKSPACE` (default `openshell`).

---

## 12. Smoke test

`smoke-test.sh` sends one prompt and asserts on file contents inside the sandbox
- not on what the model said it did.

| Step | Exercises | Assertion |
|---|---|---|
| 1 | CRM tool → `contacts_export.txt` | tool registry + code exec + workspace write |
| 2 | `uv pip install tomli` → `config.toml` | package install through egress policy |
| 3 | shell command appends `smoke-test ok` | `run_command` shell kernel |

```bash
# OpenShift
./smoke-test.sh --platform=openshift --namespace=$NAMESPACE

# Rancher Desktop
./smoke-test.sh --platform=rancher
```

Platform is auto-detected from available tools (`docker` → rancher, `oc` →
openshift). Override with `--platform=`.

Policy decisions appear in the output as `[policy/cuga] ALLOWED ...` and
`[policy/execd] ALLOWED ...` lines - one attributed OCSF event per hop. A
misconfigured policy shows `DENIED` on the failing hop; the install or tool call
fails closed.

### Observed smoke-test trace

Verified run on OpenShift - all four layers in chronological order.
The `[policy/*]` lines come from the OpenShell supervisors and are the point of
the exercise: every hop is an explicit, attributed decision.

Each connection is evaluated by two engines and emits two log lines:
- `engine:l7` - HTTP-aware check (method + URL path)
- `engine:opa` - rule-set check (Open Policy Agent, evaluates the `.rego` policy file)

Both must allow a connection for it to proceed.

```
# Verify all three sandbox pods are Running and the CUGA endpoint responds.
==> Preflight  [openshift]
  ok  cuga    (Pod) : openshell--cuga-demo  [ns=sandbox-michal1]
  ok  execd   (Pod) : openshell--code-exec  [ns=sandbox-michal1]
  ok  gateway (Pod) : openshell-gateway-6cfb67845b-nqblr  [ns=sandbox-michal1]
  ok  CUGA reachable at https://cuga-demo-sandbox-michal1.apps.agent-cluster.cp.fyre.ibm.com

# Send the three-step prompt over SSE; tool calls are auto-approved, Answer event marks completion.
==> Sending prompt  [you → cuga via HTTP]
  ..  Thread ID : smoke-1788282553
  ..  Prompt    : Do three things in order:
1. Use the CRM tool to list available contacts and save their names to /workspace/contacts_export.txt using Python.
2. Install the 'tomli' package with pip, then use it in Python to write a small TOML file /workspace/config.toml containing key version="1.0".
3. Run a shell command that appends the line 'smoke-test ok' to /workspace/contacts_export.txt and then prints the last 3 lines of that file.
  ..  Waiting up to 120s (auto-approving tool calls)...
  Stream: HTTP 200
  Event: name='CodeAgent' data_len=426
  Event: name='CodeAgent' data_len=1633
  Event: name='CodeAgent' data_len=626
  Event: name='CodeAgent' data_len=929
  Event: name='CodeAgent' data_len=457
  Event: name='CodeAgent' data_len=201
  Event: name='CodeAgent' data_len=457
  Event: name='CodeAgent_Reasoning' data_len=925
  Event: name='CodeAgent' data_len=925
  Event: name='CodeAgent' data_len=925
  Event: name='FinalAnswerAgent' data_len=972
  Event: name='Answer' data_len=1016
  Agent finished (Answer event received)
  Total approvals sent: 0                               ← fully auto-approved; no HITL needed

# Chronological policy decisions from both sandbox supervisors; each hop is an explicit, attributed decision.
==> Run timeline  [thread=smoke-1788282553]

  # CUGA polls execd with /command keepalives and calls the LLM via inference.local while planning; no code runs yet.
  --- Policy decisions  [cuga-demo sandbox] ---
  17:09:14.733  [policy/cuga]  ALLOWED /usr/bin/python3.12(87) -> POST http://execd-service.sandbox-michal1.svc.cluster.local:44772/command [policy:cuga_code_sandbox engine:l7]
  17:09:14.734  [policy/cuga]  ALLOWED inference.local:443
  17:09:15.874  [policy/cuga]  ALLOWED /usr/bin/python3.12(87) -> POST http://execd-service.sandbox-michal1.svc.cluster.local:44772/command [policy:cuga_code_sandbox engine:opa]
  17:09:17.230  [policy/cuga]  ALLOWED /usr/bin/python3.12(87) -> POST http://execd-service.sandbox-michal1.svc.cluster.local:44772/command [policy:cuga_code_sandbox engine:l7]
  17:09:18.351  [policy/cuga]  ALLOWED /usr/bin/python3.12(87) -> POST http://execd-service.sandbox-michal1.svc.cluster.local:44772/command [policy:cuga_code_sandbox engine:opa]
  17:09:19.717  [policy/cuga]  ALLOWED /usr/bin/python3.12(87) -> POST http://execd-service.sandbox-michal1.svc.cluster.local:44772/command [policy:cuga_code_sandbox engine:l7]
  17:09:20.868  [policy/cuga]  ALLOWED /usr/bin/python3.12(87) -> POST http://execd-service.sandbox-michal1.svc.cluster.local:44772/command [policy:cuga_code_sandbox engine:opa]
  17:09:22.200  [policy/cuga]  ALLOWED /usr/bin/python3.12(87) -> POST http://execd-service.sandbox-michal1.svc.cluster.local:44772/command [policy:cuga_code_sandbox engine:l7]

  # LLM plan ready; CUGA registers a kernel context (/code/context) and submits the first code block (/code).
  17:09:22.815  [policy/cuga]  ALLOWED /usr/bin/python3.12(87) -> POST http://execd-service.sandbox-michal1.svc.cluster.local:44772/code/context [policy:cuga_code_sandbox engine:l7]
  17:09:22.944  [policy/cuga]  ALLOWED /usr/bin/python3.12(87) -> POST http://execd-service.sandbox-michal1.svc.cluster.local:44772/code/context [policy:cuga_code_sandbox engine:opa]
  17:09:23.029  [policy/cuga]  ALLOWED /usr/bin/python3.12(87) -> POST http://execd-service.sandbox-michal1.svc.cluster.local:44772/code [policy:cuga_code_sandbox engine:l7]
  17:09:23.387  [policy/cuga]  ALLOWED /usr/bin/python3.12(87) -> POST http://execd-service.sandbox-michal1.svc.cluster.local:44772/command [policy:cuga_code_sandbox engine:opa]

  # execd bootstraps the per-thread venv: uv contacts pypi.org, allowed by binary-path policy.
  --- Policy decisions  [execd sandbox] ---
  17:09:23.884  [policy/execd]  ALLOWED /opt/app-root/bin/uv(36745) -> pypi.org:443 [policy:python_package_index engine:opa]
  17:09:23.983  [policy/execd]  ALLOWED GET http://pypi.org:443/simple/pip/ [policy:python_package_index engine:l7]

  # Rule-set engine confirms the /code request while execd finishes bootstrapping.
  --- Policy decisions  [cuga-demo sandbox] ---
  17:09:24.453  [policy/cuga]  ALLOWED /usr/bin/python3.12(87) -> POST http://execd-service.sandbox-michal1.svc.cluster.local:44772/code [policy:cuga_code_sandbox engine:opa]

  # Venv bootstrap complete; no user output yet.
  --- Code execution    [execd] ---
  17:09:24.455  [execd:code ✓]  duration=1508ms  output_len=0

  # CUGA dispatches the next code block; execd begins step 1 (CRM tool call).
  --- Policy decisions  [cuga-demo sandbox] ---
  17:09:24.530  [policy/cuga]  ALLOWED /usr/bin/python3.12(87) -> POST http://execd-service.sandbox-michal1.svc.cluster.local:44772/code [policy:cuga_code_sandbox engine:l7]
  17:09:24.727  [policy/cuga]  ALLOWED /usr/bin/python3.12(87) -> POST http://execd-service.sandbox-michal1.svc.cluster.local:44772/command [policy:cuga_code_sandbox engine:l7]

  # Step 1: execd calls the CRM tool registry; four paginated requests fetch all contacts.
  --- Policy decisions  [execd sandbox] ---
  17:09:24.786  [policy/execd]  ALLOWED /usr/bin/python3.12(36730) -> POST http://cuga-demo.sandbox-michal1.svc.cluster.local:8001/functions/call [policy:cuga_tool_registry engine:l7]

  --- Tools called      [cuga-demo] ---
  17:09:24.857  [tool]  crm_get_contacts_contacts_get  skip=0  limit=300

  --- Policy decisions  [execd sandbox] ---
  17:09:24.918  [policy/execd]  ALLOWED /usr/bin/python3.12(36730) -> POST http://cuga-demo.sandbox-michal1.svc.cluster.local:8001/functions/call [policy:cuga_tool_registry engine:opa]
  17:09:24.944  [policy/execd]  ALLOWED /usr/bin/python3.12(36730) -> POST http://cuga-demo.sandbox-michal1.svc.cluster.local:8001/functions/call [policy:cuga_tool_registry engine:l7]

  --- Tools called      [cuga-demo] ---
  17:09:24.986  [tool]  crm_get_contacts_contacts_get  skip=300  limit=300

  --- Policy decisions  [execd sandbox] ---
  17:09:25.033  [policy/execd]  ALLOWED /usr/bin/python3.12(36730) -> POST http://cuga-demo.sandbox-michal1.svc.cluster.local:8001/functions/call [policy:cuga_tool_registry engine:opa]
  17:09:25.062  [policy/execd]  ALLOWED /usr/bin/python3.12(36730) -> POST http://cuga-demo.sandbox-michal1.svc.cluster.local:8001/functions/call [policy:cuga_tool_registry engine:l7]

  --- Tools called      [cuga-demo] ---
  17:09:25.094  [tool]  crm_get_contacts_contacts_get  skip=600  limit=300

  --- Policy decisions  [execd sandbox] ---
  17:09:25.134  [policy/execd]  ALLOWED /usr/bin/python3.12(36730) -> POST http://cuga-demo.sandbox-michal1.svc.cluster.local:8001/functions/call [policy:cuga_tool_registry engine:opa]
  17:09:25.160  [policy/execd]  ALLOWED /usr/bin/python3.12(36730) -> POST http://cuga-demo.sandbox-michal1.svc.cluster.local:8001/functions/call [policy:cuga_tool_registry engine:l7]

  --- Tools called      [cuga-demo] ---
  17:09:25.199  [tool]  crm_get_contacts_contacts_get  skip=900  limit=300

  # Step 2: uv installs tomli; only the uv binary is allowed to reach pypi.org.
  --- Policy decisions  [execd sandbox] ---
  17:09:25.227  [policy/execd]  ALLOWED /usr/bin/python3.12(36730) -> POST http://cuga-demo.sandbox-michal1.svc.cluster.local:8001/functions/call [policy:cuga_tool_registry engine:opa]
  17:09:25.318  [policy/execd]  ALLOWED /opt/app-root/bin/uv(36763) -> pypi.org:443 [policy:python_package_index engine:opa]
  17:09:25.394  [policy/execd]  ALLOWED GET http://pypi.org:443/simple/tomli/ [policy:python_package_index engine:l7]

  # CUGA heartbeats and LLM calls continue while execd finishes all three steps.
  --- Policy decisions  [cuga-demo sandbox] ---
  17:09:25.934  [policy/cuga]  ALLOWED /usr/bin/python3.12(87) -> POST http://execd-service.sandbox-michal1.svc.cluster.local:44772/command [policy:cuga_code_sandbox engine:opa]
  17:09:27.230  [policy/cuga]  ALLOWED /usr/bin/python3.12(87) -> POST http://execd-service.sandbox-michal1.svc.cluster.local:44772/command [policy:cuga_code_sandbox engine:l7]
  17:09:28.375  [policy/cuga]  ALLOWED /usr/bin/python3.12(87) -> POST http://execd-service.sandbox-michal1.svc.cluster.local:44772/command [policy:cuga_code_sandbox engine:opa]
  17:09:29.707  [policy/cuga]  ALLOWED /usr/bin/python3.12(87) -> POST http://execd-service.sandbox-michal1.svc.cluster.local:44772/command [policy:cuga_code_sandbox engine:l7]
  17:09:30.628  [policy/cuga]  ALLOWED /usr/bin/python3.12(87) -> POST http://execd-service.sandbox-michal1.svc.cluster.local:44772/code [policy:cuga_code_sandbox engine:opa]
  17:09:30.687  [policy/cuga]  ALLOWED inference.local:443
  17:09:30.867  [policy/cuga]  ALLOWED /usr/bin/python3.12(87) -> POST http://execd-service.sandbox-michal1.svc.cluster.local:44772/command [policy:cuga_code_sandbox engine:opa]
  17:09:32.207  [policy/cuga]  ALLOWED /usr/bin/python3.12(87) -> POST http://execd-service.sandbox-michal1.svc.cluster.local:44772/command [policy:cuga_code_sandbox engine:l7]
  17:09:33.423  [policy/cuga]  ALLOWED /usr/bin/python3.12(87) -> POST http://execd-service.sandbox-michal1.svc.cluster.local:44772/command [policy:cuga_code_sandbox engine:opa]
  17:09:34.720  [policy/cuga]  ALLOWED /usr/bin/python3.12(87) -> POST http://execd-service.sandbox-michal1.svc.cluster.local:44772/command [policy:cuga_code_sandbox engine:l7]
  17:09:35.915  [policy/cuga]  ALLOWED /usr/bin/python3.12(87) -> POST http://execd-service.sandbox-michal1.svc.cluster.local:44772/command [policy:cuga_code_sandbox engine:opa]
  17:09:36.313  [policy/cuga]  ALLOWED /usr/bin/python3.12(87) -> POST http://execd-service.sandbox-michal1.svc.cluster.local:44772/code [policy:cuga_code_sandbox engine:l7]
  17:09:37.214  [policy/cuga]  ALLOWED /usr/bin/python3.12(87) -> POST http://execd-service.sandbox-michal1.svc.cluster.local:44772/command [policy:cuga_code_sandbox engine:l7]
  17:09:38.358  [policy/cuga]  ALLOWED /usr/bin/python3.12(87) -> POST http://execd-service.sandbox-michal1.svc.cluster.local:44772/command [policy:cuga_code_sandbox engine:opa]
  17:09:39.713  [policy/cuga]  ALLOWED /usr/bin/python3.12(87) -> POST http://execd-service.sandbox-michal1.svc.cluster.local:44772/command [policy:cuga_code_sandbox engine:l7]
  17:09:40.874  [policy/cuga]  ALLOWED /usr/bin/python3.12(87) -> POST http://execd-service.sandbox-michal1.svc.cluster.local:44772/command [policy:cuga_code_sandbox engine:opa]
  17:09:41.648  [policy/cuga]  ALLOWED /usr/bin/python3.12(87) -> POST http://execd-service.sandbox-michal1.svc.cluster.local:44772/code [policy:cuga_code_sandbox engine:opa]
  17:09:41.698  [policy/cuga]  ALLOWED inference.local:443
  17:09:42.203  [policy/cuga]  ALLOWED /usr/bin/python3.12(87) -> POST http://execd-service.sandbox-michal1.svc.cluster.local:44772/command [policy:cuga_code_sandbox engine:l7]
  17:09:43.341  [policy/cuga]  ALLOWED /usr/bin/python3.12(87) -> POST http://execd-service.sandbox-michal1.svc.cluster.local:44772/command [policy:cuga_code_sandbox engine:opa]
  17:09:44.717  [policy/cuga]  ALLOWED /usr/bin/python3.12(87) -> POST http://execd-service.sandbox-michal1.svc.cluster.local:44772/command [policy:cuga_code_sandbox engine:l7]
  17:09:45.846  [policy/cuga]  ALLOWED /usr/bin/python3.12(87) -> POST http://execd-service.sandbox-michal1.svc.cluster.local:44772/command [policy:cuga_code_sandbox engine:opa]
  17:09:47.210  [policy/cuga]  ALLOWED /usr/bin/python3.12(87) -> POST http://execd-service.sandbox-michal1.svc.cluster.local:44772/command [policy:cuga_code_sandbox engine:l7]
  17:09:48.355  [policy/cuga]  ALLOWED /usr/bin/python3.12(87) -> POST http://execd-service.sandbox-michal1.svc.cluster.local:44772/command [policy:cuga_code_sandbox engine:opa]
  17:09:49.703  [policy/cuga]  ALLOWED /usr/bin/python3.12(87) -> POST http://execd-service.sandbox-michal1.svc.cluster.local:44772/command [policy:cuga_code_sandbox engine:l7]
  17:09:51.121  [policy/cuga]  ALLOWED /usr/bin/python3.12(87) -> POST http://execd-service.sandbox-michal1.svc.cluster.local:44772/command [policy:cuga_code_sandbox engine:opa]
  17:09:52.213  [policy/cuga]  ALLOWED /usr/bin/python3.12(87) -> POST http://execd-service.sandbox-michal1.svc.cluster.local:44772/command [policy:cuga_code_sandbox engine:l7]
  17:09:53.355  [policy/cuga]  ALLOWED /usr/bin/python3.12(87) -> POST http://execd-service.sandbox-michal1.svc.cluster.local:44772/command [policy:cuga_code_sandbox engine:opa]
  17:09:54.726  [policy/cuga]  ALLOWED /usr/bin/python3.12(87) -> POST http://execd-service.sandbox-michal1.svc.cluster.local:44772/command [policy:cuga_code_sandbox engine:l7]
  17:09:55.845  [policy/cuga]  ALLOWED /usr/bin/python3.12(87) -> POST http://execd-service.sandbox-michal1.svc.cluster.local:44772/command [policy:cuga_code_sandbox engine:opa]
  17:09:57.198  [policy/cuga]  ALLOWED /usr/bin/python3.12(87) -> POST http://execd-service.sandbox-michal1.svc.cluster.local:44772/command [policy:cuga_code_sandbox engine:l7]
  17:09:58.328  [policy/cuga]  ALLOWED /usr/bin/python3.12(87) -> POST http://execd-service.sandbox-michal1.svc.cluster.local:44772/command [policy:cuga_code_sandbox engine:opa]
  17:09:59.717  [policy/cuga]  ALLOWED /usr/bin/python3.12(87) -> POST http://execd-service.sandbox-michal1.svc.cluster.local:44772/command [policy:cuga_code_sandbox engine:l7]
  17:10:00.865  [policy/cuga]  ALLOWED /usr/bin/python3.12(87) -> POST http://execd-service.sandbox-michal1.svc.cluster.local:44772/command [policy:cuga_code_sandbox engine:opa]
  17:10:02.223  [policy/cuga]  ALLOWED /usr/bin/python3.12(87) -> POST http://execd-service.sandbox-michal1.svc.cluster.local:44772/command [policy:cuga_code_sandbox engine:l7]
  17:10:03.341  [policy/cuga]  ALLOWED /usr/bin/python3.12(87) -> POST http://execd-service.sandbox-michal1.svc.cluster.local:44772/command [policy:cuga_code_sandbox engine:opa]
  17:10:04.716  [policy/cuga]  ALLOWED /usr/bin/python3.12(87) -> POST http://execd-service.sandbox-michal1.svc.cluster.local:44772/command [policy:cuga_code_sandbox engine:l7]
  17:10:05.845  [policy/cuga]  ALLOWED /usr/bin/python3.12(87) -> POST http://execd-service.sandbox-michal1.svc.cluster.local:44772/command [policy:cuga_code_sandbox engine:opa]
  17:10:05.900  [policy/cuga]  ALLOWED inference.local:443
  17:10:07.223  [policy/cuga]  ALLOWED /usr/bin/python3.12(87) -> POST http://execd-service.sandbox-michal1.svc.cluster.local:44772/command [policy:cuga_code_sandbox engine:l7]
  17:10:08.355  [policy/cuga]  ALLOWED /usr/bin/python3.12(87) -> POST http://execd-service.sandbox-michal1.svc.cluster.local:44772/command [policy:cuga_code_sandbox engine:opa]
  17:10:09.707  [policy/cuga]  ALLOWED /usr/bin/python3.12(87) -> POST http://execd-service.sandbox-michal1.svc.cluster.local:44772/command [policy:cuga_code_sandbox engine:l7]
  17:10:10.842  [policy/cuga]  ALLOWED /usr/bin/python3.12(87) -> POST http://execd-service.sandbox-michal1.svc.cluster.local:44772/command [policy:cuga_code_sandbox engine:opa]
  17:10:12.214  [policy/cuga]  ALLOWED /usr/bin/python3.12(87) -> POST http://execd-service.sandbox-michal1.svc.cluster.local:44772/command [policy:cuga_code_sandbox engine:l7]
  17:10:13.349  [policy/cuga]  ALLOWED /usr/bin/python3.12(87) -> POST http://execd-service.sandbox-michal1.svc.cluster.local:44772/command [policy:cuga_code_sandbox engine:opa]
  17:10:14.721  [policy/cuga]  ALLOWED /usr/bin/python3.12(87) -> POST http://execd-service.sandbox-michal1.svc.cluster.local:44772/command [policy:cuga_code_sandbox engine:l7]
  17:10:15.865  [policy/cuga]  ALLOWED /usr/bin/python3.12(87) -> POST http://execd-service.sandbox-michal1.svc.cluster.local:44772/command [policy:cuga_code_sandbox engine:opa]
  17:10:17.203  [policy/cuga]  ALLOWED /usr/bin/python3.12(87) -> POST http://execd-service.sandbox-michal1.svc.cluster.local:44772/command [policy:cuga_code_sandbox engine:l7]
  17:10:18.339  [policy/cuga]  ALLOWED /usr/bin/python3.12(87) -> POST http://execd-service.sandbox-michal1.svc.cluster.local:44772/command [policy:cuga_code_sandbox engine:opa]

# All three output files are present in the per-thread workspace directory.
==> Workspace  [openshell--code-exec:/workspace/smoke-1788282553]
  /workspace/smoke-1788282553
  /workspace/smoke-1788282553/.venv
  /workspace/smoke-1788282553/config.toml
  /workspace/smoke-1788282553/contacts_export.txt

File contents (<=2 KB):
  --- /workspace/smoke-1788282553/config.toml ---
  version = "1.0"
  --- /workspace/smoke-1788282553/contacts_export.txt ---  (13683 bytes - showing first 5 lines)
  John Smith
  Jane Johnson
  Michael Williams
  Sarah Brown
  David Jones

# Assert on actual file contents, not model output; all three pass.
==> Assertions
  ok  Python code → file write  (/workspace/smoke-1788282553/contacts_export.txt exists)
  ok  pip install + Python → config.toml  (/workspace/smoke-1788282553/config.toml contains 'version')
  ok  run_command (shell) → contacts_export.txt marker  (/workspace/smoke-1788282553/contacts_export.txt contains 'smoke-test ok')

  ok  Done.  (log saved to /Users/michal/git/SIL/cuga-openshell/sandbox/logs/smoke-20260901-190905.log)
```

The install is attributed to `/opt/app-root/bin/uv` by path and PID, allowed by
the named `python_package_index` policy. Remove that policy and the same line
comes back as `DENIED` - the install fails, the rest of the run does not.

On Rancher the same trace appears with `host.openshell.internal` in place of the
Service DNS names, and policy decisions read from `docker logs` rather than from
the gateway.

---

## 13. BYOA client

Any agent that can make HTTP calls can use execd directly, without CUGA:

```python
import httpx

EXECD_URL = "http://execd-service.<ns>.svc.cluster.local:44772"
API_KEY   = "<SANDBOX_API_KEY>"

# 1. Create a kernel context (persistent Python process)
r = httpx.post(f"{EXECD_URL}/code/context",
               json={"language": "python"},
               headers={"X-API-Key": API_KEY})
context_id = r.json()["id"]

# 2. Execute code - streaming NDJSON response
with httpx.stream("POST", f"{EXECD_URL}/code",
                  json={"context": {"id": context_id, "language": "python"},
                        "code": "print(1+1)"},
                  headers={"X-API-Key": API_KEY}) as resp:
    for line in resp.iter_lines():
        print(line)
```

Connection details (`EXECD_URL`, `SANDBOX_API_KEY`) come from `GET /info` on
`sandbox-api`:

```bash
curl -H "X-API-Key: $SANDBOX_API_KEY" https://sandbox-api-<ns>.apps.<cluster>/info
```

### Open questions - tool calling for external agents

CUGA injects its tools into the execd kernel by serialising async callables into
the `POST /code/context` payload (`context_locals` dict). A LangGraph or LangFlow
agent cannot do this. Options:

| Option | How it works | Open questions |
|---|---|---|
| **A. Agent-side HTTP proxy** | External agent stands up its own tool HTTP endpoint; injects URL into kernel context so generated code calls `httpx.post(TOOL_URL, ...)` | Who authenticates the callback? No standard schema today. |
| **B. CUGA tool registry as shared service** | CUGA's `POST /functions/call` stays running; BYOA agent points its kernel at the same registry URL | Tight coupling to CUGA registry format; unclear for agent-specific tools. |
| **C. MCP server inside the sandbox** | execd starts an MCP server process; agent registers tools there; generated code calls them via MCP | Not designed or tested; adds sidecar process; policy implications for intra-pod traffic unclear. |
| **D. Code-only, no tool calling** | Agent generates pure Python; all I/O via files, HTTP to known endpoints, or stdlib | Significantly limits capability; only works if LLM can generate self-contained code reliably. |

**Current state:** option B is implicitly what the BYOA example above does if the
agent reuses the CUGA deployment - but it is undocumented, untested, and depends
on the CUGA sandbox remaining reachable from the execd sandbox. Options A and C
have not been prototyped. The tool-calling gap is the main blocker for a complete
BYOA validation.

### Multi-agent tool routing - required work

The current implementation assumes a single CUGA agent with a single tool
registry. Two gaps must be addressed before multiple agents (or multiple BYOA
agents) can share one execd pod:

#### Gap 1 - `function_call_url` is global, not per-agent

[`CallApiHelper.get_function_call_url()`](../../src/cuga/backend/cuga_graph/nodes/cuga_lite/executors/common/call_api_helper.py)
reads the tool registry URL from global `settings` and bakes it into the
`call_api` helper code that is injected into every execd kernel. All kernels
therefore call back to the same registry regardless of which agent owns the
thread.

**Fix:** `execute_for_cuga_lite` already receives `state: AgentState`. The
registry URL should be carried per-agent (e.g. in `AgentState` or agent
settings) and passed explicitly to `create_remote_call_api_code` instead of
reading from global settings. This is a one-line change at the call site; the
injection mechanism in `create_remote_call_api_code` already accepts an
arbitrary URL.

#### Gap 2 - `execd-policy.yaml` hardcodes a single registry address

```yaml
cuga_tool_registry:  # cuga-demo.<ns>.svc.cluster.local:8001
```

With multiple agents each has its own tool registry Service at a different
address. The OpenShell egress policy must cover all of them. Two options:

| Option | How | Trade-off |
|---|---|---|
| **Per-agent execd pod** | Each agent gets its own execd pod with its own `execd-policy.yaml` listing only its registry address | Clean isolation; N pods for N agents; aligns with catalog-per-agent provisioning |
| **Shared execd pod, namespace-wide policy** | Single policy allows `*.<ns>.svc.cluster.local:8001`; all agents share one pod | Fewer pods; execd can call back to any agent in the namespace - looser boundary |

The per-agent pod option aligns better with the catalog-provisioned model
(each sandbox is a catalog entry per tenant/agent) and avoids cross-agent
callback risk. The shared pod option is cheaper and sufficient when all agents
in the namespace are trusted (single-tenant deployment).

**Current state:** neither option is implemented. Until this is resolved,
multi-agent deployments must use separate namespaces or accept that all threads
call back to the same registry.

---

## 12. Deployment - Rancher Desktop (macOS)

### Prerequisites

```bash
openshell --version   # OpenShell CLI installed
docker info           # Rancher Desktop running
```

### Deploy

```bash
cd sandbox
make rancher-up \
  OPENAI_API_KEY="sk-..." \
  OPENAI_BASE_URL="https://your-llm-endpoint" \
  MODEL_NAME="your-model-id"
```

`rancher-up` runs two steps in sequence:

**Step 1 - sandbox stack** (`_rancher-sandbox-up`):
1. Generates `SANDBOX_API_KEY` (`openssl rand -hex 32`) → `/var/lib/openshell/sandbox-api-key` (idempotent).
2. Starts `docker compose up -d` (gateway + relay + sandbox-api) from `sandbox/deploy/rancher/docker-compose.yml`.
3. Registers gateway: `openshell gateway add http://localhost:8080 --name openshell-docker`.
4. Creates workspace: `openshell workspace create --name openshell`.
5. Waits up to 60 s for gateway to become healthy (`openshell status`).
6. Builds execd image from a temp context (`Dockerfile.execd` + `execd-entrypoint.sh` + `execd-isolation.toml`).
7. Creates execd sandbox: `openshell sandbox create --name code-exec --policy execd-policy.yaml --detach`.
8. Forwards execd port: `openshell forward start 44772 code-exec --background`.

**Step 2 - CUGA** (`_rancher-cuga-up`):
1. Builds `cuga-openshell:local` Docker image from `cuga/Dockerfile.openshell` with the full repo as build context.
2. Creates/updates OpenAI provider: `openshell provider create --name cuga-openai --type openai`.
3. Sets inference route: `openshell inference set --provider cuga-openai --model <MODEL_NAME> --timeout 300`.
4. Creates CUGA sandbox: `openshell sandbox create --name cuga-demo --policy cuga-policy.yaml --detach`.
5. Forwards UI and registry ports: `openshell forward start 7860 cuga-demo`, `openshell forward start 8001 cuga-demo`.

### Access

```
http://localhost:7860   # Gradio UI
http://localhost:44772  # execd API (direct)
http://localhost:8090   # sandbox-api management
```

### Smoke test

```bash
make smoke-test
```

### Tear down

```bash
make rancher-down
```

`rancher-down` in order:
1. `openshell forward stop 7860/8001 cuga-demo` + `openshell sandbox delete cuga-demo`
2. `openshell forward stop 44772 code-exec` + `openshell sandbox delete code-exec`
3. `docker compose down` (gateway + relay + sandbox-api)

PVC state (`/var/lib/openshell/`) is preserved - the API key and JWT keypair survive `rancher-down`. Run again with the same credentials to resume without regenerating keys.

---

## 13. Deployment - OpenShift (remote amd64 cluster)

### Prerequisites

```bash
oc version               # oc CLI >= 4.12
openshell --version      # OpenShell CLI installed
docker info              # local Docker running (builds CUGA image locally)

# Check Agent Sandbox CRD (required once per cluster, cluster-admin)
oc get crd sandboxes.agents.x-k8s.io || \
  oc apply -f https://github.com/kubernetes-sigs/agent-sandbox/releases/download/v1.0.0/sandbox.yaml
```

### Log in

```bash
oc login https://api.your-cluster.example.com:6443
oc whoami --show-server   # confirm target cluster

# ICR image registry (one-time per workstation)
echo "$ICR_API_KEY" | docker login -u iamapikey --password-stdin icr.io
```

### Deploy

```bash
cd sandbox
make ocp-deploy \
  NAMESPACE="sandbox-<yourname>" \
  OPENAI_API_KEY="sk-..." \
  OPENAI_BASE_URL="https://your-llm-endpoint" \
  MODEL_NAME="your-model-id" \
  REGISTRY="icr.io/automation-saas-platform-dev" \
  ICR_API_KEY="<your-icr-api-key>"
```

`NAMESPACE` defaults to `sandbox-$(whoami)`. `ocp-deploy` runs two steps in sequence:

**Step 1 - sandbox stack** (`_ocp-deploy-sandbox`):
1. Creates namespace (idempotent): `oc create namespace <ns> --dry-run=client | oc apply`.
2. Grants SCCs: `anyuid` to `openshell-gateway` SA; `privileged` + `anyuid` to `default` SA.
3. Creates secrets (idempotent):
   - `llm-credentials` - `OPENAI_API_KEY` (skipped if already present)
   - `sandbox-credentials` - generated `SANDBOX_API_KEY` (printed once; store securely)
   - `icr-pull-secret` - docker-registry secret from `ICR_API_KEY`
4. Mirrors Docker Hub base image to ICR: `opensandbox/execd` → `icr.io/<ns>/opensandbox-execd:latest` (skipped if present).
5. In-cluster build of `sandbox-api` image: `oc start-build sandbox-api --from-dir=sandbox --follow`.
6. In-cluster build of `openshell-execd` image: `oc start-build openshell-execd --from-dir=<tmpdir> --follow` (patches `ARG EXECD_IMAGE` in `Dockerfile.execd.ocp` before build).
7. Renders kustomize manifests (`envsubst` substitutes `$NAMESPACE`, `$OPENSHELL_WORKSPACE`), applies: `oc apply -k` (gateway Deployment + Service + Route, sandbox-api Deployment, sandbox-proxy Deployment + Services + ConfigMap, PVC, RBAC).
8. Restarts and waits for gateway rollout: `oc rollout restart/status deployment/openshell-gateway`.
9. Registers gateway and creates execd sandbox (`_ocp-register-gateway`):
   - `oc port-forward svc/openshell-gateway 18080:8080` (temp, killed after)
   - `openshell gateway add http://127.0.0.1:18080 --name openshell-ocp`
   - `openshell workspace create --name openshell`
   - `openshell sandbox create --name code-exec --from <execd-imagestream> --policy execd-policy.yaml --detach`
   - `openshell service expose code-exec 44772 execd`

**Step 2 - CUGA** (`_ocp-deploy-cuga`):
1. Builds CUGA image locally for `linux/amd64`: `docker build --platform linux/amd64 -f cuga/Dockerfile.openshell`.
2. Pushes to registry: `docker push icr.io/<ns>/cuga-openshell:latest`.
3. Applies CUGA kustomize: RBAC (`cuga-demo` SA) + `cuga-demo` Service + Route.
4. Links ICR pull secret to `cuga-demo` SA: `oc secrets link cuga-demo icr-pull-secret --for=pull`.
5. Registers inference provider and creates CUGA sandbox (`_ocp-register-cuga`):
   - `oc port-forward svc/openshell-gateway 18080:8080` (temp, killed after)
   - `openshell provider create --name cuga-openai --type openai`
   - `openshell inference set --provider cuga-openai --model <MODEL_NAME> --timeout 300`
   - `openshell sandbox create --name cuga-demo --from <CUGA_IMAGE> --provider cuga-openai --policy cuga-policy.yaml --env CUGA_EXECD_URL=... --detach`
   - `openshell service expose cuga-demo 7860 ui`
   - `openshell service expose cuga-demo 8001 registry`
6. Waits up to 5 min for `openshell--cuga-demo` pod to become Ready (polls every 5 s).
7. Annotates Route: `haproxy.router.openshift.io/timeout=300s` (prevents SSE stream cutoff).

### Access

```bash
echo "https://$(oc get route cuga-demo -n $NAMESPACE -o jsonpath='{.spec.host}')"
```

The Route is TLS-edge. On a self-signed cluster `curl` needs `-k`. Full path:

```
Route → cuga-demo Service → sandbox-proxy → openshell-gateway → cuga-demo sandbox :7860
```

### Deploy only sandbox stack or only CUGA

```bash
# Sandbox stack only (gateway + execd + sandbox-api + sandbox-proxy)
make _ocp-deploy-sandbox NAMESPACE="sandbox-<yourname>" \
  OPENAI_API_KEY=... OPENAI_BASE_URL=... MODEL_NAME=... \
  REGISTRY=... ICR_API_KEY=...

# CUGA only (re-deploy after image change, sandbox stack already running)
make _ocp-deploy-cuga NAMESPACE="sandbox-<yourname>" \
  OPENAI_API_KEY=... OPENAI_BASE_URL=... MODEL_NAME=... \
  REGISTRY=... ICR_API_KEY=...
```

### Smoke test

```bash
make smoke-test NAMESPACE="sandbox-<yourname>"
# or explicitly:
./smoke-test.sh --platform=openshift --namespace=$NAMESPACE
```

### Tear down - keep PVC and namespace

```bash
make ocp-teardown NAMESPACE=<namespace>
```

`ocp-teardown` in order:
1. Deletes sandboxes via gateway port-forward (`_ocp-delete-sandboxes`):
   - `oc port-forward svc/openshell-gateway 18080:8080` (temp)
   - `openshell sandbox delete cuga-demo`
   - `openshell sandbox delete code-exec`
2. Deletes CUGA kustomize resources: `oc delete -k` (Service, Route, RBAC, ConfigMap).
3. Deletes sandbox stack kustomize resources: `oc delete -k` (gateway Deployment/Service/Route, sandbox-api, sandbox-proxy, RBAC).
4. Deletes cluster-scoped resources explicitly (survive namespace delete):
   - `oc delete clusterrole openshell-gateway-tokenreview-<ns>`
   - `oc delete clusterrolebinding openshell-gateway-tokenreview-<ns>`

PVC `openshell-state` (JWT keypair + `gateway.db`) and the namespace itself are preserved.

### Tear down - full wipe

```bash
make ocp-wipe NAMESPACE=<namespace>
```

`ocp-wipe`:
1. `oc delete namespace <namespace>` - removes everything inside the namespace including PVC.
2. Deletes cluster-scoped ClusterRole/ClusterRoleBinding.
3. `openshell gateway remove openshell-ocp` - deregisters gateway from local CLI.

> **Note:** `ocp-wipe` does **not** remove the Agent Sandbox CRD or its controller (`agent-sandbox-system` namespace) - those are cluster-wide and shared across namespaces.

### Troubleshooting

```bash
# Pod status
oc get pods -n $NAMESPACE

# Gateway logs (policy ALLOWED/DENIED decisions)
oc logs deploy/openshell-gateway -n $NAMESPACE

# CUGA sandbox logs
oc logs openshell--cuga-demo -n $NAMESPACE

# execd sandbox logs
oc logs openshell--code-exec -n $NAMESPACE

# sandbox-proxy logs (Host-header rewrite)
oc logs deploy/sandbox-proxy -n $NAMESPACE

# Check services are routed correctly
oc get endpoints cuga-demo -n $NAMESPACE
openshell service list   # requires port-forward to gateway active

# 503 on Route - check sandbox-proxy → gateway → sandbox chain
oc logs deploy/sandbox-proxy -n $NAMESPACE
openshell service list
```
