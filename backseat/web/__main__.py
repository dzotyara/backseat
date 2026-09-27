"""python -m backseat.web — the owner's panel on WEB_HOST:WEB_PORT (127.0.0.1:8090 by default).
It has no login: open it through an SSH tunnel, e.g. ssh -L 8090:127.0.0.1:8090 <server>."""

import logging

import uvicorn

from backseat.logs import setup_logging
from backseat.web.app import create_app
from backseat.web.settings import WebSettings

log = logging.getLogger("backseat.web")


def main() -> None:
    settings = WebSettings()
    setup_logging("INFO")
    if settings.web_host not in ("127.0.0.1", "localhost", "::1"):
        # Inside docker it has to be 0.0.0.0; then publish the port as 127.0.0.1:8090 only.
        log.warning(
            "WEB_HOST=%s: the panel has no login, keep its port reachable only from localhost", settings.web_host
        )
    log.info("Backseat panel on http://%s:%s", settings.web_host, settings.web_port)
    uvicorn.run(
        create_app(settings), host=settings.web_host, port=settings.web_port, log_config=None, server_header=False
    )


if __name__ == "__main__":
    main()
