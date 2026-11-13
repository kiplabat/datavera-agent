"""python -m dva serve --port 0 [--data-dir DIR] [--resources DIR]

The token comes from the DVA_TOKEN environment variable (set by the desktop app), never the command line.
Prints `DVA_READY <port>` when listening, so the app knows where to connect."""
import argparse
import asyncio
import contextlib
import os
import socket
import sys


def main():
    for stream in (sys.stdout, sys.stderr):
        with contextlib.suppress(AttributeError, ValueError):
            stream.reconfigure(errors="replace", line_buffering=True)
    os.environ.setdefault("NO_PROXY", "127.0.0.1,localhost")
    parser = argparse.ArgumentParser(prog="dva")
    sub = parser.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("serve")
    s.add_argument("--port", type=int, default=0)
    s.add_argument("--data-dir")
    s.add_argument("--resources")
    args = parser.parse_args()
    if not os.environ.get("DVA_TOKEN"):
        raise SystemExit("DVA_TOKEN is not set")
    import uvicorn

    from .server import create_app
    sock = socket.socket()
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1) if os.name != "nt" else None
    sock.bind(("127.0.0.1", args.port))
    port = sock.getsockname()[1]
    app = create_app(args.data_dir, args.resources, os.environ["DVA_TOKEN"])
    config = uvicorn.Config(app, log_level="warning", access_log=False, timeout_graceful_shutdown=5)
    server = uvicorn.Server(config)

    async def run():
        task = asyncio.create_task(server.serve(sockets=[sock]))
        while not server.started:
            await asyncio.sleep(0.05)
            if task.done():
                break
        print(f"DVA_READY {port}", flush=True)
        await task
    if os.name == "nt":
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
    asyncio.run(run())


if __name__ == "__main__":
    main()
