from servora.marketplace import MarketplaceError, MarketplaceStore, validate_entry


def entry(name="demo"):
    return {
        "name": name,
        "category": "community",
        "verification": "community",
        "description": "Demo",
        "tags": ["web"],
        "source": {"type": "oci", "image": "nginx:alpine"},
        "manifest": {
            "name": name,
            "version": "1.0.0",
            "description": "Demo",
            "services": [{"name": "web", "image": "nginx:alpine"}],
        },
    }


def test_marketplace_publish_and_download(tmp_path):
    store = MarketplaceStore(tmp_path)
    store.publish(entry())
    package = store.download("demo")
    assert package["format"] == "servora-marketplace-v1"
    assert package["entry"]["manifest"]["name"] == "demo"


def test_marketplace_fork_creates_community_copy(tmp_path):
    store = MarketplaceStore(tmp_path)
    store.save({**entry("officialish"), "category": "official", "verification": "verified"})
    fork = store.fork("officialish", "my-copy")
    assert fork.category == "community"
    assert fork.manifest["name"] == "my-copy"
    assert fork.source["forked_from"] == "officialish"
    assert "fork" in fork.tags


def test_marketplace_rejects_duplicate_fork(tmp_path):
    store = MarketplaceStore(tmp_path)
    store.publish(entry())
    store.publish(entry("other"))
    try:
        store.fork("demo", "other")
    except MarketplaceError as exc:
        assert "already exists" in str(exc)
    else:
        raise AssertionError("duplicate fork should fail")


def test_marketplace_entry_validation(tmp_path):
    validated = validate_entry(entry())
    assert validated.manifest["name"] == "demo"


def _entry(name="demo", verification="community"):
    return {
        "name": name,
        "category": "community",
        "verification": verification,
        "description": "Demo",
        "tags": ["web"],
        "source": {"type": "oci", "image": "nginx:alpine"},
        "manifest": {
            "name": name,
            "version": "1.0.0",
            "description": "Demo",
            "services": [{"name": "web", "image": "nginx:alpine"}],
        },
    }


def test_marketplace_store_keeps_installable_manifest(tmp_path):
    store = MarketplaceStore(tmp_path)
    saved = store.publish(_entry())
    assert saved.manifest["name"] == "demo"
    assert store.get("demo").manifest["services"][0]["image"] == "nginx:alpine"


def test_marketplace_risk_entry_is_marked_for_approval(tmp_path):
    store = MarketplaceStore(tmp_path)
    saved = store.publish(_entry("risky", "risk_detected"))
    assert saved.verification == "risk_detected"


def test_marketplace_imports_remote_compose(monkeypatch, tmp_path):
    import servora.source_import as source_import

    compose = b"""
services:
  web:
    image: nginx:alpine
    ports:
      - "8080:80"
"""

    def fake_fetch(url, accept="*/*"):
        return url, compose, {"content_type": "text/yaml"}

    monkeypatch.setattr(source_import, "_fetch", fake_fetch)
    store = MarketplaceStore(tmp_path)
    saved = store.import_url("https://example.com/compose.yml")
    assert saved.manifest["name"] == "compose"
    assert saved.manifest["services"][0]["image"] == "nginx:alpine"
    assert saved.manifest["services"][0]["ports"][0]["host"] == 8080
    assert saved.source["type"] == "remote_compose"


def test_marketplace_import_marks_risky_compose(monkeypatch, tmp_path):
    import servora.source_import as source_import

    compose = b"""
services:
  daemon:
    image: alpine:latest
    privileged: true
"""

    def fake_fetch(url, accept="*/*"):
        return url, compose, {"content_type": "text/yaml"}

    monkeypatch.setattr(source_import, "_fetch", fake_fetch)
    store = MarketplaceStore(tmp_path)
    saved = store.import_url("https://example.com/compose.yml")
    assert saved.verification == "risk_detected"
    assert saved.source["findings"]


def test_marketplace_imports_docker_run_from_readme(monkeypatch, tmp_path):
    import servora.source_import as source_import

    readme = b"Install:\n\n    docker run -d --name web -p 8080:80 nginx:alpine\n"

    def fake_fetch(url, accept="*/*"):
        return url, readme, {"content_type": "text/plain"}

    monkeypatch.setattr(source_import, "_fetch", fake_fetch)
    store = MarketplaceStore(tmp_path)
    saved = store.import_url("https://example.com/README.md")
    assert saved.manifest["services"][0]["image"] == "nginx:alpine"
    assert saved.manifest["services"][0]["ports"][0]["host"] == 8080
    assert saved.source["type"] == "docker_run"


def test_marketplace_imports_multiline_docker_run_and_inline_options(monkeypatch, tmp_path):
    import servora.source_import as source_import

    readme = b"""Install:
    docker run --name=web \
      --publish=8080:80 \
      --env=MODE=production \
      nginx:alpine
"""

    def fake_fetch(url, accept="*/*"):
        return url, readme, {"content_type": "text/plain"}

    monkeypatch.setattr(source_import, "_fetch", fake_fetch)
    store = MarketplaceStore(tmp_path)
    saved = store.import_url("https://example.com/README.md")
    service = saved.manifest["services"][0]
    assert service["name"] == "app"
    assert service["ports"][0]["host"] == 8080
    assert service["environment"]["MODE"] == "production"


def test_marketplace_rejects_unsupported_docker_run_option(monkeypatch, tmp_path):
    import servora.source_import as source_import

    readme = b"Install:\n    docker run --privileged nginx:alpine\n"

    def fake_fetch(url, accept="*/*"):
        return url, readme, {"content_type": "text/plain"}

    monkeypatch.setattr(source_import, "_fetch", fake_fetch)
    store = MarketplaceStore(tmp_path)
    try:
        store.import_url("https://example.com/README.md")
    except Exception as exc:
        assert "privileged" in str(exc)
    else:
        raise AssertionError("privileged docker run should be rejected")



def test_marketplace_imports_named_docker_mount(monkeypatch, tmp_path):
    import servora.source_import as source_import

    readme = b"""Install:\n    docker run --name web --mount type=volume,source=webdata,target=/var/lib/data,readonly nginx:alpine\n"""

    def fake_fetch(url, accept="*/*"):
        return url, readme, {"content_type": "text/plain"}

    monkeypatch.setattr(source_import, "_fetch", fake_fetch)
    store = MarketplaceStore(tmp_path)
    saved = store.import_url("https://example.com/README.md")
    volume = saved.manifest["services"][0]["volumes"][0]
    assert volume == {
        "name": "webdata",
        "container_path": "/var/lib/data",
        "read_only": True,
    }


def test_marketplace_rejects_bind_docker_mount(monkeypatch, tmp_path):
    import servora.source_import as source_import

    readme = b"Install:\n    docker run --mount type=bind,source=/srv/data,target=/data nginx:alpine\n"

    def fake_fetch(url, accept="*/*"):
        return url, readme, {"content_type": "text/plain"}

    monkeypatch.setattr(source_import, "_fetch", fake_fetch)
    store = MarketplaceStore(tmp_path)
    try:
        store.import_url("https://example.com/README.md")
    except Exception as exc:
        assert "named Docker volume" in str(exc)
    else:
        raise AssertionError("bind docker mount should be rejected")
