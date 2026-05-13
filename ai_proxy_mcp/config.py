"""Environment-driven configuration."""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()


def _int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError:
        raise SystemExit(f"[config] {name} must be an integer, got: {raw!r}")


def _str(name: str, default: str) -> str:
    return os.getenv(name) or default


@dataclass(frozen=True)
class Config:
    ws_host: str
    ws_port: int
    mcp_host: str
    mcp_port: int
    mcp_path: str
    task_timeout_seconds: int
    register_timeout_seconds: int
    log_level: str

    @classmethod
    def load(cls) -> "Config":
        return cls(
            ws_host=_str("WS_HOST", "0.0.0.0"),
            ws_port=_int("WS_PORT", 8765),
            mcp_host=_str("MCP_HOST", "127.0.0.1"),
            mcp_port=_int("MCP_PORT", 8766),
            mcp_path=_str("MCP_PATH", "/mcp"),
            task_timeout_seconds=_int("TASK_TIMEOUT_SECONDS", 600),
            register_timeout_seconds=_int("REGISTER_TIMEOUT_SECONDS", 10),
            log_level=_str("LOG_LEVEL", "INFO").upper(),
        )


def configure_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
