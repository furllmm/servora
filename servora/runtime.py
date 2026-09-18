from __future__ import annotations

import json
from pathlib import Path


class Runtime:
    """Filesystem layout for Servora.

    Portable mode keeps all Servora-owned state under ``root``. User mode
    defaults to ~/.servora. Podman is never silently migrated.
    """

    def __init__(self, root: str | Path | None = None, mode: str = "user"):
        if mode not in {"user", "portable"}:
            raise ValueError("mode must be 'user' or 'portable'")
        self.mode = mode
        self.root = Path(root) if root else Path.home() / ".servora"
        self.config = self.root / "config"
        self.metadata = self.root / "metadata"
        self.logs = self.root / "logs"
        self.podman = self.root / "podman"
        self.apps = self.root / "apps"
        self.backups = self.root / "backups"

    @property
    def config_file(self) -> Path:
        return self.config / "servora.json"

    def initialize(self) -> None:
        for path in (
            self.config, self.metadata, self.logs, self.podman,
            self.apps, self.backups,
        ):
            path.mkdir(parents=True, exist_ok=True)
        if not self.config_file.exists():
            self.config_file.write_text(
                json.dumps({"version": 1, "mode": self.mode}, indent=2) + "\n",
                encoding="utf-8",
            )

    def podman_environment(self) -> dict[str, str]:
        """Return environment overrides without changing system Podman state."""
        return {
            "CONTAINERS_STORAGE_CONF": str(self.podman / "storage.conf"),
            "CONTAINERS_GRAPHROOT": str(self.podman / "storage"),
            "CONTAINERS_RUNROOT": str(self.podman / "runroot"),
        }
