# How it works - OpenShell and OpenSandbox explained

This document explains the two upstream projects used in this stack and how they
fit together. It is aimed at someone who understands Kubernetes but has not worked
with either project before.

---

## OpenShell

### What it is

OpenShell is a **policy-enforcement runtime for autonomous agents**. Its job is not
to run code - it is to constrain what a running process is allowed to do. Think of
it as a mandatory access control layer that wraps an existing workload without
requiring any changes to the workload itself.

It is written primarily in Rust (~89% of the codebase) and is maintained by NVIDIA.
Watson Orchestrate and Red Hat are known internal IBM / partner users.

### What it is not

OpenShell is not a code interpreter, not a sandbox for running generated Python, and
not a container runtime. It does not replace Docker or Kubernetes - it runs on top of
them and adds a policy layer that neither provides.

### How it works - the supervisor model

OpenShell works by replacing `PID 1` inside a container with its own **supervisor**
process. The supervisor:

1. Reads a declarative policy file (`*-policy.yaml`) at startup
2. Creates a **private network namespace** for the workload - the workload has no
   default route and cannot reach anything not explicitly listed in the policy
3. Starts the actual workload as a child process inside that netns
4. Intercepts every outbound TCP/HTTP connection using a transparent proxy inside the
   netns and checks it against the policy before forwarding
5. Emits a structured OCSF event (`ALLOWED` / `DENIED`) for every decision, including
   the binary path and PID of the process that made the connection

The workload process never knows it is wrapped. From its perspective it has normal
networking - `socket()`, `connect()` work as expected - but anything not on the
allowlist is silently dropped at the netns proxy level.

### The policy file

```yaml
network_policies:
  cuga_code_sandbox:
    name: cuga-code-sandbox
    endpoints:
      - host: execd-service.${NAMESPACE}.svc.cluster.local
        port: 44772
        protocol: rest
        enforcement: enforce
        access: full
    binaries:
      - { path: /app/.venv/bin/python* }
```

Each named policy block defines:
- **endpoints** - which hosts and ports are reachable
- **binaries** - which executable paths may make those connections

A connection is allowed only when **both** match: the right binary calling the right
host. `/app/.venv/bin/python` can call `execd-service:44772`; `/bin/bash` cannot,
even though bash is on the same policy file.

### What OpenShell does NOT do

- It does not isolate filesystem access by default - that is Landlock (a separate
  Linux kernel feature also configured in the policy file under `filesystem_policy`)
- It does not provide process isolation between workloads - one sandbox = one network
  namespace, but the filesystem is the host container filesystem unless Landlock says
  otherwise
- It does not manage container images or pod scheduling - that is Kubernetes / Docker

### How OpenShell creates sandbox pods

On **Rancher Desktop** (Docker driver):

```
openshell sandbox create \
  --workspace cuga --name cuga-demo \
  --image <image> -- <cmd>
```

This calls the Docker API to create a container with a special entrypoint that is the
OpenShell supervisor binary. The supervisor reads the policy ConfigMap mounted into
the container and starts the workload.

On **OpenShift** (Kubernetes driver):

The gateway uses the Kubernetes API (via its `ServiceAccount`) to create a Pod. The
pod spec sets the container command to the OpenShell supervisor. The gateway mounts
the policy ConfigMap as a volume. The supervisor starts, reads the policy, creates the
private netns, and starts the workload inside it.

In both cases the sandbox pod is **not a regular Kubernetes Pod** you applied with
`kubectl apply` - it is created and owned by the gateway. The gateway tracks its
lifecycle in a SQLite (or Postgres) database and is the only way to reach it.

### How traffic reaches the sandbox

This is the non-obvious part. The workload runs inside a **network namespace nested
inside the pod**. The pod's `eth0` (the Kubernetes pod IP) belongs to the outer
namespace - the supervisor. The workload's IP is typically `10.200.0.2` on a private
veth pair inside the pod.

This means:
- `curl http://<pod-ip>:7860` from outside the pod reaches the supervisor, not the
  workload
- The supervisor must relay the traffic in - which it does for ports registered via
  `openshell service expose`

On **OpenShift**, a `Service` pointing at the pod IP reaches the supervisor's relay,
which forwards to the workload's netns. But Kubernetes Services use the pod's
hostname, not a Host header - so the supervisor cannot tell which sandbox to route to
from the IP alone. This is solved by `sandbox-proxy` (nginx) which rewrites the HTTP
`Host` header to `<workspace>--<sandbox>--<service>.openshell.localhost` before
forwarding to the gateway's gRPC proxy port. The gateway uses that Host header to
look up the sandbox in its database and route the connection into the right netns.

On **Rancher Desktop**, `openshell forward` runs on the macOS host and creates a
direct tunnel to the sandbox netns via the gateway's gRPC relay - no nginx needed.

### The gateway

The gateway is a separate long-running process (a Deployment, not a sidecar). It has
two jobs:

1. **Lifecycle control** - creates, destroys, and restarts sandbox pods via the
   compute driver (Docker or Kubernetes)
2. **Traffic relay** - acts as the ingress point for all inbound traffic to sandboxes
   (via the Host-header-based routing described above) and as the egress point for LLM
   calls (where it injects the real API key)

The gateway holds a SQLite database (`gateway.db` on the PVC) with the mapping of
workspace → sandbox → pod name → port. This is why the gateway must be running for
any sandbox traffic to flow.

---

## OpenSandbox (execd only)

### What it is

OpenSandbox is an **open-source (Apache 2.0) self-hosted code interpreter** from
Cohere. Its full form is a server that creates isolated execution environments as
Docker containers or Kubernetes pods. We use **only `execd`** - the execution daemon
that runs inside those environments.

The full OpenSandbox server is not used because it would try to create containers
inside the sandbox pod (Docker-in-Docker / Kubernetes-in-Kubernetes), which is not
what we want. OpenShell already manages pod lifecycle. execd is the piece that
actually runs the code.

### What execd does

execd is an HTTP server that exposes a **Jupyter kernel protocol over REST**:

| Endpoint | What it does |
|---|---|
| `POST /code/context` | Creates a new Jupyter kernel (a persistent Python process). Returns a `context_id`. |
| `POST /code` | Executes a code block in an existing kernel. Streams output as NDJSON. |
| `POST /command` | Runs a shell command. Used for `run_command()` and `uv pip install`. |

A kernel is a **persistent Python process**. Variable state, imported modules, and
installed packages (via `sys.path`) persist across calls within the same context.
This is what allows the agent to build up state across multiple code blocks in one
conversation.

### How execd uses Jupyter

execd does not implement the kernel protocol itself. It delegates to a **Jupyter
notebook server** running on loopback (`127.0.0.1:54321`). The entrypoint starts
Jupyter first, waits for it to be ready, then starts execd pointing at it:

```bash
jupyter notebook --ip=127.0.0.1 --port=54321 ...
execd --jupyter-host=http://127.0.0.1:54321 --port=44772 ...
```

Jupyter manages the kernel processes; execd is the HTTP façade that CUGA talks to.

### Isolation inside execd - bubblewrap sessions

For `POST /command` (shell commands), execd optionally wraps each execution in a
**bubblewrap** (`bwrap`) namespace - a lightweight Linux user+mount namespace that
gives each command its own PID and filesystem view without requiring root or a
container runtime.

In our deployment this hardening is **disabled** (`[hardening] enabled = false` in
`execd-isolation.toml`). The reason is a conflict with OpenShell: the launcher that
applies the inner hardening floor uses `memfd_create`, which OpenShell's seccomp
policy denies. Since OpenShell itself provides the outer confinement (private netns,
Landlock, unprivileged user), the inner bubblewrap floor is redundant anyway. If the
OpenShell policy is ever relaxed to permit `memfd_create`, the hardening can be
re-enabled by setting `enabled = true`.

### What is NOT OpenSandbox in our stack

CUGA has an `OpenSandboxExecutor` class which talks to a different service - the
full OpenSandbox server (not execd). That executor is for the original
`opensandbox_sandbox` mode which pre-dates the OpenShell integration. It is not used
in the sandbox deployment described in this repo. The execd integration uses
`ExecdExecutor`.

---

## How the two projects fit together

```
OpenShell                          OpenSandbox (execd only)
─────────────────────────────      ─────────────────────────────
Wraps the workload process         IS the workload process
Enforces network policy            Executes generated code
Manages pod lifecycle              Manages kernel lifecycle
Routes inbound traffic             Exposes HTTP API for code
Injects LLM key on egress         Streams execution output
```

OpenShell is the **cage**. OpenSandbox execd is the **engine inside the cage**.

Neither knows about the other at the binary level - execd is just a process that
listens on port 44772, and OpenShell is just a supervisor that wraps whatever process
it is given. The integration is purely at the deployment level: OpenShell creates the
pod, mounts the policy, and starts execd as the workload.

---

## What "wrapper" means here - is CUGA wrapped?

Yes, in both cases:

**CUGA pod** - the OpenShell supervisor is PID 1. `cuga start demo_crm` is its child.
CUGA's Python process has no idea it is running inside a constrained netns. It calls
`inference.local:443` as if it were a normal hostname - the supervisor intercepts,
checks the policy, injects the API key, and forwards. From CUGA's perspective it just
works.

**execd pod** - same model. The OpenShell supervisor is PID 1. execd is its child.
Generated code running inside a Jupyter kernel calls `cuga-demo:8001/functions/call`
for tool results - the supervisor checks the `cuga_tool_registry` policy entry and
forwards if allowed.

Neither CUGA nor execd have any OpenShell SDK, import, or dependency. Wrapping is
done entirely at the OS/netns level by the supervisor binary.
