"""The panel has no login: it listens on 127.0.0.1 and is opened through an SSH tunnel. On top of that it
refuses foreign Host headers (DNS rebinding), POSTs from other sites (CSRF) and being framed, so a web
page the owner happens to visit can't pause a bot or rewrite its persona."""

from collections.abc import Awaitable, Callable
from urllib.parse import urlsplit

from fastapi import Request
from fastapi.responses import PlainTextResponse, Response

LOOPBACK = frozenset({"localhost", "127.0.0.1", "::1"})
SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'self'; img-src 'self' data:; frame-ancestors 'none'; form-action 'self'; base-uri 'none'"
    ),
    "X-Frame-Options": "DENY",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
}

type CallNext = Callable[[Request], Awaitable[Response]]
type Middleware = Callable[[Request, CallNext], Awaitable[Response]]


def hostname(host: str) -> str:
    """The name in a Host header: "localhost:8090" -> "localhost", "[::1]:8090" -> "::1"."""
    host = host.strip().lower()
    if host.startswith("["):
        return host[1:].partition("]")[0]
    return host.partition(":")[0]


def allowed_hosts(web_host: str) -> frozenset[str]:
    """The loopback names, plus WEB_HOST itself unless it listens on every address."""
    return LOOPBACK | ({hostname(web_host)} - {"", "0.0.0.0", "::"})


def from_other_site(request: Request) -> bool:
    site = request.headers.get("sec-fetch-site")
    if site is not None:
        return site not in ("same-origin", "none")
    origin = request.headers.get("origin")  # older browsers: no Sec-Fetch-Site, but an Origin on POST
    return origin is not None and urlsplit(origin).netloc != request.headers.get("host")


def local_path(value: str) -> str | None:
    """`value` if it is a path on this site: redirects must not lead elsewhere."""
    return value if value.startswith("/") and not value.startswith("//") and "\\" not in value else None


def local_only(hosts: frozenset[str]) -> Middleware:
    """HTTP middleware: answer requests for `hosts` only, refuse cross-site writes, never be framed."""

    async def guard(request: Request, call_next: CallNext) -> Response:
        if hostname(request.headers.get("host", "")) not in hosts:
            response: Response = PlainTextResponse(
                "Панель открывается только через SSH-туннель, по адресу localhost.", status_code=400
            )
        elif request.method not in ("GET", "HEAD") and from_other_site(request):
            response = PlainTextResponse("Запрос пришёл с другого сайта и отклонён.", status_code=403)
        else:
            response = await call_next(request)
        response.headers.update(SECURITY_HEADERS)
        return response

    return guard
