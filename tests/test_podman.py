from servora.podman import Podman


def test_command_is_argument_list(monkeypatch):
    calls = []
    p = Podman(executable="podman")
    def fake_run(argv, **kwargs):
        calls.append(argv)
        class R:
            returncode = 0
            stdout = "ok\n"
            stderr = ""
        return R()
    monkeypatch.setattr("servora.podman.subprocess.run", fake_run)
    assert p.start_container("demo") == "ok"
    assert calls[0] == ["podman", "start", "demo"]


def test_empty_json_output(monkeypatch):
    p = Podman(executable="podman")
    monkeypatch.setattr("servora.podman.subprocess.run", lambda *a, **k: type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})())
    assert p.list_containers() == []


def test_stats_command(monkeypatch):
    calls = []
    p = Podman(executable="podman")
    def fake_run(argv, **kwargs):
        calls.append(argv)
        return type("R", (), {"returncode": 0, "stdout": "[]\n", "stderr": ""})()
    monkeypatch.setattr("servora.podman.subprocess.run", fake_run)
    assert p.stats("demo") == []
    assert calls[0] == ["podman", "stats", "--no-stream", "--format", "json", "demo"]


def test_image_metadata_normalizes_id_and_digest():
    p = Podman(executable="podman")
    p.inspect_image = lambda name: {
        "Id": "sha256:abc",
        "RepoTags": ["nginx:latest"],
        "RepoDigests": ["nginx@sha256:def"],
        "Created": "2026-01-01T00:00:00Z",
        "Size": 1234,
    }
    assert p.image_metadata("nginx:latest") == {
        "name": "nginx:latest",
        "id": "sha256:abc",
        "digest": "sha256:def",
        "repo_tags": ["nginx:latest"],
        "repo_digests": ["nginx@sha256:def"],
        "created": "2026-01-01T00:00:00Z",
        "size": 1234,
    }


def test_refresh_image_reports_unchanged(monkeypatch):
    p = Podman(executable="podman")
    states = iter([
        {"name": "nginx:latest", "id": "sha256:same", "digest": None, "repo_tags": [], "repo_digests": [], "created": None, "size": 1},
        {"name": "nginx:latest", "id": "sha256:same", "digest": None, "repo_tags": [], "repo_digests": [], "created": None, "size": 1},
    ])
    monkeypatch.setattr(p, "image_metadata", lambda name: next(states))
    monkeypatch.setattr(p, "pull_image", lambda name: "Already exists")
    result = p.refresh_image("nginx:latest")
    assert result["status"] == "unchanged"
    assert result["changed"] is False


def test_refresh_image_reports_updated(monkeypatch):
    p = Podman(executable="podman")
    states = iter([
        {"name": "nginx:latest", "id": "sha256:old", "digest": None, "repo_tags": [], "repo_digests": [], "created": None, "size": 1},
        {"name": "nginx:latest", "id": "sha256:new", "digest": None, "repo_tags": [], "repo_digests": [], "created": None, "size": 2},
    ])
    monkeypatch.setattr(p, "image_metadata", lambda name: next(states))
    monkeypatch.setattr(p, "pull_image", lambda name: "Downloaded")
    result = p.refresh_image("nginx:latest")
    assert result["status"] == "updated"
    assert result["changed"] is True


def test_refresh_image_prefers_digest_over_image_id(monkeypatch):
    p = Podman(executable="podman")
    states = iter([
        {
            "name": "nginx:latest",
            "id": "sha256:same",
            "digest": "sha256:digest",
            "repo_tags": [],
            "repo_digests": [],
            "created": None,
            "size": 1,
        },
        {
            "name": "nginx:latest",
            "id": "sha256:different",
            "digest": "sha256:digest",
            "repo_tags": [],
            "repo_digests": [],
            "created": None,
            "size": 2,
        },
    ])
    monkeypatch.setattr(p, "image_metadata", lambda name: next(states))
    monkeypatch.setattr(p, "pull_image", lambda name: "Downloaded")
    result = p.refresh_image("nginx:latest")
    assert result["status"] == "unchanged"
    assert result["changed"] is False


def test_refresh_image_detects_digest_change_even_with_same_image_id(monkeypatch):
    p = Podman(executable="podman")
    states = iter([
        {
            "name": "nginx:latest",
            "id": "sha256:same",
            "digest": "sha256:old-digest",
            "repo_tags": [],
            "repo_digests": [],
            "created": None,
            "size": 1,
        },
        {
            "name": "nginx:latest",
            "id": "sha256:same",
            "digest": "sha256:new-digest",
            "repo_tags": [],
            "repo_digests": [],
            "created": None,
            "size": 2,
        },
    ])
    monkeypatch.setattr(p, "image_metadata", lambda name: next(states))
    monkeypatch.setattr(p, "pull_image", lambda name: "Downloaded")
    result = p.refresh_image("nginx:latest")
    assert result["status"] == "updated"
    assert result["changed"] is True
