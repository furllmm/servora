"""Servora HTTP sunucusunun gerçek (mock Podman'sız) davranış testleri.

Bu testler Podman gerektirmez; Podman yokken API'nin nazikçe hata verdiğini,
portable modda storage düzeninin gerçekten oluşturulduğunu ve dashboard'un
sunulduğunu doğrular. Yalnızca 127.0.0.1 üzerinde rastgele port kullanılır.
"""

from __future__ import annotations

import json
import subprocess
import sys
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest


@pytest.fixture()
def portable_server(tmp_path, monkeypatch):
    """SERVORA_MODE=portable ile main.Handler'ı geçici kök üzerinde başlatır."""
    root = tmp_path / "Servora"
    monkeypatch.setenv("SERVORA_ROOT", str(root))
    monkeypatch.setenv("SERVORA_MODE", "portable")
    # Modül seviyesindeki singleton'ları bu tmp köke yeniden bağla.
    subprocess.run([sys.executable, "-c", "import servora.main"], check=False)
    from servora import main as m
    from servora.runtime import Runtime
    from servora.apps import AppStore
    from servora.audit import AuditLog

    runtime = Runtime(root, "portable")
    runtime.initialize()
    monkeypatch.setattr(m, "runtime", runtime)
    monkeypatch.setattr(m, "ROOT", root)
    monkeypatch.setattr(m, "MODE", "portable")
    monkeypatch.setattr(m, "app_store", AppStore(runtime.root))
    monkeypatch.setattr(m, "audit", AuditLog(runtime.root))

    server = ThreadingHTTPServer(("127.0.0.1", 0), m.Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    yield base, root
    server.shutdown()
    server.server_close()
    thread.join(timeout=5)


def _get(base: str, path: str):
    req = urllib.request.Request(base + path)
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def test_status_reports_portable_mode_and_disk(portable_server):
    base, root = portable_server
    status, body = _get(base, "/api/status")
    data = json.loads(body)
    assert status == 200
    assert data["mode"] == "portable"
    assert data["runtime"] == str(root)
    assert data["reliability"]["ok"] is True
    assert data["reliability"]["disk"]["free_bytes"] >= 0


def test_portable_initialize_creates_podman_storage_layout(portable_server):
    """Klasör varlığı değil: storage.conf gerçek içerikle ve yollar ROOT altında."""
    base, root = portable_server
    conf = root / "podman" / "storage.conf"
    graphroot = root / "podman" / "storage"
    runroot = root / "podman" / "runroot"
    assert conf.is_file()
    text = conf.read_text(encoding="utf-8")
    assert "[storage]" in text
    assert str(graphroot) in text and str(runroot) in text
    assert graphroot.is_dir() and runroot.is_dir()
    # Sistem Podman yapılandırmasına dokunulmadı (yalnızca env override).
    assert "CONTAINERS_STORAGE_CONF" not in __import__("os").environ


def test_dashboard_html_is_served(portable_server):
    base, _ = portable_server
    status, body = _get(base, "/")
    assert status == 200
    assert b"<script" in body
    # Bilinen geri kaçış hatası kalmamalı: JS içinde '\\'' dizisi olmamalı.
    assert b"\\\\'" not in body


def test_container_endpoints_fail_gracefully_without_podman(portable_server):
    base, _ = portable_server
    from servora import main as m
    if m.podman is not None:
        pytest.skip("Bu ortamda Podman mevcut; hataya-dayalı test atlandı")
    status, body = _get(base, "/api/containers")
    assert status == 400
    assert "Podman" in json.loads(body)["error"]
    # Sunucu ayakta kalmalı (bağlantı kapanmamalı):
    status2, _ = _get(base, "/api/status")
    assert status2 == 200


def test_invalid_app_name_rejected(portable_server):
    base, _ = portable_server
    status, body = _get(base, "/api/marketplace/get?name=" + "../etc/passwd")
    assert status == 400
    assert "error" in json.loads(body)
