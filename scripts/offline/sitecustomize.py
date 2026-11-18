"""Record and prevent external DNS and socket requests during offline verification."""
import sys

def audit(event, args):
    if event == "socket.getaddrinfo":
        host = args[0]
    elif event in {"socket.connect", "socket.sendto"} and isinstance(args[1], tuple):
        host = args[1][0]
    elif event in {"socket.gethostbyname", "socket.gethostbyaddr"}:
        host = args[0]
    else:
        return
    if host not in {None, "127.0.0.1", "localhost", "::1"}:
        print(f"OFFLINE BLOCK {event} {host!r}", file=sys.stderr, flush=True)
        raise RuntimeError("Non-loopback networking in offline verification")

sys.addaudithook(audit)
print("Offline socket audit installed", file=sys.stderr, flush=True)
