from __future__ import annotations

import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .podman import Podman, PodmanError
from .apps import (AppManifestError, AppStore, install_app, manifest_to_dict,
                   uninstall_app, update_app, validate_app_manifest)
from .runtime import Runtime
from .marketplace import MarketplaceError, MarketplaceStore
from .marketplace_seed import DEMO
from .ai_import import AIImportError, import_source
from .ai import AIPlanError, create_container_plan, provider_from_env
from .ai_execution import analyze_plan, execute_plan
from .server_browser import BareServerBrowser, validate_server_url

ROOT = Path(os.environ.get("SERVORA_ROOT", Path.home() / ".servora"))
MODE = os.environ.get("SERVORA_MODE", "user")
runtime = Runtime(ROOT, MODE)
runtime.initialize()
app_store = AppStore(runtime.root)
marketplace = MarketplaceStore(runtime.root)
marketplace.seed([DEMO])

try:
    podman = Podman(env=runtime.podman_environment() if MODE == "portable" else None)
except PodmanError:
    podman = None

browser = BareServerBrowser()
WEB = Path(__file__).parent.parent / "web"


def _name(value: str) -> str:
    if not value or len(value) > 128 or any(c in value for c in "\x00\n\r"):
        raise ValueError("Invalid container name")
    return value


class Handler(BaseHTTPRequestHandler):
    def _json(self, obj, status=200):
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _require_podman(self):
        if podman is None:
            raise PodmanError("Podman is not available")
        return podman

    def do_GET(self):
        parsed = urlparse(self.path)
        try:
            if parsed.path == "/api/status":
                available = podman is not None
                version = None
                if podman:
                    try:
                        version = podman.version()
                    except PodmanError:
                        available = False
                return self._json({
                    "podman": available,
                    "version": version,
                    "runtime": str(runtime.root),
                    "mode": runtime.mode,
                    "server_browser": {"available": browser.available, "mode": "bare"},
                })

            if parsed.path == "/api/podman/help":
                return self._json({"detected": Podman.detect() is not None, "install": Podman.install_help()})

            p = self._require_podman()
            if parsed.path == "/api/ai/provider":
                provider = provider_from_env()
                return self._json({"provider": getattr(provider, "name", "unknown")})
            if parsed.path == "/api/apps":
                return self._json([manifest_to_dict(app) for app in app_store.list()])
            if parsed.path == "/api/marketplace":
                q = parse_qs(parsed.query)
                category = q.get("category", [None])[0]
                query = q.get("q", [None])[0]
                return self._json([x.to_dict() for x in marketplace.list(category=category, query=query)])
            if parsed.path == "/api/marketplace/get":
                name = _name(parse_qs(parsed.query).get("name", [""])[0])
                entry = marketplace.get(name)
                if entry is None:
                    return self._json({"error": "marketplace app not found"}, 404)
                return self._json(entry.to_dict())
            if parsed.path == "/api/apps/get":
                name = _name(parse_qs(parsed.query).get("name", [""])[0])
                app = app_store.get(name)
                if app is None:
                    return self._json({"error": "app not found"}, 404)
                return self._json(manifest_to_dict(app))
            if parsed.path == "/api/containers":
                return self._json(p.list_containers())
            if parsed.path == "/api/images":
                return self._json(p.list_images())
            if parsed.path == "/api/volumes":
                return self._json(p.list_volumes())
            if parsed.path == "/api/networks":
                return self._json(p.list_networks())
            if parsed.path == "/api/containers/inspect":
                return self._json(p.inspect_container(_name(parse_qs(parsed.query).get("name", [""])[0])))
            if parsed.path == "/api/containers/logs":
                name = _name(parse_qs(parsed.query).get("name", [""])[0])
                tail = int(parse_qs(parsed.query).get("tail", [200])[0])
                return self._json({"name": name, "logs": p.container_logs(name, tail)})
            if parsed.path == "/api/containers/stats":
                name = parse_qs(parsed.query).get("name", [None])[0]
                return self._json(p.stats(_name(name) if name else None))
            if parsed.path == "/api/server-browser":
                url = parse_qs(parsed.query).get("url", [""])[0]
                validate_server_url(url)
                return self._json(browser.open(url))
            if parsed.path in ("/", "/index.html"):
                body = (WEB / "index.html").read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            self._json({"error": "not found"}, 404)
        except (ValueError, PodmanError) as exc:
            self._json({"error": str(exc)}, 400)

    def do_POST(self):
        parsed = urlparse(self.path)
        try:
            p = self._require_podman()
            if parsed.path in {"/api/apps/install", "/api/apps/update", "/api/marketplace/publish", "/api/ai/import", "/api/ai/plan", "/api/ai/create"}:
                length = int(self.headers.get("Content-Length", "0"))
                if length <= 0 or length > 512 * 1024:
                    raise ValueError("Manifest body must be between 1 byte and 512 KiB")
                raw = json.loads(self.rfile.read(length))
                if parsed.path == "/api/ai/plan":
                    prompt = raw.get("prompt") if isinstance(raw, dict) else None
                    provider = provider_from_env()
                    result = create_container_plan(provider, prompt)
                    return self._json({"provider": provider.name, "plan": result})

                if parsed.path == "/api/ai/create":
                    prompt = raw.get("prompt") if isinstance(raw, dict) else None
                    approved = bool(raw.get("approved", False)) if isinstance(raw, dict) else False
                    provider = provider_from_env()
                    plan = create_container_plan(provider, prompt)
                    manifest, findings, requires_approval = analyze_plan(plan)
                    response = {
                        "provider": provider.name,
                        "plan": plan,
                        "manifest": manifest,
                        "findings": [f.to_dict() for f in findings],
                    }
                    if requires_approval and not approved:
                        return self._json({"status": "approval_required", **response}, 409)
                    result = execute_plan(p, manifest)
                    return self._json({"status": "launched", **response, "result": result}, 201)

                if parsed.path == "/api/ai/import":
                    source = raw.get("source") if isinstance(raw, dict) else None
                    approval = bool(raw.get("approved", False)) if isinstance(raw, dict) else False
                    result = import_source(source)
                    if result.requires_approval and not approval:
                        return self._json({"status": "approval_required", **result.to_dict()}, 409)
                    entry_raw = {
                        "name": result.manifest["name"],
                        "category": "ai_imported",
                        "verification": "risk_detected" if result.findings else "ai_imported",
                        "manifest": result.manifest,
                        "source": result.source | {"analysis_engine": result.engine, "findings": [f.to_dict() for f in result.findings]},
                        "tags": ["ai-imported"],
                    }
                    entry = marketplace.save(entry_raw)
                    return self._json({"status": "approved", **result.to_dict(), "marketplace": entry.to_dict()}, 201)

                if parsed.path == "/api/marketplace/publish":
                    entry = marketplace.publish(raw)
                    return self._json(entry.to_dict(), 201)
                manifest = validate_app_manifest(raw)
                if parsed.path == "/api/apps/install":
                    if app_store.get(manifest.name) is not None:
                        raise AppManifestError("App is already installed; use update")
                    result = install_app(p, raw)
                    app_store.save(manifest)
                    return self._json({"app": manifest_to_dict(manifest), "created": result}, 201)
                old = app_store.get(manifest.name)
                if old is None:
                    raise AppManifestError("App is not installed")
                result = update_app(p, old, raw, store=app_store)
                return self._json(result)

            if parsed.path == "/api/apps/uninstall":
                q = parse_qs(parsed.query)
                name = _name(q.get("name", [""])[0])
                app = app_store.get(name)
                if app is None:
                    raise AppManifestError("App is not installed")
                result = uninstall_app(
                    p, app,
                    remove_volumes=q.get("remove_volumes", ["0"])[0] == "1",
                    remove_networks=q.get("remove_networks", ["0"])[0] == "1",
                )
                app_store.remove(name)
                return self._json({"app": name, "uninstalled": True, **result})

            name = _name(parse_qs(parsed.query).get("name", [""])[0])
            actions = {
                "/api/containers/start": p.start_container,
                "/api/containers/stop": p.stop_container,
                "/api/containers/restart": p.restart_container,
            }
            action = actions.get(parsed.path)
            if action is None:
                return self._json({"error": "not found"}, 404)
            return self._json({"name": name, "result": action(name)})
        except (ValueError, PodmanError, AppManifestError, MarketplaceError, AIImportError, AIPlanError, json.JSONDecodeError) as exc:
            self._json({"error": str(exc)}, 400)

    def log_message(self, *_args):
        pass


def run(host="127.0.0.1", port=8787):
    ThreadingHTTPServer((host, port), Handler).serve_forever()


if __name__ == "__main__":
    run()
