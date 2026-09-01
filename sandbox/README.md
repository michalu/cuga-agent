# Sandboxing

CUGA executes generated Python code in an isolated two-boundary sandbox.
The agent process (Role A) and the code execution engine (Role B) run in
separate pods with private network namespaces enforced by
[OpenShell](https://github.com/NVIDIA/OpenShell) and
[opensandbox/execd](https://github.com/cohere-ai/opensandbox).

| Directory | Contents |
|---|---|
| [`cuga/`](cuga/) | CUGA image, confinement policy, entrypoint |
| [`sandbox/`](sandbox/) | OpenShell control plane, execd sandbox, management API, Host-rewrite proxy |

> **`./sandbox`** is a candidate for its own repo once a non-CUGA team needs to
> deploy it independently.

---

## Documentation

| Document | Contents |
|---|---|
| [docs/HLD.md](docs/HLD.md) | Architecture, two-boundary security model, components, deployment targets, request flow, architectural decisions (AD-1–AD-7), security caveats, full implementation status (D1–D22) |
| [docs/LLD.md](docs/LLD.md) | Repository layout, component internals (gateway, sandbox-proxy, CUGA sandbox, execd, sandbox-api, relay), configuration, secrets, topology, smoke test + observed trace, BYOA client, deployment procedures (Rancher + OpenShift), air-gapped operation |
| [Makefile](Makefile) | Run `make help` for all deploy / teardown / smoke-test targets |

---

## Quick reference

```bash
# Rancher Desktop (macOS)
cd sandbox
make rancher-up OPENAI_API_KEY=sk-... OPENAI_BASE_URL=https://... MODEL_NAME=...
# UI: http://localhost:7860
make rancher-down

# OpenShift
oc login https://api.your-cluster.example.com:6443
make ocp-deploy NAMESPACE=sandbox-<yourname> \
  OPENAI_API_KEY=sk-... OPENAI_BASE_URL=https://... MODEL_NAME=... \
  REGISTRY=icr.io/automation-saas-platform-dev ICR_API_KEY=...
make ocp-teardown NAMESPACE=sandbox-<yourname>   # keeps PVC + namespace
make ocp-wipe     NAMESPACE=sandbox-<yourname>   # removes everything

# Smoke test (either target)
make smoke-test
```
