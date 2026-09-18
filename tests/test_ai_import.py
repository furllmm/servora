from servora.ai_import import import_source


def test_import_oci_image_builds_manifest():
    result = import_source({"type": "oci_image", "image": "nginx:alpine", "name": "web"})
    assert result.manifest["name"] == "web"
    assert result.manifest["services"][0]["image"] == "nginx:alpine"
    assert result.requires_approval is False


def test_compose_conversion_and_risk_scan():
    result = import_source({
        "type": "compose",
        "name": "demo",
        "raw": {"services": {"web": {
            "image": "nginx:alpine",
            "ports": ["8080:80"],
            "volumes": ["./data:/data", "/var/run/docker.sock:/var/run/docker.sock"],
            "privileged": True,
        }}}
    })
    codes = {f.code for f in result.findings}
    assert "privileged" in codes
    assert "docker_socket" in codes
    assert "host_mount" in codes
    assert result.requires_approval is True


def test_compose_json_environment_and_dependencies():
    result = import_source({
        "type": "compose", "name": "stack",
        "raw": {"services": {
            "db": {"image": "postgres:16", "environment": ["POSTGRES_PASSWORD=x"]},
            "web": {"image": "nginx:alpine", "depends_on": {"db": {"condition": "service_started"}}},
        }}
    })
    web = next(s for s in result.manifest["services"] if s["name"] == "web")
    assert web["depends_on"] == ["db"]
    db = next(s for s in result.manifest["services"] if s["name"] == "db")
    assert db["environment"]["POSTGRES_PASSWORD"] == "x"
