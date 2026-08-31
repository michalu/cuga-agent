"""TCP relay that makes a sandbox service reachable from other sandboxes.

OpenShell sandboxes are unreachable from outside: the supervisor puts the
workload in a private network namespace, so the sandbox container's own IP does
not expose it. The only inbound path is `openshell forward`, which binds on the
*host* — and on Rancher Desktop the host is macOS, which containers in the VM
cannot reach directly.

This relay closes that gap. Run it in the VM with --network host; it listens on
the VM's bridge and forwards to the host's forwarded port:

    sandbox A  ->  host.openshell.internal:PORT   (policy-checked egress)
               ->  this relay (in the VM)
               ->  HOST_NAME:PORT                 (the host, openshell forward)
               ->  gateway -> sandbox B

Both directions need it: CUGA calls execd on 44772, and the code execd runs
calls CUGA's tool registry back on 8001.

Needed only on Docker. On Kubernetes pods have addresses and this disappears.
"""

import os
import socket
import threading

# Ports are bridged one-for-one: a sandbox dialling host.openshell.internal:P
# reaches whatever `openshell forward` published on the host's port P.
PORTS = [int(p) for p in os.environ.get("RELAY_PORTS", "44772").split(",") if p.strip()]
HOST_NAME = os.environ.get("RELAY_TARGET_HOST", "host.rancher-desktop.internal")
# Bind the bridge address, not 0.0.0.0. Sandboxes resolve
# host.openshell.internal to the bridge gateway, so this is the only address
# they need — and Rancher Desktop republishes 0.0.0.0 listeners onto the macOS
# host, which would seize the very ports `openshell forward` has to bind and
# leave the relay pointing back at itself.
BIND = os.environ.get("RELAY_BIND", "172.17.0.1")


def close(sock):
    try:
        sock.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass
    try:
        sock.close()
    except OSError:
        pass


def pump(src, dst):
    try:
        while True:
            data = src.recv(65536)
            if not data:
                break
            dst.sendall(data)
    finally:
        close(src)
        close(dst)


def handle(client, addr, port):
    try:
        upstream = socket.create_connection((HOST_NAME, port), timeout=10)
        client.settimeout(None)
        upstream.settimeout(None)
        threading.Thread(target=pump, args=(client, upstream), daemon=True).start()
        pump(upstream, client)
    except Exception as exc:
        print(f"relay error from {addr} on port {port}: {exc!r}", flush=True)
        close(client)


def serve(port):
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind((BIND, port))
    server.listen(128)
    print(f"relaying {BIND}:{port} -> {HOST_NAME}:{port}", flush=True)
    while True:
        client, addr = server.accept()
        threading.Thread(target=handle, args=(client, addr, port), daemon=True).start()


threads = [threading.Thread(target=serve, args=(p,), daemon=True) for p in PORTS]
for t in threads:
    t.start()
for t in threads:
    t.join()
