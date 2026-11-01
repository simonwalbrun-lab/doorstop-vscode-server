import argparse
import os
import socket
import threading

import uvicorn

from doorstop_server.app import create_app
from doorstop_server.config import Settings


def parse_args(argv=None) -> tuple[Settings, bool]:
    parser = argparse.ArgumentParser(prog="doorstop-vscode-server")
    parser.add_argument("--project", required=True, help="path to the root of the Doorstop project")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=7867, help="0 picks a free port")
    parser.add_argument(
        "--exit-on-stdin-eof",
        action="store_true",
        help="exit when stdin closes; the extension uses this so the server dies with it",
    )
    args = parser.parse_args(argv)
    return Settings(project_root=args.project, host=args.host, port=args.port), args.exit_on_stdin_eof


def _exit_when_closed(stdin) -> None:
    # The OS closes the pipe when the parent dies, however it dies.
    stdin.read()
    os._exit(0)


def main(argv=None) -> None:
    settings, exit_on_stdin_eof = parse_args(argv)
    if exit_on_stdin_eof:
        # Watch a private copy and leave NUL as stdin: on Windows a child process (the
        # validation worker) hangs at startup while a read on a pipe it inherited is pending.
        stdin = os.fdopen(os.dup(0), "rb")
        os.dup2(os.open(os.devnull, os.O_RDONLY), 0)
        threading.Thread(target=_exit_when_closed, args=(stdin,), daemon=True).start()
    app = create_app(settings)
    family = socket.AF_INET6 if ":" in settings.host else socket.AF_INET
    # Bound here so the real port is known with --port 0; one parseable line the extension waits for.
    sock = socket.create_server((settings.host, settings.port), family=family)
    print(f"DOORSTOP_PORT={sock.getsockname()[1]}", flush=True)
    # workers=1, no reload: exactly one process, one event loop -- required for
    # the request-serialization guarantee in lock.py to hold.
    config = uvicorn.Config(app, host=settings.host, port=settings.port, workers=1, reload=False)
    uvicorn.Server(config).run(sockets=[sock])


if __name__ == "__main__":
    main()
