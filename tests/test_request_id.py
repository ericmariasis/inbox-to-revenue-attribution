import uuid

from fastapi.testclient import TestClient

from app.main import app


def test_health_has_request_id_header():
    with TestClient(app) as client:
        response = client.get("/health")

    assert response.status_code == 200
    request_id = response.headers.get("X-Request-Id")
    assert request_id
    uuid.UUID(request_id)


def test_reuses_incoming_request_id():
    with TestClient(app) as client:
        response = client.get("/health", headers={"X-Request-Id": "req-123"})

    assert response.headers["X-Request-Id"] == "req-123"