from uuid import uuid4

from fastapi.testclient import TestClient

from vingo_agent.api.app import create_app

HEADERS = {
    "X-Agent-Service-Key": "test-service-key",
    "X-Vingo-User-Id": str(uuid4()),
    "X-Vingo-Role": "BUYER",
    "Authorization": "Bearer session-token",
}


def client() -> TestClient:
    return TestClient(create_app())


def test_health_returns200():
    assert client().get("/health").json() == {"status": "ok"}


def test_createConversation_returns401_withoutServiceKey():
    assert client().post("/internal/v1/conversations").status_code == 401


def test_createConversation_returns401_withWrongServiceKey():
    headers = {**HEADERS, "X-Agent-Service-Key": "wrong"}
    assert client().post("/internal/v1/conversations", headers=headers).status_code == 401


def test_createConversation_returns403_withUnknownRole():
    headers = {**HEADERS, "X-Vingo-Role": "ROOT"}
    assert client().post("/internal/v1/conversations", headers=headers).status_code == 403


def test_createConversation_returns501_whileSkeleton():
    assert client().post("/internal/v1/conversations", headers=HEADERS).status_code == 501


def test_sendMessage_returns422_withExtraField():
    body = {"command_id": str(uuid4()), "text": "xin chào", "user_id": "x"}
    response = client().post(f"/internal/v1/conversations/{uuid4()}/messages", headers=HEADERS, json=body)
    assert response.status_code == 422
