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
    monkeypatch.setattr(source_import, "_validate_url", lambda url: url)
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
    monkeypatch.setattr(source_import, "_validate_url", lambda url: url)
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
    monkeypatch.setattr(source_import, "_validate_url", lambda url: url)
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
    monkeypatch.setattr(source_import, "_validate_url", lambda url: url)
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
    monkeypatch.setattr(source_import, "_validate_url", lambda url: url)
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
    monkeypatch.setattr(source_import, "_validate_url", lambda url: url)
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
    monkeypatch.setattr(source_import, "_validate_url", lambda url: url)
    store = MarketplaceStore(tmp_path)
    try:
        store.import_url("https://example.com/README.md")
    except Exception as exc:
        assert "named Docker volume" in str(exc)
    else:
        raise AssertionError("bind docker mount should be rejected")


def test_marketplace_imports_github_blob_with_slash_branch(monkeypatch, tmp_path):
    import servora.source_import as source_import

    compose = {
        "services": {
            "web": {
                "image": "nginx:alpine",
            }
        }
    }
    attempts = []

    def fake_raw_file(owner, repo, branch, path):
        attempts.append((owner, repo, branch, path))
        if branch != "feature/import":
            raise source_import.SourceImportError("not found")
        return (
            f"https://raw.githubusercontent.com/{owner}/{repo}/{branch}/{path}",
            compose,
        )

    monkeypatch.setattr(source_import, "_github_raw_file", fake_raw_file)
    monkeypatch.setattr(source_import, "_validate_url", lambda url: url)
    store = MarketplaceStore(tmp_path)
    saved = store.import_url(
        "https://github.com/example/project/blob/feature/import/compose.yml"
    )

    assert saved.manifest["services"][0]["image"] == "nginx:alpine"
    assert saved.source["type"] == "github_file"
    assert saved.source["branch"] == "feature/import"
    assert saved.source["path"] == "compose.yml"
    assert ("example", "project", "feature/import", "compose.yml") in attempts


def test_marketplace_github_blob_branch_probe_is_bounded(monkeypatch, tmp_path):
    import servora.source_import as source_import

    attempts = []

    def fake_raw_file(owner, repo, branch, path):
        attempts.append((branch, path))
        raise source_import.SourceImportError("not found")

    monkeypatch.setattr(source_import, "_github_raw_file", fake_raw_file)
    monkeypatch.setattr(source_import, "_validate_url", lambda url: url)
    store = MarketplaceStore(tmp_path)

    try:
        store.import_url(
            "https://github.com/example/project/blob/"
            "a/b/c/d/e/f/g/h/i/j/k/l/m/compose.yml"
        )
    except MarketplaceError:
        pass
    else:
        raise AssertionError("missing GitHub blob should fail")

    assert len(attempts) == 12


def test_marketplace_import_rejects_non_443_https_port():
    from servora.source_import import SourceImportError, _validate_url

    try:
        _validate_url("https://example.com:8443/source.yaml")
    except SourceImportError as exc:
        assert "port 443" in str(exc)
    else:
        raise AssertionError("non-443 HTTPS import URL should be rejected")


def test_marketplace_import_rejects_invalid_url_port():
    from servora.source_import import SourceImportError, _validate_url

    try:
        _validate_url("https://example.com:bad/source.yaml")
    except SourceImportError as exc:
        assert "invalid port" in str(exc)
    else:
        raise AssertionError("invalid import URL port should be rejected")


def test_marketplace_import_urls_enforces_server_side_limit(tmp_path):
    from servora.marketplace import MarketplaceError, MarketplaceStore

    store = MarketplaceStore(tmp_path)
    try:
        store.import_urls([f"https://example.com/{index}.yaml" for index in range(21)])
    except MarketplaceError as exc:
        assert "maximum of 20" in str(exc)
    else:
        raise AssertionError("server-side marketplace URL limit should be enforced")


def test_marketplace_import_url_rejects_overlong_url(tmp_path):
    store = MarketplaceStore(tmp_path)
    try:
        store.import_url("https://example.com/" + ("a" * 4097))
    except MarketplaceError as exc:
        assert "too long" in str(exc)
    else:
        raise AssertionError("overlong marketplace URL should be rejected")


def test_marketplace_import_url_normalizes_surrounding_whitespace(monkeypatch, tmp_path):
    import servora.source_import as source_import

    compose = b"services:\n  web:\n    image: nginx:alpine\n"

    def fake_fetch(url, accept="*/*"):
        assert url == "https://example.com/compose.yml"
        return url, compose, {"content_type": "text/yaml"}

    monkeypatch.setattr(source_import, "_fetch", fake_fetch)
    monkeypatch.setattr(source_import, "_validate_url", lambda url: url)
    store = MarketplaceStore(tmp_path)
    saved = store.import_url("  https://example.com/compose.yml  ")
    assert saved.source["url"] == "https://example.com/compose.yml"
    assert saved.source["remote_url"] == "https://example.com/compose.yml"


def test_marketplace_install_preview_blocks_failed_preflight(monkeypatch, tmp_path):
    import json
    import threading
    from http.client import HTTPConnection
    from types import SimpleNamespace
    import servora.main as main

    class FakeEntry:
        name = "demo"
        category = "community"
        verification = "community"
        description = "Demo"
        tags = ["web"]
        source = {"type": "test"}
        manifest = entry()["manifest"]

        def to_dict(self):
            return {
                "name": self.name,
                "category": self.category,
                "verification": self.verification,
                "description": self.description,
                "tags": self.tags,
                "source": self.source,
                "manifest": self.manifest,
            }

    class FakeMarketplace:
        def get(self, name):
            return FakeEntry() if name == "demo" else None

    class FakeAppStore:
        def get(self, name):
            return None

    class FakePodman:
        pass

    monkeypatch.setattr(main, "marketplace", FakeMarketplace())
    monkeypatch.setattr(main, "app_store", FakeAppStore())
    monkeypatch.setattr(main, "podman", FakePodman())
    monkeypatch.setattr(main, "preflight_manifest", lambda *args, **kwargs: {
        "ok": False,
        "findings": [{"severity": "error", "code": "low_disk_space", "message": "not enough space"}],
    })
    monkeypatch.setattr(main.runtime, "root", tmp_path)

    server = main.ThreadingHTTPServer(("127.0.0.1", 0), main.Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        conn = HTTPConnection("127.0.0.1", server.server_port, timeout=3)
        conn.request("GET", "/api/marketplace/install-preview?name=demo")
        response = conn.getresponse()
        payload = json.loads(response.read())
        conn.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)

    assert response.status == 409
    assert payload["status"] == "blocked"
    assert payload["preflight"]["ok"] is False
    assert payload["preflight"]["findings"][0]["code"] == "low_disk_space"


def test_marketplace_install_endpoint_rolls_back_after_metadata_failure(monkeypatch, tmp_path):
    import json
    import threading
    from http.client import HTTPConnection

    import servora.main as main

    class FakeEntry:
        name = "demo"
        category = "community"
        verification = "community"
        manifest = _entry("demo")["manifest"]
        source = {"type": "test"}

        def to_dict(self):
            return {
                "name": self.name,
                "category": self.category,
                "verification": self.verification,
                "manifest": self.manifest,
                "source": self.source,
            }

    class FakeMarketplace:
        def get(self, name):
            return FakeEntry() if name == "demo" else None

    class FakeAppStore:
        def get(self, name):
            return None

        def save(self, manifest):
            raise RuntimeError("metadata write failed")

        def remove(self, name):
            removed.append(name)

    class FakePodman:
        pass

    removed = []
    uninstalled = []
    monkeypatch.setattr(main, "marketplace", FakeMarketplace())
    monkeypatch.setattr(main, "app_store", FakeAppStore())
    monkeypatch.setattr(main, "podman", FakePodman())
    monkeypatch.setattr(main, "validate_app_manifest", lambda manifest: manifest)
    monkeypatch.setattr(
        main,
        "preflight_manifest",
        lambda *args, **kwargs: {"ok": True, "findings": []},
    )
    monkeypatch.setattr(
        main,
        "install_app",
        lambda *args, **kwargs: {"created_containers": ["demo-web"]},
    )
    monkeypatch.setattr(
        main,
        "capture_app_state",
        lambda *args, **kwargs: {"services": [{"name": "web"}]},
    )
    monkeypatch.setattr(
        main,
        "uninstall_app",
        lambda *args, **kwargs: uninstalled.append(args[1].name) or {"removed": ["demo-web"]},
    )

    deployment_dir = tmp_path / "metadata" / "deployments"
    deployment_dir.mkdir(parents=True)
    monkeypatch.setattr(main.runtime, "root", tmp_path)

    server = main.ThreadingHTTPServer(("127.0.0.1", 0), main.Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        conn = HTTPConnection("127.0.0.1", server.server_port, timeout=3)
        body = json.dumps({"name": "demo"}).encode()
        conn.request(
            "POST",
            "/api/marketplace/install",
            body=body,
            headers={"Content-Type": "application/json", "Content-Length": str(len(body))},
        )
        response = conn.getresponse()
        payload = json.loads(response.read())
        conn.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)

    assert response.status == 400
    assert "metadata write failed" in payload["error"]
    assert uninstalled == ["demo"]
    assert removed == ["demo"]
