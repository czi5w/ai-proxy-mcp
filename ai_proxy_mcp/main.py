"""Entry point: run the WS server (for AI_Proxy reverse connections) and the
MCP HTTP server (for Hermes Agent tool calls) in a single asyncio event loop.
"""
from __future__ import annotations

import asyncio
import logging
import signal
import sys

import uvicorn

from .config import Config, configure_logging
from .device_pool import DevicePool
from .mcp_server import build_mcp_app
from .ws_server import Bridge, serve_ws

logger = logging.getLogger("ai_proxy_mcp")


async def _run(cfg: Config) -> int:
    pool = DevicePool()
    bridge = Bridge(pool, task_timeout=cfg.task_timeout_seconds)

    ws_server = await serve_ws(cfg, pool)

    mcp_app = build_mcp_app(bridge)
    uv_config = uvicorn.Config(
        mcp_app,
        host=cfg.mcp_host,
        port=cfg.mcp_port,
        log_config=None,  # use root logging configured in configure_logging
        access_log=False,
        lifespan="on",
    )
    uv_server = uvicorn.Server(uv_config)

    logger.info(
        "MCP HTTP server listening on http://%s:%d%s",
        cfg.mcp_host,
        cfg.mcp_port,
        cfg.mcp_path,
    )

    stop_event = asyncio.Event()

    def _shutdown(signame: str) -> None:
        logger.info("received %s, shutting down", signame)
        stop_event.set()
        uv_server.should_exit = True

    loop = asyncio.get_running_loop()
    for sig in ("SIGINT", "SIGTERM"):
        if hasattr(signal, sig):
            try:
                loop.add_signal_handler(
                    getattr(signal, sig), _shutdown, sig
                )
            except NotImplementedError:
                # Windows: signals can't be installed via add_signal_handler
                pass

    try:
        await uv_server.serve()
    finally:
        logger.info("closing WS server")
        ws_server.close()
        await ws_server.wait_closed()

    return 0


def cli() -> int:
    cfg = Config.load()
    configure_logging(cfg.log_level)
    logger.info(
        "starting ai-proxy-mcp (ws=%s:%d, mcp=%s:%d%s)",
        cfg.ws_host,
        cfg.ws_port,
        cfg.mcp_host,
        cfg.mcp_port,
        cfg.mcp_path,
    )

    try:
        return asyncio.run(_run(cfg))
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(cli())
