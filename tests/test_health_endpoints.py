"""The liveness/readiness contract, exercised over HTTP.

``api/health.py`` promises two different things and the difference is the whole
point: ``/health`` is liveness and must stay 200 even when a dependency is down
(restarting the process would not fix a broken database), while ``/ready`` is
readiness and returns 503 so a scheduler or load balancer can act on the status
code alone. ``Dockerfile`` HEALTHCHECK and the compose healthcheck both curl
``/api/ready``, so a regression here is a deployment that never reports healthy
- with nothing in the suite to catch it.
"""
from __future__ import annotations

import pytest

from infrastructure import health_runtime as hr


@pytest.fixture()
def _ready_service():
    """The route binds a HealthService at import time; patch that instance."""
    from api import health as health_module

    return health_module.service


def test_health_is_200_even_when_dependencies_are_broken(client, monkeypatch):
    """Liveness must not depend on the database - that is what /ready is for."""
    def _boom(*args, **kwargs):
        raise RuntimeError("database is gone")

    monkeypatch.setattr(hr, "Session", _boom)

    response = client.get("/api/health")

    assert response.status_code == 200
    assert response.json()["ok"] is True


def test_ready_is_200_when_everything_works(client):
    response = client.get("/api/ready")

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["database"]["ok"] is True
    assert body["registry"]["ok"] is True
    assert body["registry"]["platform_count"] > 0, "a fresh env without platforms is not ready"


def test_ready_is_503_when_the_database_is_unreachable(client, monkeypatch):
    def _boom(*args, **kwargs):
        raise RuntimeError("unable to open database file")

    monkeypatch.setattr(hr, "Session", _boom)

    response = client.get("/api/ready")

    assert response.status_code == 503, "the scheduler acts on the status code, not the body"
    body = response.json()
    assert body["ok"] is False
    assert body["database"]["ok"] is False
    assert "unable to open database file" in body["database"]["error"]


def test_ready_is_503_when_the_registry_is_unreachable(client, monkeypatch):
    def _boom():
        raise RuntimeError("registry exploded")

    monkeypatch.setattr(hr, "list_platforms", _boom)

    response = client.get("/api/ready")

    assert response.status_code == 503
    body = response.json()
    assert body["ok"] is False
    assert body["registry"]["ok"] is False
    assert body["registry"]["platform_count"] == 0


def test_a_stopped_solver_does_not_make_the_service_unready(client, monkeypatch):
    """Deliberate: the solver is optional, so it is reported but never gates readiness."""
    monkeypatch.setattr(hr, "is_running", lambda: False)

    response = client.get("/api/ready")

    assert response.status_code == 200
    assert response.json()["solver"]["running"] is False
