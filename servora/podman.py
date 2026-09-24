from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any


class PodmanError(RuntimeError):
    pass


class Podman:
    def __init__(self, executable: str | None = None, env: dict[str, str] | None = None):
        self.executable = executable or shutil.which("podman")
        if not self.executable:
            raise PodmanError("Podman was not found in PATH")
        self.env = dict(os.environ)
        self.env.update(env or {})

    @staticmethod
    def detect() -> str | None:
        return shutil.which("podman")

    @staticmethod
    def install_help() -> dict[str, str]:
        return {
            "platform": "debian-ubuntu",
            "command": "sudo apt update && sudo apt install -y podman",
            "note": "Servora does not install Podman automatically.",
        }

    def _run(self, *args: object, check: bool = True) -> subprocess.CompletedProcess[str]:
        argv = [self.executable, *(str(a) for a in args if a is not None and a != "")]
        try:
            result = subprocess.run(argv, env=self.env, text=True, capture_output=True, check=False)
        except OSError as exc:
            raise PodmanError(str(exc)) from exc
        if check and result.returncode != 0:
            message = (result.stderr or result.stdout).strip()
            raise PodmanError(message or f"podman exited with {result.returncode}")
        return result

    def version(self) -> str:
        return self._run("version", "--format", "{{.Client.Version}}").stdout.strip()

    def _json_lines(self, *args: object) -> list[dict[str, Any]]:
        result = self._run(*args)
        if not result.stdout.strip():
            return []
        try:
            parsed = json.loads(result.stdout)
            if isinstance(parsed, list):
                return parsed
        except json.JSONDecodeError:
            pass
        values: list[dict[str, Any]] = []
        for line in result.stdout.splitlines():
            if line.strip():
                try:
                    values.append(json.loads(line))
                except json.JSONDecodeError as exc:
                    raise PodmanError("Podman returned invalid JSON") from exc
        return values

    def list_containers(self, all: bool = True) -> list[dict[str, Any]]:
        return self._json_lines("ps", "-a" if all else None, "--format", "json")

    def list_images(self) -> list[dict[str, Any]]:
        return self._json_lines("images", "--format", "json")

    def list_volumes(self) -> list[dict[str, Any]]:
        return self._json_lines("volume", "ls", "--format", "json")

    def list_networks(self) -> list[dict[str, Any]]:
        return self._json_lines("network", "ls", "--format", "json")

    def image_exists(self, name: str) -> bool:
        """Return whether Podman has a local image matching the given reference."""
        if not isinstance(name, str) or not name.strip():
            raise ValueError("image name is required")
        return self._run("image", "exists", name, check=False).returncode == 0

    def pull_image(self, name: str) -> str:
        """Pull an image reference into the local Podman image store."""
        if not isinstance(name, str) or not name.strip():
            raise ValueError("image name is required")
        return self._run("pull", name).stdout.strip()

    def inspect_image(self, name: str) -> dict[str, Any]:
        result = self._run("image", "inspect", name)
        try:
            values = json.loads(result.stdout)
            return values[0]
        except (json.JSONDecodeError, IndexError, TypeError) as exc:
            raise PodmanError("Podman returned invalid image inspect data") from exc

    def image_metadata(self, name: str) -> dict[str, Any]:
        """Return stable, UI/API-safe metadata for a local image."""
        data = self.inspect_image(name)
        repo_tags = data.get("RepoTags") or data.get("Names") or []
        repo_digests = data.get("RepoDigests") or []
        if isinstance(repo_tags, str):
            repo_tags = [repo_tags]
        if isinstance(repo_digests, str):
            repo_digests = [repo_digests]
        image_id = data.get("Id") or data.get("ID") or data.get("Image")
        digest = data.get("Digest")
        if not digest and repo_digests:
            digest = str(repo_digests[0]).split("@", 1)[1] if "@" in str(repo_digests[0]) else None
        return {
            "name": name,
            "id": image_id,
            "digest": digest,
            "repo_tags": [str(x) for x in repo_tags],
            "repo_digests": [str(x) for x in repo_digests],
            "created": data.get("Created"),
            "size": data.get("Size"),
        }

    @staticmethod
    def _image_identity_changed(before: dict[str, Any] | None, after: dict[str, Any]) -> bool:
        """Compare image identity with digest taking precedence over local ID."""
        if not before:
            return False
        before_digest = before.get("digest")
        after_digest = after.get("digest")
        if before_digest and after_digest:
            return before_digest != after_digest
        before_id = before.get("id")
        after_id = after.get("id")
        if before_id and after_id:
            return before_id != after_id
        return False

    def refresh_image(self, name: str) -> dict[str, Any]:
        """Pull a tag/reference and report whether its resolved local image changed."""
        before = None
        try:
            before = self.image_metadata(name)
        except PodmanError:
            pass
        result = self.pull_image(name)
        after = self.image_metadata(name)
        changed = self._image_identity_changed(before, after)
        return {
            "name": name,
            "status": "updated" if changed else ("unchanged" if before else "pulled"),
            "changed": changed,
            "before": before,
            "after": after,
            "result": result,
        }

    def inspect_network(self, name: str) -> dict[str, Any]:
        result = self._run("network", "inspect", name)
        try:
            values = json.loads(result.stdout)
            return values[0]
        except (json.JSONDecodeError, IndexError, TypeError) as exc:
            raise PodmanError("Podman returned invalid network inspect data") from exc

    def inspect_volume(self, name: str) -> dict[str, Any]:
        result = self._run("volume", "inspect", name)
        try:
            values = json.loads(result.stdout)
            return values[0]
        except (json.JSONDecodeError, IndexError, TypeError) as exc:
            raise PodmanError("Podman returned invalid volume inspect data") from exc

    def inspect_container(self, name: str) -> dict[str, Any]:
        result = self._run("inspect", name)
        try:
            values = json.loads(result.stdout)
            return values[0]
        except (json.JSONDecodeError, IndexError, TypeError) as exc:
            raise PodmanError("Podman returned invalid inspect data") from exc

    def start_container(self, name: str) -> str:
        return self._run("start", name).stdout.strip()

    def container_health(self, name: str) -> dict[str, Any]:
        """Return a normalized health snapshot from Podman inspect data."""
        data = self.inspect_container(name)
        state = data.get("State") or {}
        status = str(state.get("Status", "unknown")).lower()
        health = state.get("Health") or {}
        health_status = str(health.get("Status", "")).lower() or None
        if health_status == "healthy":
            result = "healthy"
        elif health_status in {"unhealthy", "starting"}:
            result = health_status
        elif status == "running":
            result = "running"
        elif status in {"created", "paused", "exited", "stopped", "dead"}:
            result = status
        else:
            result = "unknown"
        return {
            "name": name,
            "status": result,
            "running": status == "running",
            "healthcheck": health_status,
        }

    def stop_container(self, name: str) -> str:
        return self._run("stop", name).stdout.strip()

    def restart_container(self, name: str) -> str:
        return self._run("restart", name).stdout.strip()

    def remove_container(self, name: str, force: bool = False) -> str:
        return self._run("rm", "--force" if force else None, name).stdout.strip()

    def container_logs(self, name: str, tail: int = 200) -> str:
        if not isinstance(tail, int) or tail < 0 or tail > 10000:
            raise ValueError("tail must be between 0 and 10000")
        return self._run("logs", "--tail", tail, name).stdout

    def stats(self, name: str | None = None) -> list[dict[str, Any]]:
        args = ["stats", "--no-stream", "--format", "json"]
        if name:
            args.append(name)
        return self._json_lines(*args)

    def create_container(self, plan: dict[str, Any]) -> str:
        name = str(plan.get("name", ""))
        image = str(plan.get("image", ""))
        if not name or not image:
            raise ValueError("Container name and image are required")
        args: list[object] = ["create", "--name", name]
        for port in plan.get("ports", []):
            proto = port.get("protocol", "tcp")
            args += ["-p", f"{int(port['host'])}:{int(port['container'])}/{proto}"]
        for volume in plan.get("volumes", []):
            vname = volume["name"]
            path = volume["container_path"]
            suffix = ":ro" if volume.get("read_only") else ":rw"
            args += ["-v", f"{vname}:{path}{suffix}"]
        for network in plan.get("networks", []):
            args += ["--network", network]
        for key, value in plan.get("environment", {}).items():
            args += ["-e", f"{key}={value}"]
        command = plan.get("command", [])
        args.append(image)
        args.extend(command)
        return self._run(*args).stdout.strip()

    def create_volume(self, name: str) -> str:
        return self._run("volume", "create", name).stdout.strip()

    def remove_volume(self, name: str, force: bool = False) -> str:
        return self._run("volume", "rm", "--force" if force else None, name).stdout.strip()

    def remove_network(self, name: str) -> str:
        return self._run("network", "rm", name).stdout.strip()

    def remove_image(self, name: str, force: bool = False) -> str:
        return self._run("image", "rm", "--force" if force else None, name).stdout.strip()

    def create_network(self, name: str) -> str:
        return self._run("network", "create", name).stdout.strip()
