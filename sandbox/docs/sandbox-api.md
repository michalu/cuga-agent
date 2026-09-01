# sandbox-api — curl examples

FastAPI management API running on port **8090**.
Every request (except `/ping`) requires the header `X-API-Key`.

> **OpenShift / self-signed cert:** add `-k` to every `curl` call — the cluster uses a self-signed certificate chain that macOS rejects by default.

## Setup — get your key

The key is generated once by `setup.sh` and stored on the PVC.

**Rancher Desktop (local):**
```bash
export SANDBOX_URL=http://localhost:8090
export SANDBOX_API_KEY=$(docker exec sandbox-api cat /var/lib/openshell/sandbox-api-key)
```

**OpenShift:**
```bash
export SANDBOX_URL=https://sandbox-api-<namespace>.apps.<cluster>
export SANDBOX_API_KEY=$(oc get secret sandbox-credentials -n <namespace> \
  -o jsonpath='{.data.SANDBOX_API_KEY}' | base64 -d)
```

> On a self-signed OpenShift cluster add `-k` to every `curl` call below.

---

## Endpoints

### GET /ping — liveness probe (no auth)

```bash
curl -sk "$SANDBOX_URL/ping"
```

```json
{"status": "ok"}
```

---

### GET /status — combined health check

Returns gateway connectivity, sandbox lifecycle state (`openshell sandbox list`), and whether execd is reachable.

> **`"connected": false` / `"No gateway configured"`** — means `openshell` CLI inside the pod has no gateway registered yet.
> Fixed in the current code: the pod reads `OPENSHELL_GATEWAY_NAME` + `OPENSHELL_GATEWAY_URL` env vars and runs `openshell gateway add` on startup.
> If you see this after a redeploy, rebuild the `sandbox-api` image and roll the Deployment.

```bash
curl -sk -H "X-API-Key: $SANDBOX_API_KEY" "$SANDBOX_URL/status" | jq
```

```json
{
  "gateway": {
    "connected": true,
    "output": "gateway connected"
  },
  "sandboxes": {
    "raw": "code-exec  running"
  },
  "execd": {
    "reachable": true,
    "url": "http://localhost:44772"
  }
}
```

---

### GET /info — connection details for BYOA clients

Returns the exact URLs and headers needed to call the data plane (execd) directly, plus a ready-to-run `curl` example.

```bash
curl -sk -H "X-API-Key: $SANDBOX_API_KEY" "$SANDBOX_URL/info" | jq
```

```json
{
  "execd_url": "http://localhost:44772",
  "management_url": "http://localhost:8090",
  "auth_required": false,
  "auth_header": "X-API-Key",
  "byoa_instructions": "1. Read SANDBOX_URL ...",
  "example_code_request": "curl -s -H \"Content-Type: application/json\" ..."
}
```

---

### GET /threads — list active workspaces

Lists thread IDs that have a directory under `/workspace` inside the sandbox.

```bash
curl -sk -H "X-API-Key: $SANDBOX_API_KEY" "$SANDBOX_URL/threads" | jq
```

```json
{
  "threads": ["thread-abc123", "thread-def456"],
  "count": 2
}
```

---

### GET /packages — list installed Python packages

Endpoint ma dwa tryby:

| Wywołanie | Co zwraca |
|---|---|
| `/packages` (bez parametru) | pakiety z **base image** sandboxa — stałe zależności wbudowane w obraz (`ipykernel`, `jupyter`, `uv` i ich zależności); `thread_id` w odpowiedzi będzie `null` |
| `/packages?thread_id=<id>` | pakiety z **venv konkretnego wątku** (`/workspace/<id>/.venv`) — czyli to co agent zainstalował przez `pip install` podczas sesji |

**Base image** (bez `thread_id` — `thread_id` w odpowiedzi będzie `null`, to poprawne):
```bash
curl -sk -H "X-API-Key: $SANDBOX_API_KEY" "$SANDBOX_URL/packages" | jq
```

**Konkretny wątek** — najpierw pobierz listę wątków:
```bash
curl -sk -H "X-API-Key: $SANDBOX_API_KEY" "$SANDBOX_URL/threads" | jq
```

Potem użyj konkretnego ID z odpowiedzi:
```bash
curl -sk -H "X-API-Key: $SANDBOX_API_KEY" \
  "$SANDBOX_URL/packages?thread_id=smoke-1788170886" | jq
```

```json
{
  "thread_id": "smoke-1788170886",
  "packages": [
    {"name": "numpy", "version": "1.26.4"},
    {"name": "pandas", "version": "2.2.1"}
  ],
  "count": 2
}
```

**Wszystkie wątki naraz** (bash loop):
```bash
for t in $(curl -sk -H "X-API-Key: $SANDBOX_API_KEY" "$SANDBOX_URL/threads" | jq -r '.threads[]'); do
  echo "=== $t ==="
  curl -sk -H "X-API-Key: $SANDBOX_API_KEY" "$SANDBOX_URL/packages?thread_id=$t" | jq '.packages[].name'
done
```

---

### POST /restart — tear down and recreate the execd sandbox

Destroys the running sandbox and creates a fresh one. Active kernels are lost; `/workspace` (PVC) survives.

```bash
curl -sk -X POST -H "X-API-Key: $SANDBOX_API_KEY" "$SANDBOX_URL/restart" | jq
```

```json
{"status": "restarting", "sandbox": "code-exec"}
```

After restart, poll `/status` until `execd.reachable` is `true`:
```bash
watch -n2 "curl -s -H \"X-API-Key: $SANDBOX_API_KEY\" $SANDBOX_URL/status | jq .execd"
```

---

## Quick one-liner (OpenShift, self-signed cert)

```bash
curl -sk -H "X-API-Key: $SANDBOX_API_KEY" "$SANDBOX_URL/status" | jq .
```

## Token — where does it come from?

The `SANDBOX_API_KEY` is a 64-character hex string (`openssl rand -hex 32`) generated once by `setup.sh`. It is:

- stored at `/var/lib/openshell/sandbox-api-key` on the PVC (survives restarts)
- injected into `sandbox-api` as the `SANDBOX_API_KEY` environment variable
- on OpenShift: also stored in the `sandbox-credentials` Secret

There is **no login flow** — you just pass the key as a static header on every request.
