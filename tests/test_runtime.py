from pathlib import Path

import pytest

from servora.runtime import PortableStorageMovedError, Runtime


def test_runtime_initializes(tmp_path):
    runtime = Runtime(tmp_path / "servora", mode="portable")
    runtime.initialize()
    assert runtime.config_file.exists()
    assert runtime.podman_environment()["CONTAINERS_STORAGE_CONF"].endswith("storage.conf")


def test_invalid_mode_raises(tmp_path):
    with pytest.raises(ValueError):
        Runtime(tmp_path / "servora", mode="hybrid")


def test_user_mode_does_not_create_podman_storage_layout(tmp_path):
    """Normal kullanıcı modu Podman storage düzeni oluşturmamalıdır."""
    runtime = Runtime(tmp_path / "usermode", mode="user")
    runtime.initialize()
    env = runtime.podman_environment()
    # podman dizini Servora tarafından yönetilen metadata içindir; ancak
    # storage.conf / graphroot / runroot yalnızca portable modda üretilir.
    assert not Path(env["CONTAINERS_STORAGE_CONF"]).exists()
    assert not Path(env["CONTAINERS_GRAPHROOT"]).exists()
    assert not Path(env["CONTAINERS_RUNROOT"]).exists()


def test_portable_initialize_materializes_storage_conf(tmp_path):
    """Portable modda initialize(), CONTAINERS_STORAGE_CONF'u gerçek bir dosyaya bağlamalı."""
    runtime = Runtime(tmp_path / "portable", mode="portable")
    runtime.initialize()
    env = runtime.podman_environment()
    conf = Path(env["CONTAINERS_STORAGE_CONF"])
    graphroot = Path(env["CONTAINERS_GRAPHROOT"])
    runroot = Path(env["CONTAINERS_RUNROOT"])
    assert conf.is_file()
    assert graphroot.is_dir()
    assert runroot.is_dir()
    text = conf.read_text(encoding="utf-8")
    assert "[storage]" in text
    assert str(graphroot) in text
    assert str(runroot) in text


def test_portable_storage_conf_is_idempotent_and_preserves_edits(tmp_path):
    """Yeniden initialize mevcut kullanıcı düzenlemesinin üzerine yazmamalı."""
    runtime = Runtime(tmp_path / "portable2", mode="portable")
    runtime.initialize()
    conf = Path(runtime.podman_environment()["CONTAINERS_STORAGE_CONF"])
    original = conf.read_text(encoding="utf-8")
    conf.write_text(original + "# user edit\n", encoding="utf-8")
    runtime.initialize()
    assert conf.read_text(encoding="utf-8").endswith("# user edit\n")


def test_portable_environment_paths_are_under_root(tmp_path):
    """Tüm portable storage yolları SERVORA_ROOT altında kalmalı (taşınabilirlik)."""
    root = tmp_path / "Servora"
    runtime = Runtime(root, mode="portable")
    runtime.initialize()
    for value in runtime.podman_environment().values():
        assert Path(value).resolve().is_relative_to(root.resolve())


def test_portable_storage_conf_contains_driver_and_paths(tmp_path):
    """Generated storage.conf must be self-sufficient: CONTAINERS_STORAGE_CONF
    replaces containers.conf entirely, so driver/graphroot/runroot all live in it."""
    runtime = Runtime(tmp_path / "portable3", mode="portable")
    runtime.initialize()
    text = runtime.storage_conf.read_text(encoding="utf-8")
    assert '[storage]' in text
    assert 'driver = "' in text
    assert f'graphroot = "{runtime.storage_root}"' in text
    assert f'runroot = "{runtime.run_root}"' in text


def test_choose_storage_driver_returns_supported_pair():
    from servora.runtime import choose_storage_driver

    driver, mount_program = choose_storage_driver(Path("/tmp"))
    assert driver in {"overlay", "vfs"}
    if driver == "overlay" and mount_program is not None:
        assert Path(mount_program).is_file()


def test_moved_root_with_empty_old_storage_rewrites_conf(tmp_path):
    """Roots moved to a new location; old graphroot dir missing -> conf is
    regenerated silently (no data at risk)."""
    old_root = tmp_path / "Servora-old"
    runtime = Runtime(old_root, mode="portable")
    runtime.initialize()
    new_root = tmp_path / "Servora-new"
    new_root.mkdir()
    # simulate move of the tree without the storage payload
    import shutil as _sh
    _sh.copytree(old_root / "podman", new_root / "podman")
    moved = Runtime(new_root, mode="portable")
    moved.initialize()  # must not raise: old location does not exist
    text = moved.storage_conf.read_text(encoding="utf-8")
    assert f'graphroot = "{moved.storage_root}"' in text


def test_moved_root_with_data_in_old_storage_raises(tmp_path):
    """Data-loss guard: when the old absolute graphroot still exists and holds
    entries, initialize() must refuse to rewrite anything."""
    old_root = tmp_path / "Servora-data"
    runtime = Runtime(old_root, mode="portable")
    runtime.initialize()
    marker = runtime.storage_root / "keepme"
    marker.write_text("precious", encoding="utf-8")

    new_root = tmp_path / "Servora-moved"
    import shutil as _sh
    _sh.copytree(old_root, new_root)
    # Remove the copied payload so only the OLD location has data.
    _sh.rmtree(new_root / "podman" / "storage")
    (new_root / "podman" / "storage").mkdir()

    moved = Runtime(new_root, mode="portable")
    with pytest.raises(PortableStorageMovedError):
        moved.initialize()
    # Old data untouched, conf unchanged.
    assert marker.read_text(encoding="utf-8") == "precious"


def test_relocate_requires_confirm_and_never_touches_data(tmp_path):
    from servora.runtime import PortableStorageMovedError

    old_root = tmp_path / "Reloc-old"
    runtime = Runtime(old_root, mode="portable")
    runtime.initialize()
    payload = runtime.storage_root / "vol"
    payload.mkdir()
    (payload / "data.txt").write_text("x", encoding="utf-8")

    new_root = tmp_path / "Reloc-new"
    import shutil as _sh
    _sh.copytree(old_root, new_root)
    _sh.rmtree(new_root / "podman" / "storage")
    (new_root / "podman" / "storage").mkdir()
    moved = Runtime(new_root, mode="portable")

    # Dry run reports but changes nothing.
    with pytest.raises(PortableStorageMovedError):
        moved.relocate_portable_storage(confirm=False)
    assert 'graphroot = "%s"' % runtime.storage_root in moved.storage_conf.read_text()

    # With data present, even confirm=True refuses.
    with pytest.raises(PortableStorageMovedError):
        moved.relocate_portable_storage(confirm=True)
    assert (payload / "data.txt").read_text(encoding="utf-8") == "x"

    # Operator archives/removes old data externally, then relocation succeeds.
    _sh.rmtree(old_root / "podman" / "storage")
    report = moved.relocate_portable_storage(confirm=True)
    assert report["relocated"] is True
    text = moved.storage_conf.read_text(encoding="utf-8")
    assert f'graphroot = "{moved.storage_root}"' in text
