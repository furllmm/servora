from pathlib import Path

from servora.backup import restore_preview


def test_backup_preview_rejects_missing_file(tmp_path: Path):
    import pytest
    with pytest.raises(Exception):
        restore_preview(tmp_path / "missing.srv.zst")


def test_restore_requires_approval(tmp_path):
    from servora.backup import restore_backup
    import pytest
    with pytest.raises(Exception, match="explicit approval"):
        restore_backup(tmp_path / "backup.srv.zst", tmp_path, object(), approved=False)


def test_restore_rejects_non_safe_mode(tmp_path):
    from servora.backup import restore_backup
    import pytest
    with pytest.raises(Exception, match="safe restore mode"):
        restore_backup(tmp_path / "backup.srv.zst", tmp_path, object(), approved=True, mode="replace")


def test_safe_member_rejects_traversal():
    from servora.backup import _safe_member
    assert _safe_member("config/app.json")
    assert not _safe_member("../config/app.json")
    assert not _safe_member("/etc/passwd")


def test_restore_rolls_back_created_resources(tmp_path, monkeypatch):
    import servora.backup as backup

    class FakePodman:
        def __init__(self):
            self.containers = set()
            self.volumes = set()
            self.networks = set()

        def list_containers(self, all=True):
            return [{"Names": list(self.containers)}]

        def list_volumes(self):
            return [{"Name": x} for x in self.volumes]

        def list_networks(self):
            return [{"Name": x} for x in self.networks]

        def create_container(self, plan):
            self.containers.add(plan["name"])
            return "id"

        def create_volume(self, name):
            self.volumes.add(name)
            return name

        def create_network(self, name):
            self.networks.add(name)
            return name

        def remove_container(self, name, force=False):
            self.containers.discard(name)

        def remove_volume(self, name, force=False):
            self.volumes.discard(name)

        def remove_network(self, name):
            self.networks.discard(name)

        def start_container(self, name):
            return name

        def container_health(self, name):
            raise RuntimeError("health check failed")

    manifest = {
        "format": "servora-backup",
        "format_version": 1,
        "apps": [{
            "name": "demo",
            "version": "1",
            "services": [{
                "name": "web",
                "image": "nginx",
                "networks": ["demo-net"],
                "volumes": ["demo-data:/data"],
            }],
        }],
        "volumes_included": False,
        "resources": {"containers": [], "images": [], "networks": [], "volumes": []},
    }

    monkeypatch.setattr(backup, "inspect_backup", lambda path: {"manifest": manifest})
    monkeypatch.setattr(backup, "_extract_backup", lambda path, target: (target / "payload").mkdir() or manifest)
    monkeypatch.setattr(
        backup,
        "restore_conflicts",
        lambda path, root, podman: {
            "safe": True, "conflicts": {"apps": [], "containers": [], "networks": [], "volumes": []},
            "missing_images": [], "missing_volume_payload": [], "required_bytes_estimate": 0,
            "free_bytes": 10**12, "apps": ["demo"], "volume_payload_count": 0, "warnings": []
        },
    )
    monkeypatch.setattr(
        backup,
        "install_app",
        lambda podman, raw_manifest, transaction_root=None: (
            podman.create_network("demo-net"),
            podman.create_volume("demo-data"),
            podman.create_container({"name": "servora-demo-web", "image": "nginx"}),
        ),
    )

    p = FakePodman()
    with __import__("pytest").raises(backup.BackupError, match="Restore failed"):
        backup.restore_backup(tmp_path / "backup.srv.zst", tmp_path, p, approved=True)

    assert not p.containers
    assert not p.volumes
    assert not p.networks


def test_create_backup_roundtrip_with_fake_podman(tmp_path, monkeypatch):
    """Gerçek export→inspect→restore akışı (FakePodman; entegrasyon testi DEĞİLDİR)."""
    import json
    import pytest

    zstd = __import__("shutil").which("zstd")
    if not zstd:
        pytest.skip("zstd binary not available in this environment")

    import servora.backup as backup_mod
    from servora.runtime import Runtime

    class FakePodman:
        def list_containers(self, all=True):
            return []

        def list_volumes(self):
            return []

        def list_networks(self):
            return []

        def image_exists(self, name):
            return True

    runtime = Runtime(tmp_path / "root", mode="portable")
    runtime.initialize()
    # Basit bir config dosyası ekle (backup kapsamını doğrulamak için)
    (runtime.config / "extra.json").write_text('{"k": 1}', encoding="utf-8")

    out = tmp_path / "backup.srv.zst"
    result = backup_mod.create_backup(runtime.root, out, FakePodman(), include_volumes=False)
    assert out.is_file()
    assert result["sha256"] and result["size_bytes"] > 0
    assert result["validation"]["ok"] is True

    info = backup_mod.inspect_backup(out)
    manifest = info["manifest"]
    assert manifest["format"] == "servora-backup"
    assert manifest["volumes_included"] is False

    # Restore hedefi ayrı bir kök olsun (mevcut veriler korunur).
    target = tmp_path / "restored"
    restored = backup_mod.restore_backup(out, target, FakePodman(), approved=True, mode="safe")
    assert restored.get("ok", True) is not False
    assert (target / "config" / "extra.json").is_file()
    assert json.loads((target / "config" / "extra.json").read_text()) == {"k": 1}


def test_backup_rejects_bad_filename(tmp_path):
    import pytest
    from servora.backup import create_backup, BackupError

    with pytest.raises(BackupError, match=".srv.zst"):
        create_backup(tmp_path, tmp_path / "backup.tar.gz", object())
