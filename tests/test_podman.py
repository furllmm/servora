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
        return type("R", (), {"returncode": 0, "stdout": '[]\n', "stderr": ""})()
    monkeypatch.setattr("servora.podman.subprocess.run", fake_run)
    assert p.stats("demo") == []
    assert calls[0] == ["podman", "stats", "--no-stream", "--format", "json", "demo"]
