"""Local API protection (Phase 3): Host allowlist, loopback-peer check, Origin / fetch-metadata checks and a per-launch token.

Pure ASGI middleware that runs before routing and before any request body is read, so a rejected request cannot touch
plans, files or the database. Order of checks for every HTTP request (all rejections are generic and carry no secret):

  1. the peer address must be loopback            (defence in depth if the server were ever bound beyond 127.0.0.1)
  2. exactly one Host header, on the allowlist    (blocks DNS rebinding and forged Host values)
  3. Origin, when sent, must be a LazyDesk frontend origin; Sec-Fetch-Site, when sent, must be same-origin or same-site
  4. /api/health is public and minimal; /api/session/bootstrap additionally REQUIRES an allowed Origin
  5. everything else needs the per-launch token in the X-LazyDesk-Token header (constant-time comparison)

CORS is not the authorization mechanism: it only lets the legitimate frontend read responses. The token lives in process
memory and, after bootstrap, in the frontend's JavaScript memory. Other processes running as the same user are outside
the protection boundary (they can forge Origin and read process memory)."""
from __future__ import annotations

import hmac
import json
import logging
import re
import secrets
from dataclasses import dataclass, field

TOKEN_HEADER = "X-LazyDesk-Token"
BOOTSTRAP_PATH = "/api/session/bootstrap"
HEALTH_PATH = "/api/health"
LOOPBACK_PEERS = ("127.0.0.1", "::1")
SAFE_FETCH_SITES = ("same-origin", "same-site")
DEFAULT_BACKEND_PORT = 8000
DEFAULT_FRONTEND_PORT = 5173

_HOST_PATTERN = re.compile(r"^(127\.0\.0\.1|localhost|\[::1\])(?::([0-9]{1,5}))?$")


class ConfigurationError(ValueError):
    """An unsupported port setting. Raised at startup, so the backend refuses to run rather than silently using another port."""


def parse_port(value: str | None, default: int, name: str = "port") -> int:
    if value is None:
        return default
    text = str(value).strip()
    if not re.fullmatch(r"[0-9]{1,5}", text) or not 1 <= int(text) <= 65535:
        raise ConfigurationError(f"{name} must be a whole number from 1 to 65535.")
    return int(text)


@dataclass
class Guard:
    """Configuration and per-launch secret. A new process (or a new Guard) always has a new token."""
    backend_port: int = DEFAULT_BACKEND_PORT
    frontend_port: int = DEFAULT_FRONTEND_PORT
    token: str = field(default_factory=lambda: secrets.token_urlsafe(32), repr=False)
    extra_peers: tuple[str, ...] = ()   # explicit test configuration only; never read from the environment

    @classmethod
    def from_environment(cls, env: dict[str, str]) -> "Guard":
        return cls(parse_port(env.get("LAZYDESK_PORT"), DEFAULT_BACKEND_PORT, "LAZYDESK_PORT"),
                   parse_port(env.get("LAZYDESK_FRONTEND_PORT"), DEFAULT_FRONTEND_PORT, "LAZYDESK_FRONTEND_PORT"))

    @property
    def allowed_origins(self) -> tuple[str, ...]:
        return tuple(f"http://{host}:{self.frontend_port}" for host in ("127.0.0.1", "localhost", "[::1]"))

    def host_allowed(self, value: str) -> bool:
        match = _HOST_PATTERN.fullmatch(value.strip().lower())
        if not match:
            return False
        port = match.group(2)
        return port is not None and port == str(int(port)) and int(port) == self.backend_port

    def origin_allowed(self, value: str) -> bool:
        return value in self.allowed_origins   # exact: browsers send the origin already lower-cased

    def peer_allowed(self, peer: object) -> bool:
        host = peer[0] if isinstance(peer, (tuple, list)) and peer else None
        return host in LOOPBACK_PEERS or host in self.extra_peers

    def token_matches(self, supplied: bytes | None) -> bool:
        if not supplied:
            return False
        return hmac.compare_digest(supplied, self.token.encode("ascii"))


def _headers(scope: dict, name: str) -> list[str]:
    wanted = name.lower().encode("latin-1")
    return [value.decode("latin-1") for key, value in scope.get("headers", []) if key.lower() == wanted]


class ApiGuardMiddleware:
    def __init__(self, app, get_guard):
        self.app = app
        self.get_guard = get_guard

    async def __call__(self, scope, receive, send):
        if scope["type"] == "lifespan":
            return await self.app(scope, receive, send)
        if scope["type"] != "http":  # no websockets here
            if scope["type"] == "websocket":
                await send({"type": "websocket.close", "code": 1008})
            return
        guard: Guard = self.get_guard()
        origin_values = _headers(scope, "origin")
        origin = origin_values[0] if len(origin_values) == 1 else None
        origin_ok = origin is not None and guard.origin_allowed(origin)

        async def reject(status: int, code: str, message: str):
            await self._respond(send, status, code, message, origin if origin_ok else None)

        if not guard.peer_allowed(scope.get("client")):
            return await reject(403, "request_rejected", "Request rejected.")
        hosts = _headers(scope, "host")
        if len(hosts) != 1 or not guard.host_allowed(hosts[0]):
            self._hint_port_mismatch(guard, scope, hosts)
            return await reject(400, "request_rejected", "Request rejected.")
        if len(origin_values) > 1 or (origin_values and not origin_ok):
            return await reject(403, "request_rejected", "Request rejected.")
        sites = _headers(scope, "sec-fetch-site")
        if sites and (len(sites) > 1 or sites[0].strip().lower() not in SAFE_FETCH_SITES):
            return await reject(403, "request_rejected", "Request rejected.")

        path, method = scope.get("path", ""), scope.get("method", "GET")
        if method == "OPTIONS":      # CORS preflight carries no credentials; the CORS layer answers it
            return await self.app(scope, receive, send)
        if path == HEALTH_PATH and method in ("GET", "HEAD"):
            return await self.app(scope, receive, send)
        if path == BOOTSTRAP_PATH:
            if method != "POST" or not origin_ok:   # the token is only handed to a page served by a LazyDesk frontend origin
                return await reject(403, "request_rejected", "Request rejected.")
            return await self.app(scope, receive, send)
        supplied = _headers(scope, TOKEN_HEADER)
        if len(supplied) != 1 or not guard.token_matches(supplied[0].strip().encode("latin-1", "replace")):
            return await reject(401, "authorization_required", "Authorization required.")
        return await self.app(scope, receive, send)

    _warned = False

    def _hint_port_mismatch(self, guard: Guard, scope: dict, hosts: list[str]) -> None:
        """One log line (no secrets) when the server listens on a different port than LAZYDESK_PORT, the usual cause of 400s."""
        server = scope.get("server")
        if self._warned or len(hosts) != 1 or not isinstance(server, (tuple, list)) or len(server) < 2:
            return
        match = _HOST_PATTERN.fullmatch(hosts[0].strip().lower())
        if match and match.group(2) and int(match.group(2)) == server[1] != guard.backend_port:
            self._warned = True
            logging.getLogger("lazydesk.guard").warning(
                "Requests are arriving on port %s but LAZYDESK_PORT is %s, so they are rejected. Start the backend with the same port in both.",
                server[1], guard.backend_port)

    @staticmethod
    async def _respond(send, status: int, code: str, message: str, cors_origin: str | None):
        body = json.dumps({"detail": {"message": message, "code": code}}).encode()
        headers = [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode()), (b"cache-control", b"no-store")]
        if cors_origin:
            headers += [(b"access-control-allow-origin", cors_origin.encode("latin-1")), (b"vary", b"Origin")]
        await send({"type": "http.response.start", "status": status, "headers": headers})
        await send({"type": "http.response.body", "body": body})
