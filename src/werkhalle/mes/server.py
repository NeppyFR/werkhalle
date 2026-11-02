"""Process entry of the MES: arguments, logging, uvicorn on the one event loop."""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys

import uvicorn

from .app import create_app
from .runtime import Runtime

log = logging.getLogger("werkhalle.mes")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="python -m werkhalle.mes", description="Werkhalle MES: spricht nur per OPC UA mit der Anlage")
    parser.add_argument("--plant-url", default="opc.tcp://127.0.0.1:4840/werkhalle/")
    parser.add_argument("--http-port", type=int, default=8321)
    parser.add_argument("--data-dir", default="data")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--solver-limit", type=float, default=8.0)
    parser.add_argument("--no-auto-supply", action="store_true")
    return parser.parse_args(argv)


def configure_logging() -> None:
    logging.basicConfig(stream=sys.stderr, level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("asyncua").setLevel(logging.WARNING)
    # asyncua logs a harmless "Publish iteration crashed" traceback at ERROR on every connection loss; the client reports the loss itself.
    logging.getLogger("asyncua.client.ua_session").setLevel(logging.CRITICAL)
    logging.getLogger("asyncua.client.ua_client.UASocketProtocol").setLevel(logging.CRITICAL)


async def serve(args: argparse.Namespace) -> None:
    runtime = Runtime(
        plant_url=args.plant_url,
        data_dir=args.data_dir,
        seed=args.seed,
        solver_limit_s=args.solver_limit,
        auto_supply=not args.no_auto_supply,
    )
    config = uvicorn.Config(create_app(runtime), host="127.0.0.1", port=args.http_port, log_level="warning", access_log=False,
                            lifespan="on", timeout_graceful_shutdown=2)
    server = uvicorn.Server(config)
    log.info("MES startet: http://127.0.0.1:%s/ Anlage=%s", args.http_port, args.plant_url)
    await server.serve()
    if not server.started:
        # uvicorn logs a port conflict and returns instead of raising; a launcher has to see that as a failure.
        raise SystemExit(1)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    configure_logging()
    code = 0
    try:
        asyncio.run(serve(args))
    except KeyboardInterrupt:
        pass
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else 1
    except Exception:
        log.exception("MES abgebrochen")
        code = 1
    logging.shutdown()
    sys.stdout.flush()
    sys.stderr.flush()
    # The executor's atexit hook would wait for a solve in a worker. The workers watch this process and end with it.
    os._exit(code)
