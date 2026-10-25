"""Phase 3: local API protection. Every client here is built through `raw_client_factory` (no default Host or token) or the
low-level ASGI helper, so the production behaviour of the guard is what is under test. Synthetic temp databases only."""
import asyncio
import json
import logging
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.routing import APIRoute

from app import api_guard, main
from app.api_guard import BOOTSTRAP_PATH, HEALTH_PATH, TOKEN_HEADER, Guard

BACKEND = Path(__file__).resolve().parent.parent
REPO = BACKEND.parent
GOOD_HOST = "127.0.0.1:8000"
GOOD_ORIGIN = "http://127.0.0.1:5173"
GENERIC = {"request_rejected", "authorization_required"}


def fill(path: str) -> str:
    return re.sub(r"\{[^}]+\}", "x", path)


ALL_ROUTES = sorted({(method, fill(route.path)) for route in main.app.routes if isinstance(route, APIRoute) for method in route.methods if method != "HEAD"})
PUBLIC = {("GET", HEALTH_PATH), ("POST", BOOTSTRAP_PATH)}
PROTECTED = [route for route in ALL_ROUTES if route not in PUBLIC]
IDS = lambda routes: [f"{m} {p}" for m, p in routes]  # noqa: E731


@pytest.fixture
def client(raw_client_factory):
    return raw_client_factory()


def call(client, method, path, host=GOOD_HOST, token=None, origin=None, **extra):
    headers = {"host": host, **extra.pop("headers", {})}
    if token is not None:
        headers[TOKEN_HEADER] = token
    if origin is not None:
        headers["origin"] = origin
    if method in ("POST", "PATCH"):
        extra.setdefault("json", {})
    return client.request(method, path, headers=headers, **extra)


def guard_rejection(response) -> bool:
    body = response.json() if response.headers.get("content-type", "").startswith("application/json") else {}
    detail = body.get("detail") if isinstance(body, dict) else None
    return response.status_code in (400, 401, 403) and isinstance(detail, dict) and detail.get("code") in GENERIC


def asgi(method, path, headers, client=("127.0.0.1", 5000), query=b""):
    sent: list[dict] = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        sent.append(message)

    scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": method, "path": path, "raw_path": path.encode(),
             "query_string": query, "root_path": "", "scheme": "http", "client": client, "server": ("127.0.0.1", 8000),
             "headers": [(k.lower().encode("latin-1"), v.encode("latin-1")) for k, v in headers]}
    asyncio.run(main.app(scope, receive, send))
    start = next(m for m in sent if m["type"] == "http.response.start")
    body = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    return start["status"], body, {k.decode().lower(): v.decode() for k, v in start["headers"]}


def database_fingerprint() -> list:
    if not main.DB_PATH.exists():
        return ["<no database file>"]
    connection = main.db()
    try:
        return [(t, [tuple(r) for r in connection.execute(f"SELECT * FROM {t} ORDER BY 1")]) for t in ("plans", "activity", "benchmarks", "settings")]
    finally:
        connection.close()


# ---------------------------------------------------------------------------------------------------------------------------
# Host validation
# ---------------------------------------------------------------------------------------------------------------------------

FORGED_HOSTS = [
    "evil.com", "evil.com:8000", "127.0.0.1.evil.com:8000", "localhost.evil.com:8000", "evilhost:8000", "evil127.0.0.1:8000",
    "127.0.0.1", "localhost", "[::1]",                       # no port: browsers always send the real port, the default is not ours
    "127.0.0.1:8001", "127.0.0.1:80", "127.0.0.1:08000", "127.0.0.1:+8000", "127.0.0.1:8000.", "127.0.0.1:8 000", "127.0.0.1:",
    "localhost.:8000", "localhost:8000:8000", "[::1]x:8000", "[::2]:8000", "0.0.0.0:8000", "127.0.0.2:8000", "2130706433:8000", "0x7f.0.0.1:8000",
    "127.0.0.1:8000@evil.com", "evil.com@127.0.0.1:8000", "user@127.0.0.1:8000", "127.0.0.1:8000/", "127.0.0.1:8000 evil.com", "testserver",
    "::1:8000", "[0:0:0:0:0:0:0:1]:8000", "xlocalhost:8000", "localhostx:8000", "127.0.0.1:99999", "127.0.0.1:0",
]


@pytest.mark.parametrize("method,path", ALL_ROUTES, ids=IDS(ALL_ROUTES))
def test_forged_host_values_are_rejected_on_every_route_even_with_a_valid_token(client, method, path):
    for host in FORGED_HOSTS:
        response = call(client, method, path, host=host, token=main.GUARD.token)
        assert response.status_code == 400 and response.json() == {"detail": {"message": "Request rejected.", "code": "request_rejected"}}, (host, response.text)
        assert host not in response.text


def test_forged_hosts_are_rejected_for_bootstrap_and_health_and_preflight_too(client):
    for host in FORGED_HOSTS[:12]:
        assert call(client, "GET", HEALTH_PATH, host=host).status_code == 400
        assert call(client, "POST", BOOTSTRAP_PATH, host=host, origin=GOOD_ORIGIN).status_code == 400
        assert call(client, "OPTIONS", "/api/scan", host=host, origin=GOOD_ORIGIN,
                    headers={"access-control-request-method": "POST"}).status_code == 400
        assert call(client, "GET", "/does/not/exist", host=host, token=main.GUARD.token).status_code == 400


@pytest.mark.parametrize("host", ["127.0.0.1:8000", "LOCALHOST:8000", "LocalHost:8000", "[::1]:8000", "localhost:8000", " 127.0.0.1:8000 "])
def test_loopback_hosts_on_the_configured_port_are_accepted_case_insensitively(client, host):
    assert call(client, "GET", HEALTH_PATH, host=host).status_code == 200
    assert not guard_rejection(call(client, "GET", "/api/activity", host=host, token=main.GUARD.token))


def test_missing_or_duplicate_host_headers_are_rejected():
    ok = [("x-test", "1")]
    assert asgi("GET", HEALTH_PATH, ok)[0] == 400
    assert asgi("GET", HEALTH_PATH, ok + [("host", GOOD_HOST), ("host", GOOD_HOST)])[0] == 400
    assert asgi("GET", HEALTH_PATH, ok + [("host", GOOD_HOST), ("host", "evil.com")])[0] == 400
    assert asgi("GET", HEALTH_PATH, ok + [("host", GOOD_HOST)])[0] == 200


def test_the_port_comes_from_configuration_not_from_a_default():
    guard = Guard(backend_port=9123, frontend_port=4000)
    assert guard.host_allowed("127.0.0.1:9123") and guard.host_allowed("[::1]:9123") and not guard.host_allowed("127.0.0.1:8000")
    assert guard.origin_allowed("http://localhost:4000") and not guard.origin_allowed("http://localhost:5173")
    configured = Guard.from_environment({"LAZYDESK_PORT": "9001", "LAZYDESK_FRONTEND_PORT": "4001"})
    assert (configured.backend_port, configured.frontend_port) == (9001, 4001)
    assert Guard.from_environment({}).backend_port == 8000
    for bad in ("", "abc", "0", "70000", "-1", "8000; evil", " ", "08 00", "8000.5"):   # unsupported values are refused, never replaced
        with pytest.raises(api_guard.ConfigurationError, match="LAZYDESK_PORT"):
            Guard.from_environment({"LAZYDESK_PORT": bad})
    with pytest.raises(api_guard.ConfigurationError, match="LAZYDESK_FRONTEND_PORT"):
        Guard.from_environment({"LAZYDESK_FRONTEND_PORT": "x"})


def test_peers_that_are_not_loopback_are_rejected_even_with_everything_else_valid(client):
    headers = [("host", GOOD_HOST), ("origin", GOOD_ORIGIN), (TOKEN_HEADER, main.GUARD.token)]
    for peer in (("192.168.1.20", 5000), ("10.0.0.5", 5000), ("8.8.8.8", 1), ("::ffff:192.168.1.5", 1), None, ("example", 1)):
        assert asgi("POST", BOOTSTRAP_PATH, headers, client=peer)[0] == 403
        assert asgi("GET", "/api/activity", headers, client=peer)[0] == 403
        assert asgi("GET", HEALTH_PATH, headers, client=peer)[0] == 403
    for peer in (("127.0.0.1", 1), ("::1", 1)):
        assert asgi("GET", "/api/activity", headers, client=peer)[0] == 200


def test_the_test_peer_exemption_is_explicit_test_configuration_only():
    assert Guard.from_environment(dict(os.environ)).extra_peers == ()
    assert Guard().extra_peers == ()
    source = Path(api_guard.__file__).read_text(encoding="utf-8")
    assert set(re.findall(r'env\.get\("([A-Z_]+)"', source)) == {"LAZYDESK_PORT", "LAZYDESK_FRONTEND_PORT"}


# ---------------------------------------------------------------------------------------------------------------------------
# Token authorization
# ---------------------------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("method,path", PROTECTED, ids=IDS(PROTECTED))
def test_every_protected_route_rejects_missing_wrong_and_look_alike_tokens(client, method, path):
    good = main.GUARD.token
    attempts = [None, "", " ", "wrong", good[:-1], good + "x", good.upper(), good.lower() if good.lower() != good else good + "0", "Bearer " + good,
                "0" * len(good), good[::-1]]
    for token in attempts:
        response = call(client, method, path, token=token)
        assert response.status_code == 401 and response.json() == {"detail": {"message": "Authorization required.", "code": "authorization_required"}}, (token, response.text)


@pytest.mark.parametrize("method,path", PROTECTED, ids=IDS(PROTECTED))
def test_the_token_is_accepted_only_in_the_header(client, method, path):
    good = main.GUARD.token
    assert call(client, method, path, params={"token": good}).status_code == 401
    assert call(client, method, path, params={"access_token": good, TOKEN_HEADER: good}).status_code == 401
    assert call(client, method, path, headers={"cookie": f"token={good}; {TOKEN_HEADER}={good}"}).status_code == 401
    assert call(client, method, path, headers={"authorization": f"Bearer {good}"}).status_code == 401
    assert call(client, method, path, headers={"x-lazydesk-token-2": good}).status_code == 401
    assert not guard_rejection(call(client, method, path, token=good))


def test_two_token_headers_are_rejected_even_if_one_is_valid():
    base = [("host", GOOD_HOST)]
    assert asgi("GET", "/api/activity", base + [(TOKEN_HEADER, main.GUARD.token), (TOKEN_HEADER, "wrong")])[0] == 401
    assert asgi("GET", "/api/activity", base + [(TOKEN_HEADER, "wrong"), (TOKEN_HEADER, main.GUARD.token)])[0] == 401
    assert asgi("GET", "/api/activity", base + [(TOKEN_HEADER, main.GUARD.token)])[0] == 200


def test_unknown_paths_do_not_reveal_which_routes_exist(client):
    for path in ("/api/nope", "/", "/admin", "/api/plans/../../etc", "/docs", "/redoc", "/openapi.json", "/api/openapi.json"):
        assert call(client, "GET", path).status_code == 401
    for path in ("/api/nope", "/docs", "/redoc", "/openapi.json", "/api/openapi.json", "/api/docs"):
        assert call(client, "GET", path, token=main.GUARD.token).status_code == 404


def test_the_token_is_compared_in_constant_time():
    import inspect
    assert "hmac.compare_digest" in inspect.getsource(Guard.token_matches)
    guard = Guard()
    assert guard.token_matches(guard.token.encode()) and not guard.token_matches(b"") and not guard.token_matches(None)
    assert not guard.token_matches(guard.token.encode()[:-1])


def test_unauthorized_and_forged_requests_have_no_side_effects(client, tmp_path, fake_profile):
    workspace = fake_profile.profile / "Documents" / "ws"
    workspace.mkdir()
    source = workspace / "a.txt"
    source.write_text("x", encoding="utf-8")
    # no authorized call has happened yet: the database file must not even be created by rejected requests
    assert not main.DB_PATH.exists()
    for method, path in PROTECTED:
        call(client, method, path)
    call(client, "POST", "/api/scan", json={"path": str(workspace)}, host="evil.com", token=main.GUARD.token)
    assert not main.DB_PATH.exists()

    authorized = {main.TOKEN_HEADER: main.GUARD.token, "host": GOOD_HOST}
    plan = client.post("/api/plans", json={"workspace_root": str(workspace), "files": [str(source)]}, headers=authorized).json()
    action_id = plan["actions"][0]["id"]
    assert client.post(f"/api/plans/{plan['id']}/approve", json={"action_ids": [action_id]}, headers=authorized).status_code == 200
    before = database_fingerprint()
    attacks = [
        ("POST", f"/api/plans/{plan['id']}/reject", {"action_ids": [action_id]}), ("POST", "/api/execute", {"plan_id": plan["id"], "action_ids": [action_id]}),
        ("PATCH", f"/api/plans/{plan['id']}/actions/{action_id}", {"proposed_name": "evil.txt"}), ("POST", "/api/scan", {"path": str(workspace)}),
        ("PATCH", "/api/settings", {"model": "evil"}), ("POST", "/api/benchmarks", {"synthetic_files": 5}),
        ("POST", "/api/plans", {"workspace_root": str(workspace), "files": [str(source)]}), ("POST", "/api/duplicates", {"path": str(workspace)}),
    ]
    for method, path, body in attacks:
        for headers in ({"host": GOOD_HOST}, {"host": GOOD_HOST, TOKEN_HEADER: "wrong"}, {"host": "evil.com", TOKEN_HEADER: main.GUARD.token},
                        {"host": GOOD_HOST, TOKEN_HEADER: main.GUARD.token, "origin": "https://evil.example"},
                        {"host": GOOD_HOST, TOKEN_HEADER: main.GUARD.token, "sec-fetch-site": "cross-site"}):
            response = client.request(method, path, json=body, headers=headers)
            assert guard_rejection(response), (method, path, headers, response.status_code)
    assert database_fingerprint() == before
    assert source.exists() and not (workspace / "documents").exists()


# ---------------------------------------------------------------------------------------------------------------------------
# Origin and fetch metadata
# ---------------------------------------------------------------------------------------------------------------------------

BAD_ORIGINS = ["https://evil.example", "http://evil.example", "null", "http://127.0.0.1:5174", "http://127.0.0.1", "https://127.0.0.1:5173",
               "http://127.0.0.1:5173.evil.com", "http://evil.com:5173", "http://localhost:3000", "http://127.0.0.1:8000", "http://localhost:5173/x",
               "http://127.0.0.1.evil.com:5173", "HTTP://127.0.0.1:5173", " http://127.0.0.1:5173", "file://", ""]


@pytest.mark.parametrize("method,path", ALL_ROUTES, ids=IDS(ALL_ROUTES))
def test_foreign_origins_are_rejected_even_with_a_valid_token(client, method, path):
    for origin in BAD_ORIGINS:
        response = call(client, method, path, token=main.GUARD.token, origin=origin)
        assert response.status_code == 403 and response.json()["detail"]["code"] == "request_rejected", (origin, response.status_code)
        assert "access-control-allow-origin" not in response.headers


@pytest.mark.parametrize("method,path", ALL_ROUTES, ids=IDS(ALL_ROUTES))
def test_forged_fetch_metadata_is_rejected(client, method, path):
    for site in ("cross-site", "none", "Cross-Site", "evil", "", "same-origin, cross-site"):
        response = call(client, method, path, token=main.GUARD.token, origin=GOOD_ORIGIN, headers={"sec-fetch-site": site})
        assert response.status_code == 403, (site, response.status_code)


@pytest.mark.parametrize("origin", ["http://127.0.0.1:5173", "http://localhost:5173", "http://[::1]:5173"])
@pytest.mark.parametrize("site", ["same-origin", "same-site", None])
def test_the_legitimate_frontend_origins_and_metadata_pass(client, origin, site):
    headers = {"sec-fetch-site": site} if site else {}
    response = call(client, "GET", "/api/activity", token=main.GUARD.token, origin=origin, headers=headers)
    assert response.status_code == 200 and response.headers["access-control-allow-origin"] == origin
    assert "access-control-allow-credentials" not in response.headers


def test_duplicate_origin_headers_are_rejected():
    base = [("host", GOOD_HOST), (TOKEN_HEADER, main.GUARD.token)]
    assert asgi("GET", "/api/activity", base + [("origin", GOOD_ORIGIN), ("origin", GOOD_ORIGIN)])[0] == 403
    assert asgi("GET", "/api/activity", base + [("origin", GOOD_ORIGIN), ("origin", "https://evil.example")])[0] == 403


def test_requests_without_origin_still_need_the_token(client):
    assert call(client, "POST", "/api/scan").status_code == 401
    assert call(client, "POST", "/api/scan", token=main.GUARD.token).status_code != 401


def test_preflight_is_restrictive_and_never_allows_credentials(client):
    ok = call(client, "OPTIONS", "/api/scan", origin=GOOD_ORIGIN, headers={"access-control-request-method": "POST",
                                                                          "access-control-request-headers": f"content-type,{TOKEN_HEADER}"})
    assert ok.status_code == 200 and ok.headers["access-control-allow-origin"] == GOOD_ORIGIN
    assert TOKEN_HEADER.lower() in ok.headers["access-control-allow-headers"].lower()
    assert "access-control-allow-credentials" not in ok.headers
    assert "DELETE" not in ok.headers["access-control-allow-methods"] and "PUT" not in ok.headers["access-control-allow-methods"]
    assert call(client, "OPTIONS", "/api/scan", origin="https://evil.example", headers={"access-control-request-method": "POST"}).status_code == 403
    assert "authorization" not in ok.headers["access-control-allow-headers"].lower()


# ---------------------------------------------------------------------------------------------------------------------------
# Bootstrap and health
# ---------------------------------------------------------------------------------------------------------------------------

def test_bootstrap_returns_the_token_to_an_allowed_origin_only(client):
    response = call(client, "POST", BOOTSTRAP_PATH, origin=GOOD_ORIGIN, headers={"sec-fetch-site": "same-site"})
    assert response.status_code == 200
    assert response.json() == {"token": main.GUARD.token, "header": TOKEN_HEADER}
    assert response.headers["cache-control"] == "no-store" and response.headers["access-control-allow-origin"] == GOOD_ORIGIN
    for failure in (
        dict(origin=None), dict(origin="https://evil.example"), dict(origin="null"), dict(origin="http://127.0.0.1:5174"),
        dict(origin=GOOD_ORIGIN, headers={"sec-fetch-site": "cross-site"}), dict(origin=GOOD_ORIGIN, headers={"sec-fetch-site": "none"}),
        dict(origin=GOOD_ORIGIN, host="evil.com"), dict(origin=GOOD_ORIGIN, host="127.0.0.1"), dict(origin=GOOD_ORIGIN, host="127.0.0.1:9999"),
    ):
        denied = call(client, "POST", BOOTSTRAP_PATH, **failure)
        assert denied.status_code in (400, 403) and main.GUARD.token not in denied.text and "token" not in denied.json()


def test_bootstrap_rejects_other_methods_and_never_accepts_the_token_as_credentials_for_itself(client):
    for method in ("GET", "PUT", "DELETE", "PATCH"):
        response = call(client, method, BOOTSTRAP_PATH, origin=GOOD_ORIGIN, token=main.GUARD.token)
        assert response.status_code in (403, 405) and main.GUARD.token not in response.text
    assert call(client, "GET", BOOTSTRAP_PATH + "?token=1", origin=GOOD_ORIGIN).status_code == 403


def test_the_bootstrapped_token_authorizes_requests(client):
    token = call(client, "POST", BOOTSTRAP_PATH, origin=GOOD_ORIGIN).json()["token"]
    assert call(client, "GET", "/api/activity", token=token, origin=GOOD_ORIGIN).status_code == 200


def test_health_is_public_minimal_and_reveals_nothing(client, monkeypatch):
    monkeypatch.setenv("OLLAMA_MODEL", "secret-model-name")
    for kwargs in ({}, {"token": "wrong"}, {"token": main.GUARD.token}, {"origin": GOOD_ORIGIN}):
        response = call(client, "GET", HEALTH_PATH, **kwargs)
        assert response.status_code == 200 and response.json() == {"status": "ok"}
        assert "secret-model-name" not in response.text and str(main.DB_PATH) not in response.text
    assert call(client, "GET", HEALTH_PATH, origin="https://evil.example").status_code == 403
    assert call(client, "POST", HEALTH_PATH).status_code == 401


def test_sensitive_reads_need_the_token(client):
    for path in ("/api/status", "/api/settings", "/api/activity", "/api/benchmarks"):
        assert call(client, "GET", path).status_code == 401
        assert call(client, "GET", path, token=main.GUARD.token).status_code == 200


def test_docs_and_schema_endpoints_are_disabled(client):
    assert main.app.docs_url is None and main.app.redoc_url is None and main.app.openapi_url is None
    for path in ("/docs", "/redoc", "/openapi.json", "/docs/oauth2-redirect"):
        assert call(client, "GET", path, token=main.GUARD.token).status_code == 404
        assert main.GUARD.token not in call(client, "GET", path, token=main.GUARD.token).text


# ---------------------------------------------------------------------------------------------------------------------------
# Token lifecycle and secrecy
# ---------------------------------------------------------------------------------------------------------------------------

def launch_token() -> str:
    env = {**os.environ, "LAZYDESK_DB": str(main.DB_PATH.parent / "rotation.sqlite"), "PYTHONDONTWRITEBYTECODE": "1"}
    result = subprocess.run([sys.executable, "-I", "-c", "import sys; sys.path.insert(0, sys.argv[1]); from app import main; print(main.GUARD.token)", str(BACKEND)],
                            capture_output=True, text=True, env=env, timeout=60)
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def test_every_backend_launch_gets_a_new_unpredictable_token():
    first, second = launch_token(), launch_token()
    assert first != second and first != main.GUARD.token and second != main.GUARD.token
    for token in (first, second, main.GUARD.token):
        assert re.fullmatch(r"[A-Za-z0-9_-]{43,}", token)
    assert Guard().token != Guard().token and "token" not in repr(Guard())


def test_a_token_from_an_earlier_launch_stops_working(client):
    old = main.GUARD.token
    assert call(client, "GET", "/api/activity", token=old).status_code == 200
    previous = main.GUARD
    try:
        main.GUARD = Guard(extra_peers=("testclient",))     # what a backend restart does
        assert call(client, "GET", "/api/activity", token=old).status_code == 401
        assert call(client, "GET", "/api/activity", token=main.GUARD.token).status_code == 200
    finally:
        main.GUARD = previous


def test_the_token_never_appears_in_responses_logs_errors_or_urls(client, caplog, capsys, tmp_path, fake_profile):
    token = main.GUARD.token
    seen: list[str] = []
    caplog.set_level(logging.DEBUG)
    workspace = fake_profile.profile / "Documents" / "w"
    workspace.mkdir()
    (workspace / "a.txt").write_text("x", encoding="utf-8")
    bodies = {"/api/scan": {"path": str(workspace)}, "/api/plans": {"workspace_root": str(workspace), "files": [str(workspace / "a.txt")]},
              "/api/execute": {"plan_id": "x", "action_ids": ["y"]}, "/api/benchmarks": {"synthetic_files": 3}, "/api/duplicates": {"path": str(workspace)}}
    for method, path in ALL_ROUTES:
        if (method, path) == ("POST", BOOTSTRAP_PATH):
            continue
        for kwargs in ({"token": token}, {"token": "wrong"}, {}, {"token": token, "origin": "https://evil.example"}, {"token": token, "host": "evil.com"}):
            response = client.request(method, path, headers={"host": kwargs.get("host", GOOD_HOST), **({TOKEN_HEADER: kwargs["token"]} if "token" in kwargs else {}),
                                                               **({"origin": kwargs["origin"]} if "origin" in kwargs else {})}, json=bodies.get(path, {}))
            seen.append(response.text + json.dumps(dict(response.headers)) + str(response.url))
    # a request that blows up inside a route must not echo the token either
    original = main.ollama_models
    main.ollama_models = lambda: (_ for _ in ()).throw(RuntimeError("boom"))
    try:
        crash = client.request("GET", "/api/status", headers={"host": GOOD_HOST, TOKEN_HEADER: token})
        seen.append(crash.text)
    except RuntimeError as error:
        seen.append(str(error))
    finally:
        main.ollama_models = original
    out = capsys.readouterr()
    assert not any(token in text for text in seen), "token leaked into a response, header or URL"
    assert token not in caplog.text and token not in out.out and token not in out.err
    assert token not in repr(main.GUARD) and token not in str(main.app.routes)


def test_no_source_file_persists_or_logs_the_token():
    backend = (BACKEND / "app").glob("*.py")
    for path in backend:
        text = path.read_text(encoding="utf-8")
        assert not re.search(r"(print|logging\.|logger\.)[^\n]*(token|TOKEN)", text), path.name
    assert "?token" not in (BACKEND / "app" / "main.py").read_text(encoding="utf-8")


def test_the_frontend_never_persists_or_url_encodes_the_token_and_the_bundle_has_no_secret():
    sources = [p for p in (REPO / "frontend" / "src").rglob("*") if p.suffix in (".ts", ".tsx")]
    if not sources:
        pytest.skip("frontend sources are not part of this checkout")
    for path in sources:
        if path.name.endswith(".test.ts"):
            continue
        text = path.read_text(encoding="utf-8")
        assert not re.search(r"localStorage|sessionStorage|indexedDB|document\.cookie|location\.(hash|search)|URLSearchParams", text), path.name
        assert not re.search(r"[?&]token=|#token", text), path.name
    dist = REPO / "frontend" / "dist"
    if dist.is_dir():
        for path in dist.rglob("*"):
            if path.is_file() and path.suffix in (".js", ".html", ".css", ".map"):
                assert main.GUARD.token not in path.read_text(encoding="utf-8", errors="ignore"), path
                assert not re.search(r"localStorage|sessionStorage", path.read_text(encoding="utf-8", errors="ignore")), path.name


def test_documentation_names_the_header_but_contains_no_token_value():
    for path in (REPO / "README.md",):
        text = path.read_text(encoding="utf-8")
        assert main.GUARD.token not in text
        assert not re.search(r"X-LazyDesk-Token:\s*[A-Za-z0-9_-]{20,}", text)


# ---------------------------------------------------------------------------------------------------------------------------
# Bind configuration and production defaults
# ---------------------------------------------------------------------------------------------------------------------------

def test_every_launcher_binds_to_loopback_only():
    launchers = [REPO / "demo" / "start-demo.ps1", REPO / "README.md"]
    uvicorn_lines = [line for p in launchers for line in p.read_text(encoding="utf-8").splitlines() if "uvicorn" in line]
    assert uvicorn_lines and all("--host 127.0.0.1" in line for line in uvicorn_lines), uvicorn_lines
    for path in (*launchers, *REPO.glob("*.ps1"), *(REPO / "demo").glob("*"), BACKEND / "app" / "main.py", REPO / ".github" / "workflows" / "ci.yml"):
        if path.is_file():
            text = path.read_text(encoding="utf-8", errors="ignore")
            assert "0.0.0.0" not in text and "--host ::" not in text, path


def test_production_defaults_are_strict():
    guard = Guard()
    assert (guard.backend_port, guard.frontend_port) == (8000, 5173)
    assert guard.allowed_origins == ("http://127.0.0.1:5173", "http://localhost:5173", "http://[::1]:5173")
    assert not guard.peer_allowed(("testclient", 1)) and guard.peer_allowed(("127.0.0.1", 1)) and guard.peer_allowed(("::1", 1))
    cors = next(m for m in main.app.user_middleware if m.cls.__name__ == "CORSMiddleware")
    assert cors.kwargs["allow_credentials"] is False and "*" not in cors.kwargs["allow_origins"] and "*" not in cors.kwargs["allow_headers"]
    assert main.app.user_middleware[0].cls is api_guard.ApiGuardMiddleware   # outermost: it runs before CORS and routing
