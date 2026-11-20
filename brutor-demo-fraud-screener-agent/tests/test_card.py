def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_card_shape(client, settings):
    r = client.get("/.well-known/agent-card.json")
    assert r.status_code == 200
    card = r.json()
    assert card["name"] == "Brutor Demo Fraud Screener"
    assert card["protocolVersion"] == "1.0"
    assert card["url"] == settings.public_url
    assert card["supportedInterfaces"][0] == {"url": settings.public_url, "protocolBinding": "JSONRPC", "protocolVersion": "1.0"}
    assert card["capabilities"] == {"streaming": False}
    assert card["defaultInputModes"] == ["text"] and card["defaultOutputModes"] == ["text"]
    assert card["data_classification"] == ["PII"]
    assert len(card["skills"]) == 1
    skill = card["skills"][0]
    assert skill["id"] == skill["name"] == "screening.fraud_sanctions"
    assert skill["tags"] == ["screening", "fraud", "sanctions"]
    assert skill["data_classification"] == ["PII"]


def test_card_url_follows_public_url_env(monkeypatch):
    from starlette.testclient import TestClient

    from fraud_screener.app import create_app
    from fraud_screener.config import Settings

    monkeypatch.setenv("A2A_PUBLIC_URL", "http://localhost:9200/")
    with TestClient(create_app(Settings.from_env())) as c:
        card = c.get("/.well-known/agent-card.json").json()
    assert card["url"] == "http://localhost:9200"
    assert card["supportedInterfaces"][0]["url"] == "http://localhost:9200"
