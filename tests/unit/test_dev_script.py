from pathlib import Path

import pytest
from scripts import dev


def test_ensure_env_creates_private_file(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    example = tmp_path / ".env.example"
    target = tmp_path / ".env"
    example.write_text("SECRET=value\n", encoding="utf-8")
    monkeypatch.setattr(dev, "ENV_EXAMPLE", example)
    monkeypatch.setattr(dev, "ENV_FILE", target)

    dev.ensure_env([])

    assert target.read_text(encoding="utf-8") == "SECRET=value\n"
    assert target.stat().st_mode & 0o777 == 0o600


def test_ensure_env_does_not_follow_existing_symlink(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    example = tmp_path / ".env.example"
    target = tmp_path / ".env"
    canary = tmp_path / "canary"
    example.write_text("replacement\n", encoding="utf-8")
    canary.write_text("preserved\n", encoding="utf-8")
    target.symlink_to(canary)
    monkeypatch.setattr(dev, "ENV_EXAMPLE", example)
    monkeypatch.setattr(dev, "ENV_FILE", target)

    dev.ensure_env([])

    assert canary.read_text(encoding="utf-8") == "preserved\n"
