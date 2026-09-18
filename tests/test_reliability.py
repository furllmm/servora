from pathlib import Path

from servora.apps import validate_app_manifest
from servora.reliability import preflight_manifest, validate_runtime_state


class FakePodman:
    def list_images(self):
        return [{"RepoTags": ["nginx:alpine"]}]


def test_preflight_detects_missing_local_image(tmp_path: Path):
    m = validate_app_manifest({"name": "demo", "services": [{"name": "web", "image": "nginx:missing"}]})
    result = preflight_manifest(FakePodman(), m, tmp_path, check_images=True)
    assert result["ok"] is True
    assert any(x["code"] == "image_not_local" for x in result["findings"])


def test_runtime_state_is_valid(tmp_path: Path):
    for name in ["config", "metadata", "logs", "podman", "apps", "backups"]:
        (tmp_path / name).mkdir()
    (tmp_path / "config" / "servora.json").write_text('{"version": 1, "mode": "user"}', encoding="utf-8")
    assert validate_runtime_state(tmp_path)["ok"] is True


def test_runtime_state_detects_missing_path(tmp_path: Path):
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "servora.json").write_text('{"version": 1}', encoding="utf-8")
    result = validate_runtime_state(tmp_path)
    assert result["ok"] is False
    assert any(x["code"] == "missing_runtime_path" for x in result["findings"])


class ScanPodman:
    def list_containers(self, all=True):
        return [{"Names": ["servora-demo-web"]}, {"Names": ["servora-orphan-web"]}]


def test_scan_detects_orphan_and_missing(tmp_path):
    from servora.reliability import scan_reliability
    (tmp_path / "apps").mkdir()
    (tmp_path / "config").mkdir()
    (tmp_path / "metadata").mkdir()
    (tmp_path / "logs").mkdir()
    (tmp_path / "apps" / "demo.json").write_text(
        '{"name":"demo","version":"1","services":[{"name":"web","image":"nginx"}]}'
    )
    result = scan_reliability(ScanPodman(), tmp_path)
    codes = {x["code"] for x in result["findings"]}
    assert "orphan_container" in codes
    assert "missing_container" not in codes


def test_scan_detects_corrupt_app_state(tmp_path):
    from servora.reliability import scan_reliability
    (tmp_path / "apps").mkdir()
    (tmp_path / "logs").mkdir()
    (tmp_path / "apps" / "broken.json").write_text("{broken")
    result = scan_reliability(ScanPodman(), tmp_path)
    assert result["ok"] is False
    assert any(x["code"] == "corrupt_app_state" for x in result["findings"])


class RepairPodman(ScanPodman):
    def __init__(self):
        self.removed = []

    def remove_container(self, name, force=False):
        self.removed.append((name, force))
        return name

    def list_containers(self, all=True):
        return [{"Names": ["servora-orphan-web"]}]


def test_repair_requires_approval(tmp_path):
    from servora.reliability import repair_reliability
    result = repair_reliability(RepairPodman(), tmp_path, "remove_orphan_container",
                                name="servora-orphan-web", approved=False)
    assert result["status"] == "approval_required"


def test_repair_removes_detected_orphan(tmp_path):
    from servora.reliability import repair_reliability
    p = RepairPodman()
    result = repair_reliability(p, tmp_path, "remove_orphan_container",
                                name="servora-orphan-web", approved=True)
    assert result["status"] == "repaired"
    assert p.removed == [("servora-orphan-web", True)]

def test_runtime_state_reports_disk_usage(tmp_path: Path):
    for name in ["config", "metadata", "logs", "podman", "apps", "backups"]:
        (tmp_path / name).mkdir()
    (tmp_path / "config" / "servora.json").write_text('{"version": 1}', encoding="utf-8")
    result = validate_runtime_state(tmp_path)
    assert "disk" in result
    assert result["disk"]["total_bytes"] > 0
    assert result["disk"]["free_bytes"] >= 0


def test_preflight_min_free_space_is_configurable(tmp_path: Path):
    m = validate_app_manifest({"name": "demo", "services": [{"name": "web", "image": "nginx"}]})
    result = preflight_manifest(FakePodman(), m, tmp_path, min_free_bytes=10**30)
    assert result["ok"] is False
    assert any(x["code"] == "low_disk_space" for x in result["findings"])


def test_preflight_rejects_negative_disk_threshold(tmp_path: Path):
    m = validate_app_manifest({"name": "demo", "services": [{"name": "web", "image": "nginx"}]})
    try:
        preflight_manifest(FakePodman(), m, tmp_path, min_free_bytes=-1)
    except ValueError as exc:
        assert "non-negative" in str(exc)
    else:
        raise AssertionError("negative threshold must fail")


def test_validate_backup_tar_and_checksum(tmp_path: Path):
    import hashlib
    import tarfile
    source = tmp_path / "data.txt"
    source.write_text("servora", encoding="utf-8")
    archive = tmp_path / "backup.tar"
    with tarfile.open(archive, "w") as tar:
        tar.add(source, arcname="data/data.txt")
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    from servora.reliability import validate_backup_artifact
    result = validate_backup_artifact(archive, checksum=digest)
    assert result["ok"] is True


def test_validate_backup_rejects_traversal(tmp_path: Path):
    import io
    import tarfile
    archive = tmp_path / "bad.tar"
    with tarfile.open(archive, "w") as tar:
        info = tarfile.TarInfo("../escape.txt")
        data = b"bad"
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
    from servora.reliability import validate_backup_artifact
    result = validate_backup_artifact(archive)
    assert result["ok"] is False
    assert any(x["code"] == "unsafe_backup_path" for x in result["findings"])


def test_validate_backup_checksum_mismatch(tmp_path: Path):
    archive = tmp_path / "backup.tar"
    archive.write_bytes(b"not-a-real-tar")
    from servora.reliability import validate_backup_artifact
    result = validate_backup_artifact(archive, checksum="0" * 64)
    assert result["ok"] is False
    codes = {x["code"] for x in result["findings"]}
    assert "checksum_mismatch" in codes
    assert "invalid_backup_archive" in codes
