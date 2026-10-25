"""Shared fakes for tests that need a model without a real Ollama: a stand-in for urllib's urlopen."""
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import main


class Response:
    def __init__(self, body):
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def read(self):
        if isinstance(self.body, bytes):
            return self.body
        if isinstance(self.body, str):
            return self.body.encode()
        return json.dumps(self.body).encode()


class FakeOllama:
    """Stands in for urllib.request.urlopen. `handler(request_json)` returns the HTTP body or raises.

    Models are "already loaded" by default, so no warm-up is needed; pass loaded=False to simulate a cold model."""

    def __init__(self, handler, models=("m1",), loaded=True, warmup_error=None, digests=None):
        self.handler, self.models, self.loaded, self.warmup_error = handler, list(models), loaded, warmup_error
        self.digests = dict(digests or {})  # name -> digest; mutate between scans to simulate a re-pulled model
        self.generate_calls, self.warmup_calls, self.timeouts, self.ps_calls, self.order = [], [], [], 0, []

    def __call__(self, request, **kwargs):
        endpoint = request.full_url.rsplit("/", 1)[-1]
        self.timeouts.append((endpoint, kwargs.get("timeout")))
        if endpoint == "tags":
            return Response({"models": [({"name": name, "digest": self.digests[name]} if name in self.digests else {"name": name}) for name in self.models]})
        if endpoint == "ps":
            self.ps_calls += 1
            self.order.append('ps')
            return Response({"models": [{"name": name} for name in self.models] if self.loaded else []})
        body = json.loads(request.data)
        if "prompt" not in body:  # a warm-up request asks Ollama to load the model
            self.warmup_calls.append(body)
            self.order.append('warmup')
            if self.warmup_error is not None:
                raise self.warmup_error
            self.loaded = True
            return Response({"done": True, "done_reason": "load"})
        self.generate_calls.append(body)
        self.order.append('generate')
        return Response(self.handler(body))


def answer(category="images", name="renamed.xyz", reason="Looks like a picture."):
    return {"response": json.dumps({"category": category, "proposed_name": name, "reason": reason})}


@pytest.fixture
def client() -> TestClient:
    return TestClient(main.app)


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    root = tmp_path / "ws"
    root.mkdir()
    return root


def use_model(monkeypatch: pytest.MonkeyPatch, handler, models=("m1",), selected="m1", digests=None) -> FakeOllama:
    fake = FakeOllama(handler, models, digests=digests)
    monkeypatch.setattr(main.urllib.request, "urlopen", fake)
    monkeypatch.setenv("OLLAMA_MODEL", selected)
    return fake


def scan(client: TestClient, root: Path, **extra) -> dict:
    response = client.post("/api/scan", json={"path": str(root), **extra})
    assert response.status_code == 200, response.text
    return response.json()


def plan(client: TestClient, root: Path, *files: Path) -> dict:
    response = client.post("/api/plans", json={"workspace_root": str(root), "files": [str(item) for item in files]})
    assert response.status_code == 200, response.text
    return response.json()
