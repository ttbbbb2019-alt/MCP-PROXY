from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from typing import Sequence, Tuple
import os
import select

from .config import ProxyConfig, load_config
from .framing import JsonRpcStream
from .proxy import ProxyRouter

_LOGGER = logging.getLogger(__name__)


class _WriterProtocol(asyncio.streams.FlowControlMixin):
    def __init__(self) -> None:
        asyncio.streams.FlowControlMixin.__init__(self)
        self.transport: asyncio.BaseTransport | None = None

    def connection_made(self, transport: asyncio.BaseTransport) -> None:  # pragma: no cover - stdlib interface
        self.transport = transport
        super().connection_made(transport)


async def _stdio_streams() -> Tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    loop = asyncio.get_running_loop()
    reader = asyncio.StreamReader()
    stdin_buffer = sys.stdin.buffer
    stdin_fd = stdin_buffer.fileno()

    def _read_from_stdin() -> bytes:
        try:
            select.select([stdin_fd], [], [])
            return os.read(stdin_fd, 8192)
        except OSError:
            return b""

    async def _pump_stdin() -> None:
        while True:
            data = await asyncio.to_thread(_read_from_stdin)
            if not data:
                reader.feed_eof()
                break
            preview = data[:200].decode("utf-8", errors="replace")
            _LOGGER.debug("Read %d bytes from client stdin: %s", len(data), preview)
            reader.feed_data(data)

    asyncio.create_task(_pump_stdin())

    writer_protocol = _WriterProtocol()
    transport, _ = await loop.connect_write_pipe(lambda: writer_protocol, sys.stdout.buffer)
    writer = asyncio.StreamWriter(transport, writer_protocol, reader, loop)
    return reader, writer


async def run_proxy(config_path: str) -> None:
    """Entry point that wires stdio to JsonRpcStream and starts the router."""
    config = load_config(config_path)
    logging.basicConfig(
        level=getattr(logging, config.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )
    reader, writer = await _stdio_streams()
    stream = JsonRpcStream(reader, writer, name="client")
    router = ProxyRouter(config, stream)
    await router.serve()


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Aggregating proxy for Model Context Protocol servers.")
    parser.add_argument(
        "--config",
        required=True,
        help="Path to the JSON config file describing downstream MCP servers.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> None:
    """CLI bootstrapper: parse args and run the async proxy loop."""
    parser = _build_arg_parser()
    args = parser.parse_args(argv)
    asyncio.run(run_proxy(args.config))


if __name__ == "__main__":  # pragma: no cover
    main()
