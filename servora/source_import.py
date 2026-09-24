from __future__ import annotations

import ipaddress
import json
import socket
import shlex
import re
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

from .ai_import import AIImportError, import_source


_MAX_BYTES = 4 * 1024 * 1024
_TIMEOUT = 15
_COMPOSE_NAMES = (
    "compose.yaml",
    "compose.yml",
    "docker-compose.yml",
    "docker-compose.yaml",
)


class SourceImportError(ValueError):
    pass


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_MAX_REDIRECTS = 5


def _open_validated(request: urllib.request.Request):
    """Open a URL while validating every redirect target before connecting."""
    opener = urllib.request.build_opener(_NoRedirectHandler)
    current = request
    for _ in range(_MAX_REDIRECTS + 1):
        try:
            return opener.open(current, timeout=_TIMEOUT)
        except urllib.error.HTTPError as exc:
            if exc.code not in {301, 302, 303, 307, 308}:
                raise
            location = exc.headers.get("Location")
            if not location:
                exc.close()
                raise SourceImportError("Import source redirect has no Location header") from exc
            next_url = urljoin(current.full_url, location)
            _validate_url(next_url)
            exc.close()
            current = urllib.request.Request(
                next_url,
                headers=dict(request.header_items()),
            )
    raise SourceImportError("Import source exceeded the redirect limit")


def _validate_url(url: Any) -> str:
    if not isinstance(url, str) or len(url) > 2048:
        raise SourceImportError("Import URL is invalid")
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise SourceImportError("Import URL must use HTTPS without embedded credentials")
    try:
        addresses = socket.getaddrinfo(parsed.hostname, 443, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise SourceImportError(f"Could not resolve import host: {exc}") from exc
    for address in {item[4][0] for item in addresses}:
        ip = ipaddress.ip_address(address)
        if not ip.is_global:
            raise SourceImportError("Import host resolves to a non-public address")
    return url


def _fetch(url: str, accept: str = "*/*") -> tuple[str, bytes, dict[str, str]]:
    url = _validate_url(url)
    request = urllib.request.Request(
        url,
        headers={"Accept": accept, "User-Agent": "Servora-Marketplace/1"},
    )
    try:
        with _open_validated(request) as response:
            final = response.geturl()
            _validate_url(final)
            length = response.headers.get("Content-Length")
            if length and int(length) > _MAX_BYTES:
                raise SourceImportError("Remote source is too large")
            data = response.read(_MAX_BYTES + 1)
            content_type = response.headers.get("Content-Type", "")
    except SourceImportError:
        raise
    except (urllib.error.URLError, TimeoutError, ValueError, OSError) as exc:
        raise SourceImportError(f"Could not fetch import source: {exc}") from exc
    if len(data) > _MAX_BYTES:
        raise SourceImportError("Remote source is too large")
    return final, data, {"content_type": content_type}


def _parse_document(data: bytes, url: str) -> dict[str, Any]:
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SourceImportError("Import source is not valid UTF-8 text") from exc

    suffix = Path(urlparse(url).path).suffix.lower()
    try_json = suffix == ".json" or "json" in url.lower().split("?")[0]
    if try_json:
        try:
            raw = json.loads(text)
        except json.JSONDecodeError as exc:
            raise SourceImportError("Import source is not valid JSON") from exc
    else:
        try:
            import yaml
        except ImportError as exc:
            raise SourceImportError("YAML import requires the PyYAML dependency") from exc
        try:
            raw = yaml.safe_load(text)
        except yaml.YAMLError as exc:
            raise SourceImportError(f"Import source is not valid YAML: {exc}") from exc

    if not isinstance(raw, dict):
        raise SourceImportError("Import source must contain an object")
    return raw


def _github_repo(url: str) -> tuple[str, str] | None:
    parsed = urlparse(url)
    if parsed.hostname not in {"github.com", "www.github.com"}:
        return None
    parts = [p for p in parsed.path.split("/") if p]
    if len(parts) == 2:
        return parts[0], parts[1]
    return None


def _github_file(url: str) -> tuple[str, str, str, str] | None:
    parsed = urlparse(url)
    if parsed.hostname not in {"github.com", "www.github.com"}:
        return None
    parts = [p for p in parsed.path.split("/") if p]
    if len(parts) >= 5 and parts[2] == "blob":
        owner, repo, _, branch = parts[:4]
        path = "/".join(parts[4:])
        if owner and repo and branch and path:
            return owner, repo, branch, path
    return None


def _github_raw(url: str) -> bool:
    return urlparse(url).hostname == "raw.githubusercontent.com"


def _github_api(repo: tuple[str, str]) -> dict[str, Any]:
    owner, name = repo
    api = f"https://api.github.com/repos/{owner}/{name}"
    _, data, _ = _fetch(api, "application/vnd.github+json")
    try:
        raw = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SourceImportError("GitHub repository metadata is invalid") from exc
    if not isinstance(raw, dict) or not raw.get("default_branch"):
        raise SourceImportError("GitHub repository metadata is incomplete")
    return raw


def _github_raw_file(owner: str, repo: str, branch: str, path: str) -> tuple[str, dict[str, Any]]:
    raw_url = f"https://raw.githubusercontent.com/{owner}/{repo}/{branch}/{path}"
    final, data, _ = _fetch(raw_url)
    return final, _parse_document(data, final)


def _manifest_from_document(document: dict[str, Any], source_label: str, source_url: str) -> dict[str, Any]:
    if "services" in document:
        source = {
            "type": "compose",
            "raw": document,
            "url": source_url,
            "name": document.get("name") or source_label,
        }
        try:
            result = import_source(source)
        except AIImportError as exc:
            raise SourceImportError(str(exc)) from exc
        return result.to_dict()

    if "manifest" in document and isinstance(document["manifest"], dict):
        return _manifest_from_document(document["manifest"], source_label, source_url)

    raise SourceImportError("URL does not contain a supported Compose document or Servora manifest")


def _docker_image_reference(url: str) -> str | None:
    parsed = urlparse(url)
    if parsed.hostname not in {"hub.docker.com", "www.docker.com"}:
        return None
    parts = [p for p in parsed.path.split("/") if p]
    if len(parts) == 3 and parts[0] == "r":
        return f"{parts[1]}/{parts[2]}:latest"
    if len(parts) == 2 and parts[0] == "_":
        return f"{parts[1]}:latest"
    return None


def _extract_docker_run(text: str) -> str | None:
    lines = text.splitlines()
    for index, line in enumerate(lines):
        if not re.match(r"^\s*(?:sudo\s+)?docker\s+run(?:\s|$)", line):
            continue
        command = line.strip()
        while command.endswith("\") and index + 1 < len(lines):
            index += 1
            command = command[:-1].rstrip() + " " + lines[index].strip()
        return command
    return None


def _split_docker_option(token: str) -> tuple[str, str | None]:
    if token.startswith("--") and "=" in token:
        option, value = token.split("=", 1)
        return option, value
    return token, None


def _readme_result(text: str, source_url: str, source_label: str) -> dict[str, Any]:
    command = _extract_docker_run(text)
    if not command:
        raise SourceImportError("README has no supported docker run installation command")
    try:
        tokens = shlex.split(command)
    except ValueError as exc:
        raise SourceImportError(f"Invalid docker run command: {exc}") from exc
    if len(tokens) < 3 or tokens[:2] != ["docker", "run"]:
        raise SourceImportError("Not a docker run command")

    image = None
    name = source_label
    ports = []
    volumes = []
    environment = {}
    networks = []
    command_args = []
    i = 2
    while i < len(tokens):
        raw_token = tokens[i]
        token, inline_value = _split_docker_option(raw_token)

        if token in {"-d", "--detach", "--rm", "--init"}:
            if inline_value is not None:
                raise SourceImportError(f"{token} does not accept a value")
            i += 1
            continue

        if token in {"--name", "-n", "-p", "--publish", "-v", "--volume", "-e", "--env", "--network"}:
            if inline_value is not None:
                value = inline_value
                consumed = 1
            else:
                if i + 1 >= len(tokens):
                    raise SourceImportError(f"{token} requires a value")
                value = tokens[i + 1]
                consumed = 2

            if token in {"--name", "-n"}:
                name = value
            elif token in {"-p", "--publish"}:
                bits = value.rsplit(":", 2)
                if len(bits) == 2:
                    host, container = bits
                elif len(bits) == 3:
                    host, container = bits[0], bits[1]
                    if not bits[2]:
                        raise SourceImportError(f"Unsupported port mapping: {value}")
                else:
                    raise SourceImportError(f"Unsupported port mapping: {value}")
                proto = "tcp"
                if "/" in container:
                    container, proto = container.rsplit("/", 1)
                try:
                    host_port = int(host)
                    container_port = int(container)
                except ValueError as exc:
                    raise SourceImportError(f"Unsupported port mapping: {value}") from exc
                ports.append({"host": host_port, "container": container_port, "protocol": proto})
            elif token in {"-v", "--volume"}:
                bits = value.split(":")
                if len(bits) not in {2, 3}:
                    raise SourceImportError(f"Unsupported volume mapping: {value}")
                source, target = bits[:2]
                if source.startswith("/") or source.startswith("./") or source.startswith("../"):
                    raise SourceImportError("README uses a host bind mount; automatic import is refused")
                volumes.append({
                    "name": source,
                    "container_path": target,
                    "read_only": len(bits) == 3 and bits[2] == "ro",
                })
            elif token in {"-e", "--env"}:
                if "=" not in value:
                    raise SourceImportError("Environment assignment must contain =")
                key, val = value.split("=", 1)
                environment[key] = val
            elif token == "--network":
                networks.append(value)
            i += consumed
            continue

        if token.startswith("-"):
            if token in {
                "--privileged",
                "--pid",
                "--ipc",
                "--device",
                "--cap-add",
                "--security-opt",
                "--userns",
            }:
                raise SourceImportError(f"Unsupported privileged Docker option: {token}")
            raise SourceImportError(f"Unsupported Docker option: {token}")

        image = token
        command_args = tokens[i + 1 :]
        break

    if not image:
        raise SourceImportError("docker run command has no image")

    try:
        result = import_source({"type": "oci_image", "image": image, "name": name})
    except AIImportError as exc:
        raise SourceImportError(str(exc)) from exc

    manifest = result.manifest
    service = manifest["services"][0]
    service.update(
        {
            "name": "app",
            "ports": ports,
            "volumes": volumes,
            "environment": environment,
            "networks": networks,
            "command": command_args,
        }
    )
    manifest["metadata"]["source_type"] = "docker_run"
    manifest["metadata"]["source_url"] = source_url
    manifest["description"] = f"Imported docker run instructions from {source_label}"
    return {
        "manifest": manifest,
        "source": {"type": "docker_run", "url": source_url, "command": command},
        "findings": [f.to_dict() for f in result.findings],
        "requires_approval": result.requires_approval,
    }


def resolve_url(url: str) -> dict[str, Any]:
    url = _validate_url(url)

    docker = _docker_image_reference(url)
    if docker:
        try:
            result = import_source({"type": "oci_image", "image": docker, "url": url})
        except AIImportError as exc:
            raise SourceImportError(str(exc)) from exc
        manifest = result.manifest
        # Docker Hub repository metadata is optional; installation must not
        # depend on a second metadata request succeeding.
        source = {"type": "docker_hub", "url": url, "image": docker}
        try:
            parsed = urlparse(url)
            parts = [p for p in parsed.path.split("/") if p]
            if len(parts) == 3 and parts[0] == "r":
                namespace, repository = parts[1], parts[2]
            elif len(parts) == 2 and parts[0] == "_":
                namespace, repository = "library", parts[1]
            else:
                namespace = repository = ""
            if namespace and repository:
                metadata_url = f"https://hub.docker.com/v2/repositories/{namespace}/{repository}/"
                _, metadata_data, _ = _fetch(metadata_url, "application/json")
                metadata = json.loads(metadata_data.decode("utf-8"))
                if isinstance(metadata, dict):
                    description = metadata.get("description") or metadata.get("full_description")
                    if description:
                        manifest["description"] = str(description)[:1000]
                    manifest.setdefault("metadata", {})["source_digest"] = metadata.get("last_updated")
                    source["repository"] = f"{namespace}/{repository}"
        except (SourceImportError, UnicodeDecodeError, json.JSONDecodeError, OSError, ValueError):
            pass
        manifest.setdefault("metadata", {}).update({
            "source_type": "docker_hub",
            "docker_hub_url": url,
        })
        return {
            "manifest": manifest,
            "source": source,
            "findings": [f.to_dict() for f in result.findings],
            "requires_approval": result.requires_approval,
        }

    github_file = _github_file(url)
    if github_file:
        owner, repo, branch, path = github_file
        try:
            final, document = _github_raw_file(owner, repo, branch, path)
        except SourceImportError as exc:
            raise SourceImportError(f"GitHub file could not be imported: {exc}") from exc
        result = _manifest_from_document(document, Path(path).stem, final)
        result["source"] = {
            "type": "github_file",
            "url": url,
            "file_url": final,
            "repository": f"{owner}/{repo}",
            "branch": branch,
            "path": path,
        }
        return result

    repo_info = _github_repo(url)
    if repo_info:
        metadata = _github_api(repo_info)
        owner, name = repo_info
        branch = str(metadata["default_branch"])
        for filename in _COMPOSE_NAMES:
            try:
                raw_url, document = _github_raw_file(owner, name, branch, filename)
                result = _manifest_from_document(document, name, raw_url)
                result["source"] = {
                    "type": "github_compose",
                    "url": url,
                    "file_url": raw_url,
                    "repository": f"{owner}/{name}",
                    "branch": branch,
                }
                return result
            except SourceImportError:
                continue

        readme_url = f"https://raw.githubusercontent.com/{owner}/{name}/{branch}/README.md"
        try:
            final, data, _ = _fetch(readme_url)
            return _readme_result(data.decode("utf-8"), final, name)
        except (SourceImportError, UnicodeDecodeError):
            raise SourceImportError("GitHub repository has no supported Compose file or docker run instructions")

    if _github_raw(url):
        final, data, _ = _fetch(url)
        document = _parse_document(data, final)
        result = _manifest_from_document(document, Path(urlparse(final).path).stem, final)
        result["source"] = {"type": "github_raw", "url": final}
        return result

    final, data, headers = _fetch(url)
    try:
        document = _parse_document(data, final)
    except SourceImportError as document_error:
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            raise document_error
        try:
            return _readme_result(
                text,
                final,
                Path(urlparse(final).path).stem or "imported-app",
            )
        except SourceImportError:
            raise document_error
    result = _manifest_from_document(document, Path(urlparse(final).path).stem or "imported-app", final)
    result["source"] = {
        "type": "remote_compose",
        "url": final,
        "content_type": headers["content_type"],
    }
    return result
