from __future__ import annotations

from dataclasses import dataclass, asdict
import json
import re
from pathlib import Path
from typing import Any

from .apps import AppManifestError, manifest_to_dict, validate_app_manifest

_CATEGORIES = {"official", "community", "ai_imported"}
_VERIFICATION = {"verified", "community", "ai_imported", "risk_detected"}
_SLUG = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


class MarketplaceError(ValueError):
    pass


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

    def seed(self, entries: list[dict[str, Any]]) -> None:
        for entry in entries:
            if self.get(entry.get("name", "")) is None:
                self.save(entry)
