"""Unified CLI entrypoint for local development and installed usage."""

from __future__ import annotations

import argparse


def build_parser() -> argparse.ArgumentParser:
    from aistudio_api.config import settings

    parser = argparse.ArgumentParser(description="AI Studio API entrypoint")
    subparsers = parser.add_subparsers(dest="command")

    server_parser = subparsers.add_parser("server", help="启动 Gemini 兼容 API 服务")
    server_parser.add_argument("--port", type=int, default=settings.port)
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    from aistudio_api.api.app import main as server_main
    from aistudio_api.config import settings

    port = getattr(args, "port", None)
    port_val = port if port is not None else settings.port
    server_main(["--port", str(port_val)])


if __name__ == "__main__":
    main()
