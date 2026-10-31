def test_health_reports_ok_and_project_root(client, project_root):
    response = client.get("/health")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["projectRoot"] == str(project_root)


def test_foreign_host_is_rejected(client):
    # A DNS-rebinding page reaches 127.0.0.1 under its own host name.
    assert client.get("/health", headers={"Host": "evil.example"}).status_code == 400
    assert client.get("/health", headers={"Host": "localhost:7867"}).status_code == 200
