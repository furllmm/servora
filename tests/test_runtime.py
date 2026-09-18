from servora.runtime import Runtime


def test_runtime_initializes(tmp_path):
    runtime = Runtime(tmp_path / "servora", mode="portable")
    runtime.initialize()
    assert runtime.config_file.exists()
    assert runtime.podman_environment()["CONTAINERS_STORAGE_CONF"].endswith("storage.conf")
