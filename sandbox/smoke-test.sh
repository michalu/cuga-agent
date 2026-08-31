#!/usr/bin/env bash
# End-to-end smoke test: send a prompt to CUGA, wait for completion, then
# show a chronological trace of execd activity.
#
# Works on both Rancher Desktop (docker) and OpenShift (oc).
#
# Usage (from repo root):
#   bash sandbox/smoke-test.sh [OPTIONS] ["PROMPT"]
#
# Options:
#   --platform=rancher      Use docker (default; auto-detected if docker is found)
#   --platform=openshift    Use oc (auto-detected if oc is found and logged in)
#   --namespace=NAMESPACE   OpenShift namespace (default: sandbox)
#   --cuga-url=URL          CUGA HTTP endpoint (auto-detected per platform)
#
# Examples:
#   bash sandbox/smoke-test.sh
#   bash sandbox/smoke-test.sh --platform=openshift
#   bash sandbox/smoke-test.sh --platform=openshift --namespace=my-sandbox
#   bash sandbox/smoke-test.sh "Write the first 10 primes to /workspace/primes.txt"
#
# Requirements (Rancher):  curl, docker
# Requirements (OpenShift): curl, oc (logged in)
set -euo pipefail

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------
PLATFORM=""          # auto-detected below if not set
NAMESPACE="${SANDBOX_NAMESPACE:-sandbox}"
# OpenShell workspace — the prefix in "<workspace>--<sandbox>" pod and container
# names. Must match OPENSHELL_WORKSPACE in the Makefile.
OPENSHELL_WORKSPACE="${OPENSHELL_WORKSPACE:-openshell}"
CUGA_URL="${CUGA_URL:-}"
PROMPT="${PROMPT:-Do three things in order:
1. Use the CRM tool to list available contacts and save their names to /workspace/contacts_export.txt using Python.
2. Install the 'tomli' package with pip, then use it in Python to write a small TOML file /workspace/config.toml containing key version=\"1.0\".
3. Run a shell command that appends the line 'smoke-test ok' to /workspace/contacts_export.txt and then prints the last 3 lines of that file.}"
WAIT_SECONDS="${SMOKE_WAIT:-120}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
mkdir -p "$SCRIPT_DIR/logs"
LOG_FILE="$SCRIPT_DIR/logs/smoke-$(date +%Y%m%d-%H%M%S).log"

# ---------------------------------------------------------------------------
# Parse flags  (non-flag arg = prompt)
# ---------------------------------------------------------------------------
for arg in "$@"; do
  case "$arg" in
    --platform=*)   PLATFORM="${arg#--platform=}" ;;
    --namespace=*)  NAMESPACE="${arg#--namespace=}" ;;
    --cuga-url=*)   CUGA_URL="${arg#--cuga-url=}" ;;
    --help|-h)
      grep '^#' "$0" | head -30 | sed 's/^# \{0,1\}//'
      exit 0 ;;
    -*)
      echo "Unknown option: $arg  (use --help)" >&2; exit 1 ;;
    *)
      PROMPT="$arg" ;;
  esac
done

# ---------------------------------------------------------------------------
# Auto-detect platform
# ---------------------------------------------------------------------------
if [[ -z "$PLATFORM" ]]; then
  if command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; then
    PLATFORM="rancher"
  elif command -v oc >/dev/null 2>&1 && oc whoami >/dev/null 2>&1; then
    PLATFORM="openshift"
  else
    echo "Cannot auto-detect platform: neither docker nor oc (logged in) found." >&2
    echo "Pass --platform=rancher or --platform=openshift explicitly." >&2
    exit 1
  fi
fi

# ---------------------------------------------------------------------------
# Tee all output to log file (ANSI-stripped)
# ---------------------------------------------------------------------------
exec > >(tee >(sed 's/\x1b\[[0-9;]*m//g' > "$LOG_FILE")) 2>&1

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; DIM='\033[2m'; NC='\033[0m'

step()  { printf "\n${YELLOW}==> %s${NC}\n" "$*"; }
ok()    { printf "${GREEN}  ok${NC}  %s\n" "$*"; }
info()  { printf "${CYAN}  ..${NC}  %s\n" "$*"; }
fail()  { printf "${RED}  FAIL${NC}  %s\n" "$*" >&2; }

printf "Log:      %s\n" "$LOG_FILE"
printf "Platform: %s\n" "$PLATFORM"

# ---------------------------------------------------------------------------
# Platform backend — all primitives resolved here, used uniformly below.
#
#  cuga_exec           CMD...  run a command inside the CUGA pod/container
#  execd_exec          CMD...  run a command inside the execd pod/container
#  cuga_logs_since     EPOCH   stream cuga-demo container logs since UNIX timestamp
#  gateway_logs_since  EPOCH   stream openshell-gateway container logs since UNIX timestamp
# ---------------------------------------------------------------------------

CUGA_CONTAINER=""
EXECD_CONTAINER=""
GATEWAY_CONTAINER=""

if [[ "$PLATFORM" == "rancher" ]]; then
  command -v docker >/dev/null 2>&1 || { fail "docker not found"; exit 1; }
  command -v curl   >/dev/null 2>&1 || { fail "curl not found"; exit 1; }

  cuga_exec()          { docker exec "$CUGA_CONTAINER"    "$@"; }
  execd_exec()         { docker exec "$EXECD_CONTAINER"   "$@"; }
  cuga_logs_since()    { docker logs "$CUGA_CONTAINER"    --since "$1" 2>&1; }
  execd_logs_since()   { docker logs "$EXECD_CONTAINER"   --since "$1" 2>&1; }
  gateway_logs_since() { docker logs "$GATEWAY_CONTAINER" --since "$1" 2>&1; }

elif [[ "$PLATFORM" == "openshift" ]]; then
  command -v oc   >/dev/null 2>&1 || { fail "oc not found"; exit 1; }
  command -v curl >/dev/null 2>&1 || { fail "curl not found"; exit 1; }
  oc whoami >/dev/null 2>&1       || { fail "Not logged in to OpenShift (oc whoami failed)"; exit 1; }

  cuga_exec()       { oc exec -n "$NAMESPACE" "$CUGA_CONTAINER"  -- "$@"; }
  execd_exec()      { oc exec -n "$NAMESPACE" "$EXECD_CONTAINER" -- "$@"; }
  # Policy decisions do NOT reach pod stdout on Kubernetes: the supervisor sends
  # its OCSF events to the gateway, which aggregates them per sandbox.  `oc logs`
  # on a sandbox pod therefore shows zero ALLOWED/DENIED lines and the timeline
  # loses its whole policy layer.  Read them from the gateway instead.
  #
  # The gateway stamps "[<epoch>.<millis>] "; the timeline parser expects the
  # OCSF "YYYY-MM-DDTHH:MM:SS.mmmZ" form, so normalise on the way through and
  # drop anything older than the run.
  _openshell_logs_since() {
    local sandbox="$1" since="$2"
    [[ -n "${OPENSHELL_ENDPOINT:-}" ]] || return 0
    openshell --gateway-endpoint "$OPENSHELL_ENDPOINT" \
             --workspace "$OPENSHELL_WORKSPACE" logs "$sandbox" 2>/dev/null \
      | python3 -c '
import datetime, re, sys
since = float(sys.argv[1])
pat = re.compile(r"^\[(\d+\.\d+)\]\s*(.*)$")
for line in sys.stdin:
    m = pat.match(line.rstrip("\n"))
    if not m:
        continue
    ts = float(m.group(1))
    if ts < since:
        continue
    stamp = datetime.datetime.fromtimestamp(ts, datetime.timezone.utc) \
              .strftime("%Y-%m-%dT%H:%M:%S.") + "%03dZ" % ((ts % 1) * 1000)
    print(stamp, m.group(2))
' "$since"
  }
  cuga_logs_since()  { _openshell_logs_since cuga-demo "$1"; }
  execd_logs_since() { _openshell_logs_since code-exec "$1"; }
  gateway_logs_since() {
    local iso
    iso="$(date -u -d "@$1" '+%Y-%m-%dT%H:%M:%SZ' 2>/dev/null \
        || date -u -r "$1"  '+%Y-%m-%dT%H:%M:%SZ')"
    oc logs -n "$NAMESPACE" "$GATEWAY_CONTAINER" --since-time="$iso" 2>&1
  }

else
  fail "Unknown platform: $PLATFORM  (expected: rancher | openshift)"; exit 1
fi

# ---------------------------------------------------------------------------
step "Preflight  [$PLATFORM]"
# ---------------------------------------------------------------------------

if [[ "$PLATFORM" == "rancher" ]]; then
  CUGA_CONTAINER="$(docker ps --filter "name=openshell-${OPENSHELL_WORKSPACE}--cuga-demo" --format '{{.Names}}' | head -1)"
  EXECD_CONTAINER="$(docker ps --filter "name=openshell-${OPENSHELL_WORKSPACE}--code-exec" --format '{{.Names}}' | head -1)"
  # The gateway has a fixed container_name in docker-compose.yml.
  GATEWAY_CONTAINER="$(docker ps --filter "name=openshell-execd-gateway" \
    --format '{{.Names}}' | head -1 || true)"

  [[ -n "$CUGA_CONTAINER" ]]    || { fail "cuga-demo container not found — is the sandbox running?"; exit 1; }
  [[ -n "$EXECD_CONTAINER" ]]   || { fail "code-exec container not found — is the sandbox running?"; exit 1; }
  ok "cuga    (LLM agent + server)       : $CUGA_CONTAINER"
  ok "execd   (code sandbox)             : $EXECD_CONTAINER"
  if [[ -n "$GATEWAY_CONTAINER" ]]; then
    ok "gateway (openshell control plane) : $GATEWAY_CONTAINER"
  else
    info "gateway container not found — execd policy log will be skipped"
  fi

  : "${CUGA_URL:=http://localhost:7860}"

elif [[ "$PLATFORM" == "openshift" ]]; then
  # OpenShell gateway names sandbox pods "<workspace>--<sandboxname>".
  # Neither cuga-demo nor execd carry an app= label — match by pod name.
  CUGA_CONTAINER="$(oc get pods -n "$NAMESPACE" \
    --field-selector=status.phase=Running \
    -o jsonpath='{range .items[*]}{.metadata.name}{"\n"}{end}' 2>/dev/null \
    | grep 'cuga-demo' | head -1 || true)"
  GATEWAY_CONTAINER="$(oc get pods -n "$NAMESPACE" -l app=openshell-gateway \
    --field-selector=status.phase=Running \
    -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || true)"
  # OpenShell names execd pods "<workspace>--<sandboxname>" (e.g. "cuga--code-exec").
  EXECD_CONTAINER="$(oc get pods -n "$NAMESPACE" \
    --field-selector=status.phase=Running \
    -o jsonpath='{range .items[*]}{.metadata.name}{"\n"}{end}' 2>/dev/null \
    | grep 'code-exec' | head -1 || true)"

  [[ -n "$CUGA_CONTAINER" ]]  || { fail "cuga-demo pod not found in namespace '$NAMESPACE' — is the deployment running?  (oc get pods -n $NAMESPACE)"; exit 1; }
  if [[ -z "$EXECD_CONTAINER" ]]; then
    # Check whether a code-exec pod exists but is not Running (Error/CrashLoop/Pending).
    EXECD_ANY="$(oc get pods -n "$NAMESPACE" \
      -o jsonpath='{range .items[*]}{.metadata.name}{" "}{.status.phase}{"\n"}{end}' 2>/dev/null \
      | grep 'code-exec' | head -1 || true)"
    if [[ -n "$EXECD_ANY" ]]; then
      fail "execd pod found but not Running in namespace '$NAMESPACE': $EXECD_ANY"
      fail "Delete it and let OpenShell recreate it:  oc delete pod $(echo "$EXECD_ANY" | awk '{print $1}') -n $NAMESPACE"
    else
      fail "execd pod not found in namespace '$NAMESPACE'.  Check: oc get pods -n $NAMESPACE"
    fi
    exit 1
  fi
  ok "cuga    (Pod) : $CUGA_CONTAINER  [ns=$NAMESPACE]"
  ok "execd   (Pod) : $EXECD_CONTAINER  [ns=$NAMESPACE]"
  if [[ -n "$GATEWAY_CONTAINER" ]]; then
    ok "gateway (Pod) : $GATEWAY_CONTAINER  [ns=$NAMESPACE]"
  else
    info "openshell-gateway pod not found — execd policy log will be skipped"
  fi

  # Policy decisions live in the gateway (see _openshell_logs_since).  Reaching
  # it needs a tunnel; --gateway-endpoint keeps this out of the user's gateway
  # registry, so running the smoke test never disturbs their `openshell` setup.
  OPENSHELL_ENDPOINT=""
  OPENSHELL_PF_PID=""
  if [[ -n "$GATEWAY_CONTAINER" ]] && command -v openshell >/dev/null 2>&1; then
    OPENSHELL_PF_PORT="${OPENSHELL_PF_PORT:-18099}"
    oc port-forward "svc/openshell-gateway" "$OPENSHELL_PF_PORT:8080" -n "$NAMESPACE" \
      >/dev/null 2>&1 &
    OPENSHELL_PF_PID=$!
    for _ in $(seq 1 15); do
      curl -s -m 1 "http://127.0.0.1:$OPENSHELL_PF_PORT" >/dev/null 2>&1 && break
      sleep 1
    done
    OPENSHELL_ENDPOINT="http://127.0.0.1:$OPENSHELL_PF_PORT"
  else
    info "openshell CLI or gateway unavailable — policy decisions will be skipped"
  fi

  if [[ -z "$CUGA_URL" ]]; then
    ROUTE_HOST="$(oc get route cuga-demo -n "$NAMESPACE" \
      -o jsonpath='{.spec.host}' 2>/dev/null || true)"
    if [[ -n "$ROUTE_HOST" ]]; then
      CUGA_URL="https://$ROUTE_HOST"
    else
      CUGA_URL="http://cuga-demo.${NAMESPACE}.svc.cluster.local:7860"
    fi
  fi
fi

curl -sfk "$CUGA_URL/health" >/dev/null 2>&1 \
  || { fail "CUGA not reachable at $CUGA_URL"; exit 1; }
ok "CUGA reachable at $CUGA_URL"

# ---------------------------------------------------------------------------
step "Sending prompt  [you → cuga via HTTP]"
# ---------------------------------------------------------------------------
THREAD_ID="smoke-$(date +%s)"
info "Thread ID : $THREAD_ID"
info "Prompt    : $PROMPT"

T_BEFORE="$(date -u +%s)"
T_BEFORE_ISO="$(date -u -d "@$T_BEFORE" '+%Y-%m-%d %H:%M' 2>/dev/null \
             || date -u -r "$T_BEFORE"  '+%Y-%m-%d %H:%M')"
CUGA_LOG_PATH="/tmp/cuga/logging/cuga.log"

info "Waiting up to ${WAIT_SECONDS}s (auto-approving tool calls)..."
python3 - "$CUGA_URL" "$THREAD_ID" "$PROMPT" "$WAIT_SECONDS" <<'PYEOF'
import datetime, http.client, json, ssl, sys, time, urllib.parse, urllib.request, urllib.error

base, thread_id, prompt, timeout = sys.argv[1], sys.argv[2], sys.argv[3], int(sys.argv[4])

# Clusters with self-signed TLS (e.g. Fyre dev) fail strict certificate
# verification.  We disable it for the smoke-test HTTP client only.
_ssl_ctx = ssl.create_default_context()
_ssl_ctx.check_hostname = False
_ssl_ctx.verify_mode = ssl.CERT_NONE

def post(path, payload, timeout_s=30):
    req = urllib.request.Request(
        base + path,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "X-Thread-ID": thread_id},
        method="POST"
    )
    return urllib.request.urlopen(req, timeout=timeout_s, context=_ssl_ctx)

def iter_sse_events(resp):
    """Yield (event_name, data_str) pairs from a proper text/event-stream response.

    CUGA emits fully SSE-conformant blocks:
        event: <Name>
        data: <line1>
        data: <line2>
        <blank line>

    The earlier line-by-line parser broke on multi-line data payloads and
    completely missed the event name because it only looked for 'data:' lines.
    This parser accumulates a full event block (terminated by a blank line) and
    then returns both the event name and the joined data value.
    """
    buf = b""
    while True:
        chunk = resp.read(4096)
        if not chunk:
            break
        buf += chunk
        # Events are separated by blank lines ("\n\n").
        while b"\n\n" in buf:
            block, buf = buf.split(b"\n\n", 1)
            event_name = None
            data_lines = []
            for raw_line in block.split(b"\n"):
                line = raw_line.decode("utf-8", errors="replace")
                if line.startswith("event:"):
                    event_name = line[6:].strip()
                elif line.startswith("data:"):
                    value = line[5:]
                    # Per SSE spec: a single leading space is syntactic, not data.
                    if value.startswith(" "):
                        value = value[1:]
                    if value != "[DONE]":
                        data_lines.append(value)
            data = "\n".join(data_lines)
            if event_name or data:
                yield event_name, data

def send_approval(action_id):
    """POST an ActionResponse to /stream to approve a HITL tool-approval request.

    ActionResponse requires: action_id, response_type, timestamp, confirmed.
    The earlier code sent {"action_id": ..., "approved": True} which failed
    Pydantic validation because 'approved' is not a field and 'response_type'
    / 'timestamp' were missing — causing a 422 and the HITL gate to stay open.
    """
    payload = {
        "action_id": action_id,
        "response_type": "confirmation",
        "timestamp": datetime.datetime.utcnow().isoformat(),
        "confirmed": True,
    }
    resp = post("/stream", payload, timeout_s=30)
    # Drain the response so the connection is released cleanly.
    resp.read()
    return resp.status

deadline = time.time() + timeout
approvals = 0
try:
    resp = post("/stream", {"query": prompt}, timeout_s=timeout)
    print(f"  Stream: HTTP {resp.status}")
    for event_name, data in iter_sse_events(resp):
        # Try to parse data as JSON for structured inspection.
        ev = None
        try:
            ev = json.loads(data) if data else None
        except Exception:
            pass

        print(f"  Event: name={event_name!r} data_len={len(data)}")

        # CUGA HITL: the stream emits a FollowUpAction JSON payload tagged with
        # the event name "HumanFeedback" (or similar supervisor node name) and
        # the data JSON contains an "action_id" field.
        action_id = None
        if isinstance(ev, dict):
            action_id = ev.get("action_id")
            if not action_id and isinstance(ev.get("data"), dict):
                action_id = ev["data"].get("action_id")

        if action_id:
            approvals += 1
            print(f"  Auto-approving #{approvals}: action_id={action_id[:16]}...")
            try:
                status = send_approval(action_id)
                print(f"  Approved ok (HTTP {status})")
            except Exception as e:
                print(f"  Approval failed: {e}")

        # CUGA signals completion with an "Answer" event (the terminal SSE event).
        if event_name == "Answer":
            print(f"  Agent finished (Answer event received)")
            break

except Exception as e:
    print(f"  Stream error: {e}")

print(f"  Total approvals sent: {approvals}")
PYEOF

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"; [[ -n "${OPENSHELL_PF_PID:-}" ]] && kill "$OPENSHELL_PF_PID" 2>/dev/null' EXIT

# Fetch the CUGA structured log once (same path on both platforms).
cuga_exec cat "$CUGA_LOG_PATH" > "$TMP/full.log" 2>/dev/null || true

# Find the context_id assigned to this thread.
CONTEXT_ID="$(grep "thread=${THREAD_ID}" "$TMP/full.log" \
  | grep -o 'context_id=[^ ]*' | head -1 | cut -d= -f2 || true)"

# Collect execd lines for this context_id + all lines mentioning our thread.
if [[ -n "$CONTEXT_ID" ]]; then
  grep -E "context_id=${CONTEXT_ID}|thread=${THREAD_ID}" "$TMP/full.log" \
    > "$TMP/run.log" || true
else
  grep "thread=${THREAD_ID}" "$TMP/full.log" > "$TMP/run.log" || true
fi

# ---------------------------------------------------------------------------
step "Run timeline  [thread=${THREAD_ID}]"
# ---------------------------------------------------------------------------

# Collect policy decisions from both gateways (OCSF format: ALLOWED/DENIED).
# cuga-demo supervisor  → policy for traffic OUT of the cuga-demo sandbox
# execd supervisor      → policy for traffic OUT of the execd sandbox
#
# Filter: keep only /code and /code/context (the actual execution calls) plus
# inference and DENIED decisions.  Drop /command — that is execd's periodic
# heartbeat/keepalive and produces one pair of l7+opa lines every second,
# drowning out the meaningful events.
cuga_logs_since "$T_BEFORE" \
  | grep -E 'ALLOWED|DENIED' \
  | grep -v 'unknown channel\|channel_eof' \
  | grep -vE 'ALLOWED .* /command ' \
  > "$TMP/policy_cuga.log" || true

execd_logs_since "$T_BEFORE" \
  | grep -E 'ALLOWED|DENIED' \
  | grep -v 'unknown channel\|channel_eof' \
  > "$TMP/policy_execd.log" || true

# Build a unified, timestamped event stream and sort it chronologically.
# Each source uses a different timestamp format — normalise to "YYYY-MM-DD HH:MM:SS.mmm"
# so a plain lexicographic sort gives the correct order.
#
# Sources:
#   CUGA structured log       →  "2026-08-27 10:52:18.392"  prefix
#   OCSF policy (cuga-demo)   →  "2026-08-27T10:52:16.229Z" → normalised
#   OCSF policy (execd)       →  same format, separate file
python3 - "$TMP/run.log" "$TMP/policy_cuga.log" "$TMP/policy_execd.log" \
    "$T_BEFORE_ISO" "$CUGA_LOG_PATH" <<'PYEOF'
import sys, re, json

run_log         = sys.argv[1]
policy_cuga_log = sys.argv[2]   # OCSF policy from cuga-demo supervisor
policy_execd_log = sys.argv[3]  # OCSF policy from execd supervisor
t_before        = sys.argv[4]   # "YYYY-MM-DD HH:MM" lower bound for tool-call filter
cuga_log        = sys.argv[5]   # path inside the container — only used as label

RESET  = "\033[0m"
YELLOW = "\033[1;33m"
CYAN   = "\033[0;36m"
GREEN  = "\033[0;32m"
RED    = "\033[0;31m"
DIM    = "\033[2m"
BLUE   = "\033[0;34m"
MAG    = "\033[0;35m"

events = []  # list of (sort_key, label, detail)

# ── 1. CUGA structured log: tool calls ──────────────────────────────────────
# Format: "2026-08-27 10:52:18.392 | DEBUG | ...call_function:301 - ApiRegistry: call_function(function_name='foo', ...)"
re_ts   = re.compile(r'^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d+)')
re_fn   = re.compile(r"function_name='([^']+)'")
re_args = re.compile(r"arguments=(\{[^}]*\})")

try:
    with open(run_log) as fh:
        for line in fh:
            # execd:code result lines  (come from run.log, already filtered to our context_id)
            if '[execd:code]' in line and 'duration_ms' in line:
                m = re_ts.search(line)
                ts = m.group(1) if m else "0000-00-00 00:00:00.000"
                ok = '✓' if '✓' in line else '✗'
                color = GREEN if ok == '✓' else RED
                dur = re.search(r'duration_ms=(\d+)', line)
                out = re.search(r'output_len=(\d+)', line)
                err = re.search(r'error=([^\s].*?)(?= \w+=|$)', line)
                detail = f"duration={dur.group(1)}ms" if dur else ""
                if out:
                    detail += f"  output_len={out.group(1)}"
                if err:
                    detail += f"  error={err.group(1)[:80]}"
                events.append((ts,
                    f"{color}[execd:code {ok}]{RESET}",
                    detail))

            # filesystem tool operations
            if 'fs_op=' in line:
                m = re_ts.search(line)
                ts = m.group(1) if m else "0000-00-00 00:00:00.000"
                op   = re.search(r'fs_op=(\S+)', line)
                path = re.search(r'\bpath=(\S+)', line)
                events.append((ts,
                    f"{MAG}[fs]{RESET}",
                    f"{op.group(1) if op else '?'}  {path.group(1) if path else ''}"))
except FileNotFoundError:
    pass

# ── 2. Full CUGA log: tool calls (filter by T_BEFORE wall-clock) ─────────────
# These lines are NOT scoped to context_id so we read the file passed in argv.
# We re-read run_log which already has thread-scoped lines; tool calls however
# aren't tagged with thread_id so we need to use time-based filtering from stdin.
# The caller piped the full log into $TMP/full.log — read it from there.
import os
full_log = os.path.join(os.path.dirname(run_log), "full.log")
try:
    with open(full_log) as fh:
        for line in fh:
            m = re_ts.match(line)
            if not m:
                continue
            ts = m.group(1)
            if ts[:16] < t_before:
                continue
            if 'call_function' not in line or 'function_name=' not in line:
                continue
            fn = re_fn.search(line)
            if not fn:
                continue
            args = re_args.search(line)
            arg_str = ""
            if args:
                try:
                    d = json.loads(args.group(1).replace("'", '"'))
                    arg_str = "  " + "  ".join(f"{k}={v}" for k, v in d.items())
                except Exception:
                    arg_str = "  " + args.group(1)[:60]
            events.append((ts,
                f"{YELLOW}[tool]{RESET}",
                f"{fn.group(1)}{arg_str}"))
except FileNotFoundError:
    pass

# ── 3. Policy decisions (OCSF timestamps: "2026-08-27T10:52:16.229Z") ────────
re_ocsf    = re.compile(r'^(\d{4}-\d{2}-\d{2})T(\d{2}:\d{2}:\d{2}\.\d+)Z')
re_verdict = re.compile(r'(ALLOWED|DENIED)')

def _parse_policy_log(path, label_tag):
    try:
        with open(path) as fh:
            for line in fh:
                m = re_ocsf.match(line)
                if not m:
                    continue
                ts = f"{m.group(1)} {m.group(2)}"
                verdict = re_verdict.search(line)
                color = GREEN if verdict and verdict.group(1) == 'ALLOWED' else RED
                detail = line.strip()
                # Strip log decoration up to the severity marker. Docker emits
                # "<ts> OCSF <kind> [INFO] …"; the gateway emits
                # "<ts> [sandbox] [OCSF ] [ocsf] <kind> [INFO] …". Both end here.
                detail = re.sub(r'^.*?\[INFO\]\s+', '', detail)
                events.append((ts, f"{color}{label_tag}{RESET}", detail))
    except FileNotFoundError:
        pass

# cuga-demo supervisor:  policy for outbound traffic FROM the cuga-demo sandbox
_parse_policy_log(policy_cuga_log,  "[policy/cuga]")
# execd supervisor:      policy for outbound traffic FROM the execd sandbox
_parse_policy_log(policy_execd_log, "[policy/execd]")

# ── Sort and print (chronological, section header on label change) ────────────
events.sort(key=lambda e: e[0])

SECTION_LABELS = {
    "[tool]":          "Tools called      [cuga-demo]",
    "[execd:code ✓]":  "Code execution    [execd]",
    "[execd:code ✗]":  "Code execution    [execd]",
    "[fs]":            "Code execution    [execd]",
    "[policy/cuga]":   "Policy decisions  [cuga-demo sandbox]",
    "[policy/execd]":  "Policy decisions  [execd sandbox]",
}

def bare_label(label):
    """Strip ANSI escapes to get the plain [tag] text."""
    return re.sub(r'\033\[[0-9;]*m', '', label)

if not events:
    print("  (no events captured)")
else:
    prev_section = None
    for ts, label, detail in events:
        hms = ts[11:23] if len(ts) > 11 else ts  # show HH:MM:SS.mmm only
        section = SECTION_LABELS.get(bare_label(label), bare_label(label))
        if section != prev_section:
            print(f"\n  {DIM}--- {section} ---{RESET}")
            prev_section = section
        print(f"  {DIM}{hms}{RESET}  {label}  {detail}")
PYEOF

# ---------------------------------------------------------------------------
step "Workspace  [${EXECD_CONTAINER}:/workspace/${THREAD_ID}]"
# ---------------------------------------------------------------------------
WORKSPACE_DIR="/workspace/${THREAD_ID}"
WORKSPACE_FILES="$(execd_exec \
  find "$WORKSPACE_DIR" -not -path '*/.venv/*' -not -path '*/vendor/*' \
    -not -path '*/libs/*' \
    -not -name '*.pyc' -not -name '*.pyo' -not -name '*.so' -not -name '*.so.*' \
  2>/dev/null | sort || true)"

SMOKE_FAIL=0

if [[ -z "$WORKSPACE_FILES" ]]; then
  info "(workspace empty or not accessible)"
else
  echo "$WORKSPACE_FILES" | sed 's/^/  /'
  echo
  printf "${YELLOW}File contents (<=2 KB):${NC}\n"
  while IFS= read -r fpath; do
    # skip directories
    execd_exec test -d "$fpath" 2>/dev/null && continue || true
    fname="${fpath##*/}"
    [[ "$fname" == .* ]] && continue
    size="$(execd_exec stat -c '%s' "$fpath" 2>/dev/null || echo 9999)"
    if [[ "$size" -le 2048 ]]; then
      printf "${CYAN}  --- %s ---${NC}\n" "$fpath"
      execd_exec cat "$fpath" 2>/dev/null | head -20 | LC_ALL=C sed 's/^/  /'
    else
      printf "${CYAN}  --- %s ---${NC}  ${DIM}(%s bytes — showing first 5 lines)${NC}\n" "$fpath" "$size"
      execd_exec cat "$fpath" 2>/dev/null | head -5 | LC_ALL=C sed 's/^/  /'
    fi
    # Assert non-empty — only for files directly in the workspace root,
    # not inside subdirectories (installed packages have intentionally
    # empty marker files such as REQUESTED, py.typed, etc.).
    parent="${fpath%/*}"
    if [[ "$size" -eq 0 ]] && [[ "$parent" == "$WORKSPACE_DIR" ]]; then
      fail "$fpath is empty"
      SMOKE_FAIL=1
    fi
  done < <(echo "$WORKSPACE_FILES")
fi

# ---------------------------------------------------------------------------
# Assertions — verify each execution channel produced expected output
# ---------------------------------------------------------------------------
step "Assertions"

# 1. Python code channel — contacts written by generated Python
_assert_file() {
  local label="$1" path="$2" pattern="$3"
  if execd_exec test -f "$path" 2>/dev/null; then
    if [[ -n "$pattern" ]]; then
      if execd_exec grep -q "$pattern" "$path" 2>/dev/null; then
        ok "$label  ($path contains '$pattern')"
      else
        fail "$label  ($path exists but does not contain '$pattern')"
        SMOKE_FAIL=1
      fi
    else
      ok "$label  ($path exists)"
    fi
  else
    fail "$label  ($path not found)"
    SMOKE_FAIL=1
  fi
}

_assert_file "Python code → file write" \
  "$WORKSPACE_DIR/contacts_export.txt" ""

# 2. pip install + Python → TOML file written by installed package
_assert_file "pip install + Python → config.toml" \
  "$WORKSPACE_DIR/config.toml" 'version'

# 3. shell command (run_command) → appended marker line
_assert_file "run_command (shell) → contacts_export.txt marker" \
  "$WORKSPACE_DIR/contacts_export.txt" "smoke-test ok"

echo
if [[ "$SMOKE_FAIL" -eq 0 ]]; then
  ok "Done.  (log saved to $LOG_FILE)"
else
  fail "Smoke test FAILED — see above.  (log saved to $LOG_FILE)"
  exit 1
fi
