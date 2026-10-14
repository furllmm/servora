from pathlib import Path

import pytest

from servora.runtime import Runtime


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
