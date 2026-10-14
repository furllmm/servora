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


def test_env_overrides_are_passed_to_subprocess(monkeypatch):
    """Portable modda Podman env override'ları (CONTAINERS_*) gerçek subprocess'e iletilmeli."""
    captured = {}
    p = Podman(executable="podman", env={
        "CONTAINERS_STORAGE_CONF": "/srv/Servora/podman/storage.conf",
        "CONTAINERS_GRAPHROOT": "/srv/Servora/podman/storage",
        "CONTAINERS_RUNROOT": "/srv/Servora/podman/runroot",
    })

    def fake_run(argv, **kwargs):
        captured["argv"] = argv
        captured["env"] = kwargs.get("env", {})
        return type("R", (), {"returncode": 0, "stdout": "5.0.0\n", "stderr": ""})()

    monkeypatch.setattr("servora.podman.subprocess.run", fake_run)
    assert p.version() == "5.0.0"
    assert captured["env"]["CONTAINERS_STORAGE_CONF"] == "/srv/Servora/podman/storage.conf"
    assert captured["env"]["CONTAINERS_GRAPHROOT"] == "/srv/Servora/podman/storage"
    assert captured["env"]["CONTAINERS_RUNROOT"] == "/srv/Servora/podman/runroot"
    # Sistem ortamını bozmadan mevcut değişkenleri korumalı:
    import os
    assert captured["env"].get("PATH") == os.environ.get("PATH")


def test_missing_podman_binary_raises_podman_error():
    p = Podman(executable="/nonexistent/podman-binary")
    from servora.podman import PodmanError
    try:
        p.version()
    except PodmanError as exc:
        assert "podman" in str(exc).lower() or "no such file" in str(exc).lower()
    else:
        raise AssertionError("PodmanError beklenirken başarı döndü")
