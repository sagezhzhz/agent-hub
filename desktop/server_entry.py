"""Private backend owned by the native macOS application, including its lifetime."""

import argparse
import json
import os
from pathlib import Path
import signal
import threading

os.environ.setdefault("AGENT_HUB_DATA_DIR", str(Path.home() / "Library/Application Support/Agent Hub"))

import agent_hub


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--parent-pid", type=int, default=os.getppid())
    parser.add_argument("--port", type=int, default=47322)
    parser.add_argument("--demo", action="store_true")
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    if args.check:
        return agent_hub.check()

    # Never attach to a terminal server: the desktop app owns only its own process.
    ports = [0] if args.port == 0 else [*range(args.port, min(args.port + 20, 65536)), 0]
    for port in ports:
        try:
            server = agent_hub.ThreadingHTTPServer((agent_hub.HOST, port), agent_hub.Handler)
            break
        except OSError:
            if port == 0:
                raise
    agent_hub.PORT = server.server_port
    server.daemon_threads = True
    finished = threading.Event()
    stopping = threading.Event()

    def stop(*_):
        if not stopping.is_set():
            stopping.set()
            threading.Thread(target=server.shutdown, daemon=True).start()

    def watch_parent():
        while not finished.wait(0.5):
            # Reparenting catches a native app crash, even if its PID is reused.
            if os.getppid() != args.parent_pid:
                stop()
                return

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    threading.Thread(target=watch_parent, daemon=True).start()
    print(json.dumps({"app": "agent-hub-desktop", "port": server.server_port, "pid": os.getpid()}), flush=True)
    try:
        server.serve_forever(poll_interval=0.2)
    finally:
        finished.set()
        server.server_close()


if __name__ == "__main__":
    main()
