from __future__ import annotations

import json
import os
import shutil
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .podman import Podman, PodmanError
from .apps import (AppManifestError, AppStore, install_app, manifest_to_dict,
                   uninstall_app, update_app, validate_app_manifest, app_health)
from .runtime import Runtime
from .marketplace import MarketplaceError, MarketplaceStore
from .marketplace_seed import DEMO
from .ai_import import AIImportError, import_source
from .ai import AIPlanError, create_container_plan, provider_from_env
from .ai_execution import analyze_plan, execute_plan
from .troubleshooting import TroubleshootingError, collect_container_diagnostics, troubleshoot_container
from .recovery import RecoveryError, execute_recovery
from .recovery_policy import RecoveryPolicyError, RecoveryController
from .server_browser import BareServerBrowser, validate_server_url
from .audit import AuditLog, AuditError
from .reliability import ReliabilityError, preflight_manifest, validate_runtime_state, scan_reliability, repair_reliability
from .transaction import list_operations
from .backup import BackupError, create_backup, restore_preview, restore_conflicts, restore_backup

ROOT = Path(os.environ.get("SERVORA_ROOT", Path.home() / ".servora"))
MODE = os.environ.get("SERVORA_MODE", "user")
runtime = Runtime(ROOT, MODE)
runtime.initialize()
app_store = AppStore(runtime.root)
marketplace = MarketplaceStore(runtime.root)
marketplace.seed([DEMO])
audit = AuditLog(runtime.root)

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
                state = validate_runtime_state(runtime.root)
                return self._json({
                    "podman": available,
                    "version": version,
                    "runtime": str(runtime.root),
                    "mode": runtime.mode,
                    "server_browser": {"available": browser.available, "mode": "bare"},
                    "reliability": state,
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
            if parsed.path == "/api/apps/health":
                name = _name(parse_qs(parsed.query).get("name", [""])[0])
                app = app_store.get(name)
                if app is None:
                    return self._json({"error": "app not found"}, 404)
                return self._json(app_health(p, app))
            if parsed.path == "/api/apps/get":
                name = _name(parse_qs(parsed.query).get("name", [""])[0])
                app = app_store.get(name)
                if app is None:
                    return self._json({"error": "app not found"}, 404)
                return self._json(manifest_to_dict(app))
            if parsed.path == "/api/storage":
                usage = shutil.disk_usage(runtime.root)
                return self._json({
                    "path": str(runtime.root),
                    "total_bytes": usage.total,
                    "used_bytes": usage.total - usage.free,
                    "free_bytes": usage.free,
                })

            if parsed.path == "/api/containers":
                return self._json(p.list_containers())
            if parsed.path == "/api/images":
                return self._json(p.list_images())
            if parsed.path == "/api/images/inspect":
                return self._json(p.inspect_image(_name(parse_qs(parsed.query).get("name", [""])[0])))
            if parsed.path == "/api/volumes":
                return self._json(p.list_volumes())
            if parsed.path == "/api/volumes/inspect":
                return self._json(p.inspect_volume(_name(parse_qs(parsed.query).get("name", [""])[0])))
            if parsed.path == "/api/networks":
                return self._json(p.list_networks())
            if parsed.path == "/api/networks/inspect":
                return self._json(p.inspect_network(_name(parse_qs(parsed.query).get("name", [""])[0])))
            if parsed.path == "/api/containers/inspect":
                return self._json(p.inspect_container(_name(parse_qs(parsed.query).get("name", [""])[0])))
            if parsed.path == "/api/containers/logs":
                name = _name(parse_qs(parsed.query).get("name", [""])[0])
                tail = int(parse_qs(parsed.query).get("tail", [200])[0])
                return self._json({"name": name, "logs": p.container_logs(name, tail)})
            if parsed.path == "/api/containers/stats":
                name = parse_qs(parsed.query).get("name", [None])[0]
                return self._json(p.stats(_name(name) if name else None))
            if parsed.path == "/api/containers/health":
                name = _name(parse_qs(parsed.query).get("name", [""])[0])
                return self._json(p.container_health(name))
            if parsed.path == "/api/recovery/policy":
                controller = RecoveryController(p, runtime.root)
                return self._json(controller.store.load())
            if parsed.path == "/api/reliability":
                scan = scan_reliability(p, runtime.root)
                scan["runtime"] = validate_runtime_state(runtime.root)
                scan["operations"] = list_operations(runtime.root)
                return self._json(scan)
            if parsed.path == "/api/reliability/scan":
                return self._json(scan_reliability(p, runtime.root))
            if parsed.path == "/api/backups":
                backups = []
                for item in sorted(runtime.backups.glob("*.srv.zst")):
                    try:
                        backups.append({"name": item.name, "path": str(item), "size_bytes": item.stat().st_size, "modified": item.stat().st_mtime})
                    except OSError:
                        continue
                return self._json(backups)
            if parsed.path in ("/api/backups/preview", "/api/backups/restore-preview"):
                name = _name(parse_qs(parsed.query).get("name", [""])[0])
                if not name.endswith(".srv.zst") or Path(name).name != name:
                    raise BackupError("Invalid backup name")
                path = runtime.backups / name
                if parsed.path == "/api/backups/restore-preview":
                    return self._json({
                        "preview": restore_preview(path),
                        "restore": restore_conflicts(path, runtime.root, p),
                    })
                return self._json(restore_preview(path))
            if parsed.path == "/api/audit":
                q = parse_qs(parsed.query)
                return self._json(audit.read(int(q.get("limit", [100])[0]), event=q.get("event", [None])[0], name=q.get("name", [None])[0]))
            if parsed.path == "/api/containers/diagnostics":
                q = parse_qs(parsed.query)
                name = _name(q.get("name", [""])[0])
                tail = int(q.get("tail", [200])[0])
                return self._json(collect_container_diagnostics(p, name, tail))
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
            if parsed.path in {"/api/images/remove", "/api/volumes/remove", "/api/networks/remove"}:
                length = int(self.headers.get("Content-Length", "0"))
                raw = json.loads(self.rfile.read(length)) if length else {}
                name = _name(raw.get("name", "") if isinstance(raw, dict) else "")
                force = bool(raw.get("force", False)) if isinstance(raw, dict) else False
                if parsed.path == "/api/images/remove": result = p.remove_image(name, force=force)
                elif parsed.path == "/api/volumes/remove": result = p.remove_volume(name, force=force)
                else: result = p.remove_network(name)
                return self._json({"name": name, "result": result})

            if parsed.path in {"/api/backups/export", "/api/backups/restore", "/api/apps/install", "/api/apps/update", "/api/marketplace/publish", "/api/ai/import", "/api/ai/plan", "/api/ai/create", "/api/ai/troubleshoot", "/api/ai/recover", "/api/recovery/evaluate", "/api/recovery/policy", "/api/reliability/repair"}:
                length = int(self.headers.get("Content-Length", "0"))
                if length <= 0 or length > 512 * 1024:
                    raise ValueError("Manifest body must be between 1 byte and 512 KiB")
                raw = json.loads(self.rfile.read(length))
                if parsed.path == "/api/backups/restore":
                    requested = raw.get("name") if isinstance(raw, dict) else None
                    approved = bool(raw.get("approved", False)) if isinstance(raw, dict) else False
                    mode = raw.get("mode", "safe") if isinstance(raw, dict) else "safe"
                    if requested is None:
                        raise BackupError("Backup name is required")
                    name = Path(str(requested)).name
                    if name != str(requested) or not name.endswith(".srv.zst"):
                        raise BackupError("Backup name must be a simple .srv.zst filename")
                    result = restore_backup(
                        runtime.backups / name,
                        runtime.root,
                        p,
                        approved=approved,
                        mode=mode,
                    )
                    audit.append(
                        "backup.restore",
                        "user",
                        action="restore",
                        summary="Servora backup restored",
                        details={"backup": name, "apps": result.get("apps", []), "mode": mode},
                    )
                    return self._json(result, 201)

                if parsed.path == "/api/backups/export":
                    include_volumes = bool(raw.get("include_volumes", True)) if isinstance(raw, dict) else True
                    requested = raw.get("name") if isinstance(raw, dict) else None
                    if requested is None:
                        requested = "servora-backup.srv.zst"
                    name = Path(str(requested)).name
                    if name != str(requested) or not name.endswith(".srv.zst"):
                        raise BackupError("Backup name must be a simple .srv.zst filename")
                    result = create_backup(runtime.root, runtime.backups / name, p, include_volumes=include_volumes)
                    audit.append("backup.export", "user", action="export", summary="Servora backup created", details={"size_bytes": result["size_bytes"], "sha256": result["sha256"], "volumes_included": include_volumes})
                    return self._json(result, 201)
                if parsed.path == "/api/ai/plan":
                    prompt = raw.get("prompt") if isinstance(raw, dict) else None
                    provider = provider_from_env()
                    result = create_container_plan(provider, prompt)
                    return self._json({"provider": provider.name, "plan": result})

                if parsed.path == "/api/ai/create":
                    prompt = raw.get("prompt") if isinstance(raw, dict) else None
                    approved = bool(raw.get("approved", False)) if isinstance(raw, dict) else False
                    provider = provider_from_env()
                    supplied_plan = raw.get("plan") if isinstance(raw, dict) else None
                    if supplied_plan is not None:
                        plan = supplied_plan
                        if not isinstance(plan, dict):
                            raise AIPlanError("plan must be an object")
                    else:
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
                    preflight = preflight_manifest(p, validate_app_manifest(manifest), runtime.root)
                    if not preflight["ok"]:
                        raise ReliabilityError(json.dumps(preflight))
                    result = execute_plan(p, manifest)
                    audit.append("ai.create.launch", "ai", name=manifest.get("name"), action="create_container", summary="AI-generated container plan launched", details={"provider": provider.name, "findings": response["findings"], "health": result.get("health")})
                    return self._json({"status": "launched", **response, "result": result}, 201)

                if parsed.path == "/api/reliability/repair":
                    action = raw.get("action") if isinstance(raw, dict) else None
                    name = _name(raw.get("name", "") if isinstance(raw, dict) else "")
                    approved = bool(raw.get("approved", False)) if isinstance(raw, dict) else False
                    result = repair_reliability(p, runtime.root, action, name=name, approved=approved)
                    if result.get("status") == "approval_required":
                        audit.append("reliability.repair", "user", name=name, action=action,
                                     status="approval_required", summary="Reliability repair requires explicit approval")
                        return self._json(result, 409)
                    audit.append("reliability.repair", "user", name=name, action=action,
                                 summary="Approved reliability repair executed")
                    return self._json(result)

                if parsed.path == "/api/recovery/policy":
                    controller = RecoveryController(p, runtime.root)
                    policy = raw.get("policy") if isinstance(raw, dict) else None
                    if not isinstance(policy, dict):
                        raise RecoveryPolicyError("policy must be an object")
                    saved = controller.store.save(policy)
                    audit.append("recovery.policy.update", "user", action="update_policy", summary="Recovery policy updated", details={"enabled": saved.get("enabled"), "rules": saved.get("rules")})
                    return self._json(saved)
                if parsed.path == "/api/recovery/evaluate":
                    name = _name(raw.get("name", "") if isinstance(raw, dict) else "")
                    approved = bool(raw.get("approved", False)) if isinstance(raw, dict) else False
                    controller = RecoveryController(p, runtime.root)
                    result = controller.evaluate(name, approved=approved)
                    if result.get("status") == "approval_required":
                        audit.append("recovery.approval_required", "policy", name=name, action=result.get("action"), status="approval_required", summary="Automatic recovery policy proposed an action")
                    elif result.get("status") == "recovered":
                        audit.append("recovery.execute", "policy", name=name, action=result.get("action", {}).get("action"), summary="Automatic recovery policy executed an action", details={"health": result.get("result", {}).get("health")})
                    return self._json(result)
                if parsed.path == "/api/ai/recover":
                    name = _name(raw.get("name", "") if isinstance(raw, dict) else "")
                    action = raw.get("action") if isinstance(raw, dict) else None
                    if not isinstance(action, dict):
                        raise RecoveryError("action must be an object")
                    if not bool(raw.get("approved", False)):
                        return self._json({"status": "approval_required", "name": name, "action": action}, 409)
                    result = execute_recovery(p, name, action)
                    audit.append("ai.recovery.execute", "user", name=name, action=action.get("action"), summary="Approved AI recovery action executed", reason=action.get("reason"), details={"health": result.get("health")})
                    return self._json({"status": "recovered", "result": result})

                if parsed.path == "/api/ai/troubleshoot":
                    name = _name(raw.get("name", "") if isinstance(raw, dict) else "")
                    tail = int(raw.get("tail", 200)) if isinstance(raw, dict) else 200
                    diagnostics = collect_container_diagnostics(p, name, tail)
                    provider = provider_from_env()
                    diagnosis = troubleshoot_container(provider, diagnostics)
                    audit.append("ai.diagnosis", "ai", name=name, summary=str(diagnosis.get("summary", "")), details={"provider": provider.name, "confidence": diagnosis.get("confidence"), "finding_count": len(diagnosis.get("findings", [])), "actions": diagnosis.get("actions", [])})
                    for suggested in diagnosis.get("actions", []):
                        audit.append("ai.recovery.suggestion", "ai", name=name, action=suggested.get("action"), status="suggested", reason=suggested.get("reason"))
                    return self._json({"provider": provider.name, "diagnostics": diagnostics, "diagnosis": diagnosis})

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
                    preflight = preflight_manifest(p, manifest, runtime.root)
                    if not preflight["ok"]:
                        raise ReliabilityError(json.dumps(preflight))
                    try:
                        result = install_app(p, raw, transaction_root=runtime.root)
                        app_store.save(manifest)
                    except Exception as exc:
                        audit.append("app.install", "user", status="failed", name=manifest.name,
                                     action="install", summary="Servora app install failed", reason=str(exc))
                        raise
                    audit.append("app.install", "user", name=manifest.name, action="install", summary="Servora app installed")
                    return self._json({"app": manifest_to_dict(manifest), "created": result}, 201)
                old = app_store.get(manifest.name)
                if old is None:
                    raise AppManifestError("App is not installed")
                try:
                    result = update_app(p, old, raw, store=app_store, transaction_root=runtime.root)
                except Exception as exc:
                    audit.append("app.update", "user", status="failed", name=manifest.name,
                                 action="update", summary="Servora app update failed", reason=str(exc))
                    raise
                audit.append("app.update", "user", name=manifest.name, action="update", summary="Servora app updated")
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
                audit.append("app.uninstall", "user", name=name, action="uninstall", summary="Servora app uninstalled")
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
            result = action(name)
            audit.append("container.action", "user", name=name, action=parsed.path.rsplit("/", 1)[-1], summary="Container action executed")
            return self._json({"name": name, "result": result})
        except (ValueError, BackupError, PodmanError, AppManifestError, MarketplaceError, AIImportError, AIPlanError, json.JSONDecodeError, TroubleshootingError, RecoveryError, RecoveryPolicyError, AuditError, ReliabilityError) as exc:
            self._json({"error": str(exc)}, 400)

    def log_message(self, *_args):
        pass


def run(host="127.0.0.1", port=8787):
    ThreadingHTTPServer((host, port), Handler).serve_forever()


if __name__ == "__main__":
    run()
