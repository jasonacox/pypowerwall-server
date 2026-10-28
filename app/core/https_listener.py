"""Optional HTTPS listener (PW_HTTPS=yes): a second uvicorn server for the same
app, started from the lifespan so it works however the server is launched.
Startup failures are logged; HTTP keeps serving.
"""

import asyncio
import contextlib
import logging
from typing import Iterator, Optional, Tuple

import uvicorn
from fastapi import FastAPI

from app.config import Settings
from app.core.tls import certificate_files

Listener = Tuple[uvicorn.Server, asyncio.Task]
_START_TIMEOUT = 10.0  # seconds

logger = logging.getLogger(__name__)


class _EmbeddedServer(uvicorn.Server):
    """A uvicorn server that runs inside another one's event loop."""

    @contextlib.contextmanager
    def capture_signals(self) -> Iterator[None]:
        # The main server owns SIGINT/SIGTERM and stops this one on shutdown
        yield


async def start_https_listener(app: FastAPI, settings: Settings) -> Optional[Listener]:
    """Start when PW_HTTPS=yes: (server, task), or None if off or failed."""
    if not settings.https_mode:
        return None
    # PW_PORT is the HTTP port for Docker and the CLI. Running uvicorn by hand
    # with --port needs PW_PORT set to match: the app can't see --port.
    if settings.https_port == settings.server_port:
        logger.error(
            "HTTPS not started: PW_HTTPS_PORT (%s) is the same as PW_PORT",
            settings.https_port,
        )
        return None
    try:
        certfile, keyfile = certificate_files(settings.https_cert, settings.https_key)
        config = uvicorn.Config(
            app,
            host=settings.server_host,
            port=settings.https_port,
            ssl_certfile=certfile,
            ssl_keyfile=keyfile,
            lifespan="off",  # startup/shutdown run once, in the main server
            access_log=settings.debug,
            log_config=None,  # keep the logging the main server set up
        )
        config.load()  # validates the certificate and key
        # Bind here, not in serve(): on a bind error uvicorn calls sys.exit(),
        # which inside a task would stop the whole event loop (and HTTP)
        sock = config.bind_socket()
    except (Exception, SystemExit) as e:
        logger.error("HTTPS not started on port %s: %s", settings.https_port, e)
        return None
    server = _EmbeddedServer(config)
    task = asyncio.create_task(server.serve(sockets=[sock]))
    deadline = asyncio.get_running_loop().time() + _START_TIMEOUT
    while not server.started and not task.done():
        if asyncio.get_running_loop().time() > deadline:
            server.should_exit = True
            break
        await asyncio.sleep(0.05)
    if not server.started:
        with contextlib.suppress(Exception):
            await asyncio.wait_for(task, timeout=_START_TIMEOUT)
        sock.close()
        logger.error("HTTPS not started on port %s", settings.https_port)
        return None
    logger.info(
        "HTTPS listening on %s:%s", settings.server_host, settings.https_port
    )
    return server, task


async def stop_https_listener(listener: Optional[Listener]) -> None:
    """Stop the HTTPS listener started by start_https_listener()."""
    if not listener:
        return
    server, task = listener
    server.should_exit = True
    with contextlib.suppress(Exception):
        await asyncio.wait_for(task, timeout=10)
