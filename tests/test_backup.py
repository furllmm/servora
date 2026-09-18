from pathlib import Path

from servora.backup import restore_preview


def test_backup_preview_rejects_missing_file(tmp_path: Path):
    import pytest
    with pytest.raises(Exception):
        restore_preview(tmp_path / "missing.srv.zst")
