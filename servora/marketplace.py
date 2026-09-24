from __future__ import annotations

from dataclasses import dataclass, asdict
import json
import re
import ipaddress
import socket
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .apps import AppManifestError, manifest_to_dict, validate_app_manifest
from .source_import import SourceImportError, _fetch as _fetch_source, resolve_url

_CATEGORIES = {"official", "community", "ai_imported"}
_VERIFICATION = {"verified", "community", "ai_imported", "risk_detected"}
_SLUG = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_MAX_REMOTE_BYTES = 1024 * 1024
_REMOTE_TIMEOUT = 15


class MarketplaceError(ValueError):
    pass


def _validate_remote_url(url: Any) -> str:
    if not isinstance(url, str) or len(url) > 2048:
        raise MarketplaceError("Remote marketplace URL is invalid")
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise MarketplaceError("Remote marketplace URL must use HTTPS without embedded credentials")
    try:
        addresses = socket.getaddrinfo(parsed.hostname, 443, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise MarketplaceError(f"Could not resolve marketplace host: {exc}") from exc
    for address in {item[4][0] for item in addresses}:
        try:
            ip = ipaddress.ip_address(address)
        except ValueError as exc:
            raise MarketplaceError("Marketplace host resolved to an invalid address") from exc
        if not ip.is_global:
            raise MarketplaceError("Marketplace host resolves to a non-public address")
    return url


def import_url(url: str) -> MarketplaceEntry:
    """Resolve a supported installation URL into a validated marketplace entry."""
    try:
        resolved = resolve_url(url)
    except SourceImportError as exc:
        # Keep the legacy marketplace JSON format as a compatibility path.
        try:
            raw = _fetch_remote_json(url)
        except MarketplaceError:
            raise MarketplaceError(str(exc)) from exc
        items = raw.get("entries") if isinstance(raw.get("entries"), list) else [raw]
        if len(items) != 1:
            raise MarketplaceError("URL is not a supported installation source or single marketplace entry")
        entry = validate_entry(items[0])
        source = dict(entry.source)
        source.setdefault("url", url)
        source.setdefault("imported_by", "servora")
        return MarketplaceEntry(
            entry.name,
            entry.version,
            entry.description,
            entry.category,
            entry.verification,
            entry.manifest,
            source,
            entry.tags,
        )

    manifest = resolved["manifest"]
    findings = resolved.get("findings", [])
    verification = "risk_detected" if findings else "community"
    source = dict(resolved.get("source", {}))
    source.setdefault("url", url)
    source["imported_by"] = "servora"
    if findings:
        source["findings"] = findings
    tags = ["imported", source.get("type", "remote")]
    if source.get("type") == "docker_hub":
        tags.append("docker-hub")
    return validate_entry({
        "name": manifest["name"],
        "category": "community",
        "verification": verification,
        "manifest": manifest,
        "source": source,
        "tags": tags,
    })

def _fetch_remote_json(url: str) -> dict[str, Any]:
    """Fetch the legacy marketplace JSON format using the hardened source fetcher."""
    try:
        final_url, data, _ = _fetch_source(url, "application/json")
    except SourceImportError as exc:
        raise MarketplaceError(str(exc)) from exc
    if final_url != url and not final_url.startswith("https://"):
        raise MarketplaceError("Remote marketplace URL resolved to an invalid scheme")
    if len(data) > _MAX_REMOTE_BYTES:
        raise MarketplaceError("Remote marketplace document is too large")
    try:
        raw = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise MarketplaceError("Remote marketplace document is not valid UTF-8 JSON") from exc
    if not isinstance(raw, dict):
        raise MarketplaceError("Remote marketplace document must be a JSON object")
    return raw

@dataclass(frozen=True)
class MarketplaceEntry:
    name: str
    version: str
    description: str
    category: str
    verification: str
    manifest: dict[str, Any]
    source: dict[str, Any]
    tags: list[str]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _slug(value: Any) -> str:
    if not isinstance(value, str) or not _SLUG.fullmatch(value):
        raise MarketplaceError("Invalid marketplace app name")
    return value


def validate_entry(raw: dict[str, Any]) -> MarketplaceEntry:
    if not isinstance(raw, dict):
        raise MarketplaceError("Marketplace entry must be an object")
    manifest = validate_app_manifest(raw.get("manifest"))
    if _slug(raw.get("name", manifest.name)) != manifest.name:
        raise MarketplaceError("Marketplace name must match manifest name")
    category = raw.get("category", "community")
    verification = raw.get("verification", "community")
    if category not in _CATEGORIES:
        raise MarketplaceError("Invalid marketplace category")
    if verification not in _VERIFICATION:
        raise MarketplaceError("Invalid verification status")
    source = raw.get("source", {})
    if not isinstance(source, dict):
        raise MarketplaceError("source must be an object")
    tags = raw.get("tags", [])
    if not isinstance(tags, list) or not all(isinstance(x, str) and x.strip() for x in tags):
        raise MarketplaceError("tags must be a list of non-empty strings")
    return MarketplaceEntry(
        name=manifest.name,
        version=manifest.version,
        description=manifest.description,
        category=category,
        verification=verification,
        manifest=manifest_to_dict(manifest),
        source=source,
        tags=tags,
    )


class MarketplaceStore:
    """Local marketplace registry; network synchronization can be added later."""
    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.path = self.root / "marketplace"
        self.path.mkdir(parents=True, exist_ok=True)

    def _file(self, name: str) -> Path:
        return self.path / f"{_slug(name)}.json"

    def save(self, raw: dict[str, Any] | MarketplaceEntry) -> MarketplaceEntry:
        entry = raw if isinstance(raw, MarketplaceEntry) else validate_entry(raw)
        target = self._file(entry.name)
        tmp = target.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(entry.to_dict(), indent=2, sort_keys=True), encoding="utf-8")
        tmp.replace(target)
        return entry

    def get(self, name: str) -> MarketplaceEntry | None:
        target = self._file(name)
        if not target.exists():
            return None
        try:
            return validate_entry(json.loads(target.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError, MarketplaceError, AppManifestError):
            return None

    def list(self, *, category: str | None = None, query: str | None = None) -> list[MarketplaceEntry]:
        entries: list[MarketplaceEntry] = []
        for item in sorted(self.path.glob("*.json")):
            try:
                entry = validate_entry(json.loads(item.read_text(encoding="utf-8")))
            except (OSError, json.JSONDecodeError, MarketplaceError, AppManifestError):
                continue
            if category and entry.category != category:
                continue
            if query:
                haystack = " ".join([entry.name, entry.description, *entry.tags]).lower()
                if query.lower() not in haystack:
                    continue
            entries.append(entry)
        return entries

    def remove(self, name: str) -> None:
        self._file(name).unlink(missing_ok=True)

    def publish(self, raw: dict[str, Any]) -> MarketplaceEntry:
        entry = validate_entry(raw)
        if entry.category == "official":
            raise MarketplaceError("Official entries cannot be published through the local API")
        return self.save(entry)

    def fork(self, name: str, new_name: str) -> MarketplaceEntry:
        source = self.get(name)
        if source is None:
            raise MarketplaceError("Marketplace app not found")
        new_name = _slug(new_name)
        if self.get(new_name) is not None:
            raise MarketplaceError("Marketplace app already exists")
        manifest = dict(source.manifest)
        manifest["name"] = new_name
        source_meta = dict(source.source)
        source_meta["forked_from"] = source.name
        source_meta["forked_from_version"] = source.version
        return self.save({
            "name": new_name,
            "category": "community",
            "verification": "community",
            "description": source.description,
            "manifest": manifest,
            "source": source_meta,
            "tags": list(source.tags) + ["fork"],
        })

    def download(self, name: str) -> dict[str, Any]:
        entry = self.get(name)
        if entry is None:
            raise MarketplaceError("Marketplace app not found")
        return {"format": "servora-marketplace-v1", "entry": entry.to_dict()}

    def import_url(self, url: str) -> MarketplaceEntry:
        entry = import_url(url)
        source = dict(entry.source)
        source.setdefault("remote_url", url)
        return self.save({**entry.to_dict(), "source": source})

    def import_urls(self, urls: list[str]) -> dict[str, Any]:
        if not isinstance(urls, list) or not urls:
            raise MarketplaceError("At least one import URL is required")
        if len(urls) > 20:
            raise MarketplaceError("A maximum of 20 URLs can be imported at once")
        imported: list[MarketplaceEntry] = []
        errors: list[dict[str, str]] = []
        seen: set[str] = set()
        for raw_url in urls:
            if not isinstance(raw_url, str):
                errors.append({"url": str(raw_url), "error": "URL must be a string"})
                continue
            url = raw_url.strip()
            if not url or url in seen:
                continue
            seen.add(url)
            try:
                imported.append(self.import_url(url))
            except MarketplaceError as exc:
                errors.append({"url": url, "error": str(exc)})
        if not imported and errors:
            raise MarketplaceError(json.dumps({"imported": [], "errors": errors}))
        return {"imported": imported, "errors": errors}

    def import_remote(self, url: str) -> list[MarketplaceEntry]:
        """Import one or more validated entries from a public HTTPS marketplace document."""
        raw = _fetch_remote_json(url)
        items = raw.get("entries") if isinstance(raw.get("entries"), list) else [raw]
        if not items:
            raise MarketplaceError("Remote marketplace document contains no entries")
        imported = []
        for item in items:
            entry = validate_entry(item)
            source = dict(entry.source)
            source.setdefault("remote_url", url)
            imported.append(self.save({**entry.to_dict(), "source": source}))
        return imported

    def seed(self, entries: list[dict[str, Any]]) -> None:
        for entry in entries:
            if self.get(entry.get("name", "")) is None:
                self.save(entry)
