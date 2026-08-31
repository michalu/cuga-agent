# =============================================================================
#  cuga-openshell sandbox
#
#  QUICKSTART
#  ──────────
#
#  1. Set credentials (once — or export in your shell):
#
#       export OPENAI_API_KEY="sk-..."
#       export OPENAI_BASE_URL="https://your-llm-endpoint"
#       export MODEL_NAME="your-model-id"
#
#  2a. Rancher Desktop (macOS, local Docker):
#
#       make rancher-up        # start gateway + execd + cuga demo
#       open http://localhost:7860
#       make rancher-down      # stop everything
#
#  2b. Remote OpenShift (amd64 cluster):
#
#       # 1. log in to OpenShift (credentials in team 1Password vault)
#       oc login https://api.your-cluster.example.com:6443
#
#       # 2. log in to the image registry (ICR: needs CloudRock account + key)
#       export REGISTRY="icr.io/automation-saas-platform-dev"
#       echo "$ICR_API_KEY" | docker login -u iamapikey --password-stdin icr.io
#
#       # 3. deploy
#       make ocp-deploy        # build image (linux/amd64), push, deploy full stack
#       make ocp-teardown      # remove sandboxes + deployments, keep PVC + namespace
#       make ocp-wipe          # remove the namespace and everything in it
#
#  3. Smoke test (either target):
#
#       make smoke-test
#       # or with a custom prompt:
#       make smoke-test PROMPT="List files in /workspace"
#
#  All variables can be set on the command line instead of exporting:
#    make ocp-deploy OPENAI_API_KEY=sk-... OPENAI_BASE_URL=https://... \
#                    MODEL_NAME=my-model REGISTRY=registry.example.com/ns
#
#  Run `make help` for the full target reference.
# =============================================================================

SHELL = /usr/bin/env bash -o pipefail
.SHELLFLAGS = -ec

# BuildKit required by Dockerfile.openshell (--chmod in COPY --from)
export DOCKER_BUILDKIT := 1

# ---------------------------------------------------------------------------
# Variables — override on the command line or export in the environment
# ---------------------------------------------------------------------------

# OpenShell workspace. The gateway names sandbox pods "<workspace>--<sandbox>",
# so this is what turns "default--cuga-demo" into "openshell--cuga-demo". It is
# a tenancy boundary (workspaces have members); naming it "openshell" makes it
# clear that the pod is managed by OpenShell.
OPENSHELL_WORKSPACE ?= openshell
export OPENSHELL_WORKSPACE

OPENAI_API_KEY  ?=
OPENAI_BASE_URL ?=
MODEL_NAME      ?=

# OCP: external registry the cluster can pull from (required for ocp-deploy)
#   ICR (team default): icr.io/automation-saas-platform-dev
#   Quay (on-cluster):  quay-<cluster-route>/sovereign
#   ICR login:          make ocp-login-icr ICR_API_KEY=<key>
REGISTRY  ?=
IMAGE_TAG ?= latest

# OCP: target namespace — always use a personal namespace on shared clusters.
#   Convention: sandbox-<yourname>
#   Example:    make ocp-deploy NAMESPACE=sandbox-michal ...
NAMESPACE ?= sandbox-$(shell whoami)

# OCP: ICR API key — used only by ocp-login-icr
ICR_API_KEY ?=

# Rancher: Docker socket — auto-detected; override only if needed
DOCKER_HOST ?= $(shell \
  [ -S /var/run/docker.sock ]       && echo "unix:///var/run/docker.sock"       || \
  [ -S "$(HOME)/.rd/docker.sock" ]  && echo "unix://$(HOME)/.rd/docker.sock"    || true)

# ---------------------------------------------------------------------------
# Layout
# ---------------------------------------------------------------------------

# All paths are relative to this Makefile (sandbox/)
RANCHER_SANDBOX := sandbox/deploy/rancher
RANCHER_CUGA    := cuga/deploy/rancher

OCP_SANDBOX     := sandbox/deploy/openshift
OCP_CUGA        := cuga/deploy/openshift

# execd image built in-cluster
EXECD_IMAGE     := image-registry.openshift-image-registry.svc:5000/$(NAMESPACE)/openshell-execd:latest
# cuga image pushed from local Docker
CUGA_IMAGE      := $(REGISTRY)/cuga-openshell:$(IMAGE_TAG)
# opensandbox/execd base image mirrored to ICR so in-cluster builds avoid Docker Hub rate limits
EXECD_BASE_SRC  := opensandbox/execd@sha256:1cd30ef9bb7f816e71b276415cf153107da04d6129a60d181d95ca73e1bb62c5
EXECD_BASE_DST  := $(REGISTRY)/opensandbox-execd:latest

# ---------------------------------------------------------------------------
.PHONY: all
all: help

##@ General

.PHONY: help
help: ## Display this help.
	@awk 'BEGIN {FS = ":.*##"; printf "\nUsage:\n  make \033[36m<target>\033[0m\n"} /^[a-zA-Z_0-9-]+:.*?##/ { printf "  \033[36m%-24s\033[0m %s\n", $$1, $$2 } /^##@/ { printf "\n\033[1m%s\033[0m\n", substr($$0, 5) } ' $(MAKEFILE_LIST)

# ===========================================================================
##@ Rancher Desktop (local, macOS)
# ===========================================================================

.PHONY: rancher-up
rancher-up: _check-llm-vars ## Start full stack: gateway + execd + cuga demo.
	@echo "==> [1/2] Starting sandbox stack (gateway + execd)"
	@$(MAKE) _rancher-sandbox-up
	@echo "==> [2/2] Starting CUGA demo"
	@$(MAKE) _rancher-cuga-up

.PHONY: rancher-down
rancher-down: ## Stop full stack (cuga → execd → gateway).
	@echo "==> Stopping CUGA demo sandbox"
	DOCKER_HOST=$(DOCKER_HOST) openshell forward stop 7860  cuga-demo  2>/dev/null || true
	DOCKER_HOST=$(DOCKER_HOST) openshell forward stop 8001  cuga-demo  2>/dev/null || true
	DOCKER_HOST=$(DOCKER_HOST) openshell sandbox  delete    cuga-demo  2>/dev/null || true
	@echo "==> Stopping execd sandbox"
	DOCKER_HOST=$(DOCKER_HOST) openshell forward stop 44772 code-exec  2>/dev/null || true
	DOCKER_HOST=$(DOCKER_HOST) openshell sandbox  delete    code-exec  2>/dev/null || true
	@echo "==> Stopping compose stack"
	SANDBOX_API_KEY=$$(cat /var/lib/openshell/sandbox-api-key 2>/dev/null || echo dummy) \
	DOCKER_HOST=$(DOCKER_HOST) \
	  docker compose -f $(RANCHER_SANDBOX)/docker-compose.yml down

.PHONY: _rancher-sandbox-up
_rancher-sandbox-up:
	@# Generate sandbox API key if missing
	@mkdir -p /var/lib/openshell
	@if [ ! -f /var/lib/openshell/sandbox-api-key ]; then \
	  printf '%s' "$$(head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n')" \
	    > /var/lib/openshell/sandbox-api-key; \
	  chmod 600 /var/lib/openshell/sandbox-api-key; \
	  echo "==> Generated sandbox API key: /var/lib/openshell/sandbox-api-key"; \
	else \
	  echo "==> Reusing existing sandbox API key"; \
	fi
	@export DOCKER_HOST=$(DOCKER_HOST); \
	SANDBOX_API_KEY=$$(cat /var/lib/openshell/sandbox-api-key); \
	docker compose -f $(RANCHER_SANDBOX)/docker-compose.yml up -d; \
	openshell gateway list 2>/dev/null | grep -q openshell-docker || \
	  openshell gateway add http://localhost:8080 --name openshell-docker; \
	openshell gateway select openshell-docker >/dev/null; \
	openshell workspace create --name $(OPENSHELL_WORKSPACE) >/dev/null 2>&1 || true; \
	echo "==> Waiting for gateway…"; \
	for i in $$(seq 1 60); do \
	  openshell status >/dev/null 2>&1 && break; \
	  sleep 1; \
	done; \
	openshell status; \
	BUILD_CTX=$$(mktemp -d); \
	cp sandbox/Dockerfile.execd      $$BUILD_CTX/Dockerfile; \
	cp sandbox/execd-entrypoint.sh   $$BUILD_CTX/; \
	cp sandbox/execd-isolation.toml  $$BUILD_CTX/; \
	openshell forward stop 44772 code-exec >/dev/null 2>&1 || true; \
	openshell sandbox delete code-exec     >/dev/null 2>&1 || true; \
	openshell sandbox create \
	  --name code-exec \
	  --from $$BUILD_CTX \
	  --policy $(RANCHER_SANDBOX)/execd-policy.yaml \
	  --no-auto-providers \
	  --detach \
	  -- /usr/local/bin/execd-entrypoint; \
	rm -rf $$BUILD_CTX; \
	openshell forward start 44772 code-exec --background >/dev/null 2>&1 || \
	  echo "WARNING: could not forward execd port 44772"
	@echo "==> sandbox stack ready"
	@echo "    execd:       http://localhost:44772/ping"
	@echo "    sandbox-api: http://localhost:8090/ping"
	@echo "    API key:     $$(cat /var/lib/openshell/sandbox-api-key)"

.PHONY: _rancher-cuga-up
_rancher-cuga-up:
	@export DOCKER_HOST=$(DOCKER_HOST); \
	BUILD_CTX=$$(mktemp -d); \
	cp cuga/Dockerfile.openshell         $$BUILD_CTX/Dockerfile; \
	mkdir -p                             $$BUILD_CTX/sandbox/cuga; \
	cp cuga/cuga-entrypoint.sh           $$BUILD_CTX/sandbox/cuga/; \
	cp ../pyproject.toml ../uv.lock ../README.md $$BUILD_CTX/; \
	cp -R ../src/.                               $$BUILD_CTX/src/; \
	echo "==> Building cuga-openshell image…"; \
	docker build -t cuga-openshell:local $$BUILD_CTX; \
	rm -rf $$BUILD_CTX; \
	PROVIDER=cuga-openai; \
	openshell workspace create --name $(OPENSHELL_WORKSPACE) >/dev/null 2>&1 || true; \
	openshell forward stop 7860 cuga-demo >/dev/null 2>&1 || true; \
	openshell forward stop 8001 cuga-demo >/dev/null 2>&1 || true; \
	openshell sandbox delete    cuga-demo >/dev/null 2>&1 || true; \
	if openshell provider get $$PROVIDER >/dev/null 2>&1; then \
	  openshell provider update $$PROVIDER \
	    --credential OPENAI_API_KEY \
	    --config "OPENAI_BASE_URL=$(OPENAI_BASE_URL)"; \
	else \
	  openshell provider create \
	    --name $$PROVIDER --type openai \
	    --credential OPENAI_API_KEY \
	    --config "OPENAI_BASE_URL=$(OPENAI_BASE_URL)"; \
	fi; \
	openshell inference set \
	  --provider $$PROVIDER --model $(MODEL_NAME) --timeout 300 --no-verify; \
	openshell sandbox create \
	  --name cuga-demo \
	  --from cuga-openshell:local \
	  --provider $$PROVIDER \
	  --policy $(RANCHER_CUGA)/cuga-policy.yaml \
	  --detach \
	  -- /usr/local/bin/cuga-entrypoint; \
	openshell forward start 7860 cuga-demo --background >/dev/null 2>&1 || \
	  echo "WARNING: could not forward UI port 7860"; \
	openshell forward start 8001 cuga-demo --background >/dev/null 2>&1 || \
	  echo "WARNING: could not forward registry port 8001"
	@echo "==> CUGA demo ready: http://localhost:7860"

# ===========================================================================
##@ Remote OpenShift (amd64, requires: oc login + REGISTRY)
# ===========================================================================

.PHONY: ocp-deploy
ocp-deploy: _check-oc _check-llm-vars _check-registry _check-icr-api-key ## Deploy full stack: sandbox stack then cuga.
	@$(MAKE) _ocp-deploy-sandbox \
	  NAMESPACE=$(NAMESPACE) OPENAI_API_KEY=$(OPENAI_API_KEY) \
	  OPENAI_BASE_URL=$(OPENAI_BASE_URL) MODEL_NAME=$(MODEL_NAME) \
	  REGISTRY=$(REGISTRY) IMAGE_TAG=$(IMAGE_TAG) ICR_API_KEY=$(ICR_API_KEY)
	@$(MAKE) _ocp-deploy-cuga \
	  NAMESPACE=$(NAMESPACE) OPENAI_API_KEY=$(OPENAI_API_KEY) \
	  OPENAI_BASE_URL=$(OPENAI_BASE_URL) MODEL_NAME=$(MODEL_NAME) \
	  REGISTRY=$(REGISTRY) IMAGE_TAG=$(IMAGE_TAG) ICR_API_KEY=$(ICR_API_KEY)

.PHONY: _ocp-deploy-sandbox
_ocp-deploy-sandbox: _check-oc _check-llm-vars _check-registry _check-docker-registry-login _check-icr-api-key
	@echo "==> Namespace $(NAMESPACE)"
	oc create namespace $(NAMESPACE) --dry-run=client -o yaml | oc apply -f -
	@# SCC permissions
	oc adm policy add-scc-to-user anyuid    -z openshell-gateway -n $(NAMESPACE) 2>/dev/null || true
	oc adm policy add-scc-to-user privileged -z default          -n $(NAMESPACE) 2>/dev/null || true
	oc adm policy add-scc-to-user anyuid    -z default           -n $(NAMESPACE) 2>/dev/null || true
	@# LLM secret (kept on re-run)
	oc get secret llm-credentials -n $(NAMESPACE) >/dev/null 2>&1 || \
	  oc create secret generic llm-credentials \
	    --from-literal=OPENAI_API_KEY=$(OPENAI_API_KEY) -n $(NAMESPACE)
	@# Sandbox API key secret (generated once)
	@if ! oc get secret sandbox-credentials -n $(NAMESPACE) >/dev/null 2>&1; then \
	  KEY=$$(openssl rand -hex 32); \
	  oc create secret generic sandbox-credentials \
	    --from-literal=SANDBOX_API_KEY=$$KEY -n $(NAMESPACE); \
	  echo "==> Generated SANDBOX_API_KEY — store securely: $$KEY"; \
	fi
	@# ICR pull secret — lets in-cluster builds pull from ICR
	@$(MAKE) _ocp-icr-pull-secret NAMESPACE=$(NAMESPACE) REGISTRY=$(REGISTRY) ICR_API_KEY=$(ICR_API_KEY)
	@# Mirror Docker Hub base images to ICR (avoids rate limits on in-cluster builds)
	@$(MAKE) _ocp-mirror-base NAMESPACE=$(NAMESPACE) REGISTRY=$(REGISTRY)
	@# Build sandbox-api image in-cluster (must exist before oc apply)
	oc get imagestream sandbox-api -n $(NAMESPACE) >/dev/null 2>&1 || \
	  oc create imagestream sandbox-api -n $(NAMESPACE)
	@if ! oc get buildconfig sandbox-api -n $(NAMESPACE) >/dev/null 2>&1; then \
	  oc new-build --name=sandbox-api --binary --strategy=docker -n $(NAMESPACE); \
	  oc patch buildconfig sandbox-api -n $(NAMESPACE) --type=merge -p \
	    '{"spec":{"strategy":{"dockerStrategy":{"dockerfilePath":"Dockerfile.sandbox-api.ocp","pullSecret":{"name":"icr-pull-secret"}}},"output":{"to":{"kind":"ImageStreamTag","name":"sandbox-api:latest"}}}}'; \
	else \
	  oc patch buildconfig sandbox-api -n $(NAMESPACE) --type=merge -p \
	    '{"spec":{"strategy":{"dockerStrategy":{"pullSecret":{"name":"icr-pull-secret"}}}}}' 2>/dev/null || true; \
	fi
	oc start-build sandbox-api --from-dir=sandbox --follow -n $(NAMESPACE)
	@# Build execd image in-cluster (must exist before gateway registers sandbox)
	@# Binary builds don't support --build-arg, so patch the Dockerfile in a tmpdir.
	oc get imagestream openshell-execd -n $(NAMESPACE) >/dev/null 2>&1 || \
	  oc create imagestream openshell-execd -n $(NAMESPACE)
	@if ! oc get buildconfig openshell-execd -n $(NAMESPACE) >/dev/null 2>&1; then \
	  oc new-build --name=openshell-execd --binary --strategy=docker -n $(NAMESPACE); \
	  oc patch buildconfig openshell-execd -n $(NAMESPACE) --type=merge -p \
	    '{"spec":{"strategy":{"dockerStrategy":{"dockerfilePath":"Dockerfile.execd.ocp","pullSecret":{"name":"icr-pull-secret"}}},"output":{"to":{"kind":"ImageStreamTag","name":"openshell-execd:latest"}}}}'; \
	else \
	  oc patch buildconfig openshell-execd -n $(NAMESPACE) --type=merge -p \
	    '{"spec":{"strategy":{"dockerStrategy":{"pullSecret":{"name":"icr-pull-secret"}}}}}' 2>/dev/null || true; \
	fi
	@BCTX=$$(mktemp -d); \
	cp -r sandbox/. $$BCTX/; \
	sed -i.bak 's|ARG EXECD_IMAGE=EXECD_BASE_DST|ARG EXECD_IMAGE=$(EXECD_BASE_DST)|' \
	  $$BCTX/Dockerfile.execd.ocp; \
	oc start-build openshell-execd --from-dir=$$BCTX --follow -n $(NAMESPACE); \
	rm -rf $$BCTX
	@# Apply kustomize manifests (gateway + sandbox-api) — images exist now
	@# rbac.yaml applied separately (without -n) because oc apply -k -n skips cluster-scoped resources
	@TMPDIR=$$(mktemp -d); \
	$(MAKE) _ocp-render SRC=$(OCP_SANDBOX) DST=$$TMPDIR NAMESPACE=$(NAMESPACE); \
	oc apply -f $$TMPDIR/manifests/rbac.yaml; \
	oc apply -k $$TMPDIR -n $(NAMESPACE); \
	rm -rf $$TMPDIR
	@# Restart gateway so it picks up new ClusterRole (tokenreviews) before sandbox create
	oc rollout restart deployment/openshell-gateway -n $(NAMESPACE)
	oc rollout status deployment/openshell-gateway -n $(NAMESPACE) --timeout=120s
	oc rollout status deployment/sandbox-api       -n $(NAMESPACE) --timeout=120s
	@# Register gateway + create execd sandbox via port-forward
	@$(MAKE) _ocp-register-gateway NAMESPACE=$(NAMESPACE)

.PHONY: _ocp-deploy-cuga
_ocp-deploy-cuga: _check-oc _check-llm-vars _check-registry _check-docker-registry-login _check-icr-api-key
	@echo "==> [1/4] Building cuga-openshell image → $(CUGA_IMAGE)"
	docker build \
	  --platform linux/amd64 \
	  -f cuga/Dockerfile.openshell \
	  -t $(CUGA_IMAGE) \
	  ..
	docker push $(CUGA_IMAGE)
	@echo "==> [2/4] Applying RBAC + Service + Route"
	@# Single oc apply -k covers: rbac.yaml + cuga-deployment.yaml (Service+Route) + cuga-policy ConfigMap
	@TMPDIR=$$(mktemp -d); \
	$(MAKE) _ocp-render SRC=$(OCP_CUGA) DST=$$TMPDIR NAMESPACE=$(NAMESPACE); \
	oc apply -k $$TMPDIR -n $(NAMESPACE); \
	rm -rf $$TMPDIR
	@# Link ICR pull secret to cuga-demo SA so the gateway-created pod can pull from ICR
	oc secrets link cuga-demo icr-pull-secret --for=pull -n $(NAMESPACE) 2>/dev/null || true
	@echo "==> [3/4] Registering inference route + creating cuga-demo sandbox via gateway"
	@$(MAKE) _ocp-register-cuga \
	  NAMESPACE=$(NAMESPACE) \
	  OPENAI_API_KEY=$(OPENAI_API_KEY) \
	  OPENAI_BASE_URL=$(OPENAI_BASE_URL) \
	  MODEL_NAME=$(MODEL_NAME)
	@echo "==> [4/4] Waiting for CUGA pod ($(OPENSHELL_WORKSPACE)--cuga-demo) to become Ready"
	@# Gateway names the pod "<workspace>--<sandbox>".
	@# Wait up to 5 minutes; probe every 5 s.
	@for i in $$(seq 1 60); do \
	  PHASE=$$(oc get pod $(OPENSHELL_WORKSPACE)--cuga-demo -n $(NAMESPACE) \
	    -o jsonpath='{.status.phase}' 2>/dev/null || echo ""); \
	  READY=$$(oc get pod $(OPENSHELL_WORKSPACE)--cuga-demo -n $(NAMESPACE) \
	    -o jsonpath='{.status.containerStatuses[0].ready}' 2>/dev/null || echo "false"); \
	  [ "$$READY" = "true" ] && break; \
	  echo "  waiting… ($$i/60) phase=$$PHASE"; sleep 5; \
	done; \
	[ "$$READY" = "true" ] || { echo "ERROR: $(OPENSHELL_WORKSPACE)--cuga-demo pod did not become Ready in 5m"; exit 1; }
	@# No Service-selector patch here: cuga-demo points at sandbox-proxy, which
	@# rewrites Host and forwards to the gateway.  Targeting the sandbox pod
	@# directly cannot work — its pod IP serves nothing (nested netns).
	@# Extend the HAProxy ingress timeout so long-running SSE streams (LLM
	@# responses) are not cut mid-flight by the default 30s route timeout.
	oc annotate route cuga-demo -n $(NAMESPACE) \
	  haproxy.router.openshift.io/timeout=300s \
	  --overwrite
	@echo
	@echo "==> CUGA ready"
	@echo "    UI: https://$$(oc get route cuga-demo -n $(NAMESPACE) \
	  -o jsonpath='{.spec.host}' 2>/dev/null || echo cuga-demo.$(NAMESPACE).svc)"

.PHONY: ocp-teardown
ocp-teardown: _check-oc ## Remove cuga then sandbox stack. Keeps PVC and namespace.
	@# Sandboxes first, while the gateway is still up to honour the delete.
	@# Their pods belong to no kustomization, so `oc delete -k` cannot reach them
	@# and they would linger as orphans after the gateway Deployment is gone.
	@$(MAKE) _ocp-delete-sandboxes NAMESPACE=$(NAMESPACE)
	@echo "==> Removing CUGA resources"
	@TMPDIR=$$(mktemp -d); \
	$(MAKE) _ocp-render SRC=$(OCP_CUGA) DST=$$TMPDIR NAMESPACE=$(NAMESPACE); \
	oc delete -k $$TMPDIR -n $(NAMESPACE) --ignore-not-found 2>/dev/null || true; \
	rm -rf $$TMPDIR
	@echo "==> Removing sandbox stack resources"
	@TMPDIR=$$(mktemp -d); \
	$(MAKE) _ocp-render SRC=$(OCP_SANDBOX) DST=$$TMPDIR NAMESPACE=$(NAMESPACE); \
	oc delete -k $$TMPDIR -n $(NAMESPACE) --ignore-not-found 2>/dev/null || true; \
	rm -rf $$TMPDIR
	@# ClusterRole/ClusterRoleBinding are cluster-scoped — not removed by namespace delete
	oc delete clusterrole    openshell-gateway-tokenreview-$(NAMESPACE) --ignore-not-found 2>/dev/null || true
	oc delete clusterrolebinding openshell-gateway-tokenreview-$(NAMESPACE) --ignore-not-found 2>/dev/null || true

.PHONY: ocp-wipe
ocp-wipe: _check-oc ## Remove everything including namespace and PVC (destructive).
	@# Delete the namespace — this removes every resource inside it in one shot.
	oc delete namespace $(NAMESPACE) --ignore-not-found 2>/dev/null || true
	@# ClusterRole/ClusterRoleBinding survive namespace deletion — remove explicitly
	oc delete clusterrole    openshell-gateway-tokenreview-$(NAMESPACE) --ignore-not-found 2>/dev/null || true
	oc delete clusterrolebinding openshell-gateway-tokenreview-$(NAMESPACE) --ignore-not-found 2>/dev/null || true
	openshell gateway remove openshell-ocp 2>/dev/null || true

.PHONY: _ocp-icr-pull-secret
_ocp-icr-pull-secret:
	@[ -n "$(ICR_API_KEY)" ] || { echo "ICR_API_KEY is not set — cannot create ICR pull secret"; exit 1; }
	@REGISTRY_HOST=$$(echo $(REGISTRY) | cut -d/ -f1); \
	if oc get secret icr-pull-secret -n $(NAMESPACE) >/dev/null 2>&1; then \
	  echo "==> ICR pull secret already present — skipping"; \
	else \
	  echo "==> Creating ICR pull secret in $(NAMESPACE)"; \
	  oc create secret docker-registry icr-pull-secret \
	    --docker-server=$$REGISTRY_HOST \
	    --docker-username=iamapikey \
	    --docker-password=$(ICR_API_KEY) \
	    -n $(NAMESPACE); \
	  oc secrets link default   icr-pull-secret --for=pull -n $(NAMESPACE); \
	  oc secrets link builder   icr-pull-secret --for=pull,mount -n $(NAMESPACE); \
	fi

# _ocp-mirror-base
# Mirrors Docker Hub base images to ICR so in-cluster oc start-build never
# hits the anonymous pull rate limit.  Idempotent — skips if already present.
.PHONY: _ocp-mirror-base
_ocp-mirror-base:
	@echo "==> Mirroring $(EXECD_BASE_SRC) → $(EXECD_BASE_DST)"
	@# Always pull linux/amd64 — the OCP cluster is amd64, and running this
	@# from a Mac (arm64) would otherwise mirror the wrong platform.
	@if docker manifest inspect $(EXECD_BASE_DST) >/dev/null 2>&1; then \
	  echo "    already present — skipping push"; \
	else \
	  docker pull --platform linux/amd64 $(EXECD_BASE_SRC) && \
	  docker tag  $(EXECD_BASE_SRC) $(EXECD_BASE_DST) && \
	  docker push $(EXECD_BASE_DST); \
	fi

# _ocp-delete-sandboxes
# Deletes the gateway-managed sandboxes (and with them their pods, Services and
# exposed service endpoints).  Best-effort: a missing gateway is not an error,
# because `ocp-wipe` removes the namespace outright and never needs this.
.PHONY: _ocp-delete-sandboxes
_ocp-delete-sandboxes:
	@command -v openshell >/dev/null 2>&1 || { \
	  echo "    openshell CLI not found — delete sandboxes manually"; exit 0; }
	@oc get deploy openshell-gateway -n $(NAMESPACE) >/dev/null 2>&1 || { \
	  echo "    gateway not running — nothing to delete"; exit 0; }
	@LOCAL_PORT=18080; \
	lsof -ti :$$LOCAL_PORT | xargs kill -9 2>/dev/null || true; \
	oc port-forward svc/openshell-gateway $$LOCAL_PORT:8080 -n $(NAMESPACE) & \
	PF_PID=$$!; \
	for i in $$(seq 1 15); do \
	  curl -s http://127.0.0.1:$$LOCAL_PORT >/dev/null 2>&1 && break; sleep 1; done; \
	echo "==> Deleting sandboxes (cuga-demo, code-exec)"; \
	openshell --gateway-endpoint http://127.0.0.1:$$LOCAL_PORT sandbox delete cuga-demo >/dev/null 2>&1 || true; \
	openshell --gateway-endpoint http://127.0.0.1:$$LOCAL_PORT sandbox delete code-exec >/dev/null 2>&1 || true; \
	kill $$PF_PID 2>/dev/null || true

.PHONY: _ocp-render
_ocp-render:
	@set -e; \
	src=$(SRC); dst=$(DST); \
	while IFS= read -r -d '' f; do \
	  rel=$${f#$$src/}; \
	  mkdir -p $$dst/$$(dirname $$rel); \
	  case "$$f" in \
	    *.yaml|*.toml) NAMESPACE=$(NAMESPACE) OPENSHELL_WORKSPACE=$(OPENSHELL_WORKSPACE) \
	                   envsubst '$$NAMESPACE $$OPENSHELL_WORKSPACE' < $$f > $$dst/$$rel ;; \
	    *)             cp $$f $$dst/$$rel ;; \
	  esac; \
	done < <(find $$src -type f -print0)

# ---------------------------------------------------------------------------

# _ocp-register-gateway
# Port-forwards to the gateway, registers it, and creates (or recreates) the
# code-exec sandbox.
#
# `openshell service expose` is what makes the sandbox reachable at all.  The
# supervisor puts the workload in a private network namespace nested inside the
# pod, so the pod IP does NOT expose it — a Service pointing at the sandbox pod
# gets connection-refused.  `service expose` registers a loopback port with the
# gateway, which then reverse-proxies to it, keyed on the Host header
# `<workspace>--<sandbox>--<service>.openshell.localhost`.
#
# `--forward` is NOT an alternative: it is a client-side tunnel that binds on
# the machine running the CLI, and it is a no-op with --detach.
.PHONY: _ocp-register-gateway
_ocp-register-gateway:
	@if ! command -v openshell >/dev/null 2>&1; then \
	  echo "    openshell CLI not found — register gateway manually"; exit 0; fi
	@LOCAL_PORT=18080; \
	lsof -ti :$$LOCAL_PORT | xargs kill -9 2>/dev/null || true; \
	oc port-forward svc/openshell-gateway $$LOCAL_PORT:8080 -n $(NAMESPACE) & \
	PF_PID=$$!; \
	for i in $$(seq 1 30); do \
	  curl -s http://127.0.0.1:$$LOCAL_PORT >/dev/null 2>&1 && break; sleep 1; done; \
	rm -f /tmp/execd-policy-*.yaml; \
	POLICY_TMP=$$(mktemp /tmp/execd-policy-XXXXXX.yaml); \
	NAMESPACE=$(NAMESPACE) envsubst '$$NAMESPACE' < $(OCP_SANDBOX)/execd-policy.yaml > $$POLICY_TMP \
	  || { echo "ERROR: failed to render execd-policy.yaml (src: $(OCP_SANDBOX)/execd-policy.yaml)"; kill $$PF_PID 2>/dev/null; exit 1; }; \
	[ -s $$POLICY_TMP ] \
	  || { echo "ERROR: rendered policy file is empty — check $(OCP_SANDBOX)/execd-policy.yaml"; kill $$PF_PID 2>/dev/null; exit 1; }; \
	openshell gateway remove openshell-ocp 2>/dev/null || true; \
	openshell gateway add http://127.0.0.1:$$LOCAL_PORT --name openshell-ocp; \
	openshell gateway select openshell-ocp >/dev/null; \
	openshell workspace create --name $(OPENSHELL_WORKSPACE) >/dev/null 2>&1 || true; \
	openshell sandbox delete code-exec >/dev/null 2>&1 || true; \
	openshell sandbox create \
	  --name code-exec \
	  --from $(EXECD_IMAGE) \
	  --policy $$POLICY_TMP \
	  --no-auto-providers \
	  --detach \
	  -- /usr/local/bin/execd-entrypoint \
	  || { echo "ERROR: sandbox create code-exec failed"; kill $$PF_PID 2>/dev/null; rm -f $$POLICY_TMP; exit 1; }; \
	rm -f $$POLICY_TMP; \
	openshell service expose code-exec 44772 execd \
	  || { echo "ERROR: service expose code-exec:44772 failed — sandbox may not have started yet"; kill $$PF_PID 2>/dev/null; exit 1; }; \
	echo "==> Gateway registered and execd sandbox created"; \
	kill $$PF_PID 2>/dev/null || true

# _ocp-register-cuga
# Port-forwards to the gateway, registers the inference provider, and creates
# (or recreates) the cuga-demo sandbox.  The gateway creates the pod in-cluster
# with the OpenShell supervisor as PID 1 — inference.local then resolves.
# Analogous to _ocp-register-gateway (which does the same for execd), including
# the `openshell service expose` calls that give the sandbox an inbound path:
#   7860 (ui)       Gradio UI, fronted by the cuga-demo Route
#   8001 (registry) tool-call registry, called back by code running in execd
.PHONY: _ocp-register-cuga
_ocp-register-cuga:
	@if ! command -v openshell >/dev/null 2>&1; then \
	  echo "    openshell CLI not found — register cuga-demo sandbox manually"; exit 0; fi
	@LOCAL_PORT=18080; \
	lsof -ti :$$LOCAL_PORT | xargs kill -9 2>/dev/null || true; \
	oc port-forward svc/openshell-gateway $$LOCAL_PORT:8080 -n $(NAMESPACE) & \
	PF_PID=$$!; \
	for i in $$(seq 1 30); do \
	  curl -s http://127.0.0.1:$$LOCAL_PORT >/dev/null 2>&1 && break; sleep 1; done; \
	openshell gateway remove openshell-ocp 2>/dev/null || true; \
	openshell gateway add http://127.0.0.1:$$LOCAL_PORT --name openshell-ocp; \
	openshell gateway select openshell-ocp >/dev/null; \
	PROVIDER=cuga-openai; \
	openshell provider get $$PROVIDER >/dev/null 2>&1 && \
	  openshell provider update $$PROVIDER \
	    --credential OPENAI_API_KEY \
	    --config "OPENAI_BASE_URL=$(OPENAI_BASE_URL)" || \
	  openshell provider create \
	    --name $$PROVIDER --type openai \
	    --credential OPENAI_API_KEY \
	    --config "OPENAI_BASE_URL=$(OPENAI_BASE_URL)"; \
	openshell inference set \
	  --provider $$PROVIDER --model $(MODEL_NAME) --timeout 300 --no-verify; \
	rm -f /tmp/cuga-policy-*.yaml; \
	POLICY_TMP=$$(mktemp /tmp/cuga-policy-XXXXXX.yaml); \
	NAMESPACE=$(NAMESPACE) envsubst '$$NAMESPACE' < $(OCP_CUGA)/cuga-policy.yaml > $$POLICY_TMP \
	  || { echo "ERROR: failed to render cuga-policy.yaml (src: $(OCP_CUGA)/cuga-policy.yaml)"; kill $$PF_PID 2>/dev/null; exit 1; }; \
	[ -s $$POLICY_TMP ] \
	  || { echo "ERROR: rendered policy file is empty — check $(OCP_CUGA)/cuga-policy.yaml"; kill $$PF_PID 2>/dev/null; exit 1; }; \
	openshell sandbox delete cuga-demo >/dev/null 2>&1 || true; \
	openshell sandbox create \
	  --name cuga-demo \
	  --from $(CUGA_IMAGE) \
	  --provider $$PROVIDER \
	  --policy $$POLICY_TMP \
	  --env "CUGA_EXECD_URL=http://execd-service.$(NAMESPACE).svc.cluster.local:44772" \
	  --env "CUGA_FUNCTION_CALL_HOST=http://cuga-demo.$(NAMESPACE).svc.cluster.local:8001" \
	  --env "CUGA_HOST=0.0.0.0" \
	  --env "DYNACONF_SERVER_PORTS__DEMO=7860" \
	  --env "CUGA_DBS_DIR=/tmp/cuga/dbs" \
	  --env "CUGA_LOGGING_DIR=/tmp/cuga/logging" \
	  --env "CUGA_FOLDER=/tmp/cuga/policies" \
	  --env "FASTEMBED_CACHE_PATH=/tmp/cuga/fastembed" \
	  --env "HF_HOME=/tmp/cuga/hf" \
	  --env "HF_HUB_OFFLINE=1" \
	  --detach \
	  -- /usr/local/bin/cuga-entrypoint \
	  || { echo "ERROR: sandbox create cuga-demo failed"; kill $$PF_PID 2>/dev/null; rm -f $$POLICY_TMP; exit 1; }; \
	rm -f $$POLICY_TMP; \
	openshell service expose cuga-demo 7860 ui \
	  || { echo "ERROR: service expose cuga-demo:7860 failed"; kill $$PF_PID 2>/dev/null; exit 1; }; \
	openshell service expose cuga-demo 8001 registry \
	  || { echo "ERROR: service expose cuga-demo:8001 failed"; kill $$PF_PID 2>/dev/null; exit 1; }; \
	echo "==> Gateway: inference registered and cuga-demo sandbox created"; \
	kill $$PF_PID 2>/dev/null || true

# ===========================================================================
##@ Smoke test
# ===========================================================================

.PHONY: smoke-test
smoke-test: ## Run end-to-end smoke test (works against both Rancher and OCP).
	@PROMPT=$${PROMPT:-"Write a Python script that prints 'hello from sandbox'"}; \
	bash smoke-test.sh "$$PROMPT"

# ===========================================================================
# Internal guards
# ===========================================================================

.PHONY: _check-oc
_check-oc:
	@command -v oc >/dev/null 2>&1 || { echo "oc not on PATH"; exit 127; }
	@oc whoami >/dev/null 2>&1 || { \
	  echo "Not logged in to OpenShift."; \
	  echo "  Run: oc login <api-url>   (credentials in team 1Password vault)"; \
	  exit 1; }

.PHONY: _check-llm-vars
_check-llm-vars:
	@[ -n "$(OPENAI_API_KEY)" ]  || { echo "OPENAI_API_KEY is not set";  exit 1; }
	@[ -n "$(OPENAI_BASE_URL)" ] || { echo "OPENAI_BASE_URL is not set"; exit 1; }
	@[ -n "$(MODEL_NAME)" ]      || { echo "MODEL_NAME is not set";      exit 1; }

.PHONY: _check-registry
_check-registry:
	@[ -n "$(REGISTRY)" ] || { \
	  echo "REGISTRY is not set."; \
	  echo "  Team default: REGISTRY=icr.io/automation-saas-platform-dev"; \
	  echo "  ICR login:    make ocp-login-icr ICR_API_KEY=<key> REGISTRY=<registry>"; \
	  exit 1; }

.PHONY: _check-docker-registry-login
_check-docker-registry-login:
	@REGISTRY_HOST=$$(echo $(REGISTRY) | cut -d/ -f1); \
	if ! docker system info >/dev/null 2>&1; then \
	  echo "Docker is not running"; exit 1; \
	fi; \
	if ! cat ~/.docker/config.json 2>/dev/null | grep -q "$$REGISTRY_HOST"; then \
	  echo "Not logged in to $$REGISTRY_HOST."; \
	  echo "  Run: make ocp-login-icr ICR_API_KEY=<key> REGISTRY=$(REGISTRY)"; \
	  exit 1; \
	fi

.PHONY: _check-icr-api-key
_check-icr-api-key:
	@[ -n "$(ICR_API_KEY)" ] || { \
	  echo "ICR_API_KEY is not set."; \
	  echo "  Needed to create pull secret for in-cluster builds."; \
	  echo "  Run: make ocp-deploy ICR_API_KEY=<key> ..."; \
	  exit 1; }

