from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_generate_sdks_accepts_clean_npm_workspace_hoisting(tmp_path: Path) -> None:
    script_dir = tmp_path / "scripts"
    spec_dir = tmp_path / "docs" / "api"
    package_dir = tmp_path / "packages" / "api-client"
    root_bin = tmp_path / "node_modules" / ".bin"
    script_dir.mkdir()
    spec_dir.mkdir(parents=True)
    package_dir.mkdir(parents=True)
    root_bin.mkdir(parents=True)
    shutil.copy(REPO_ROOT / "scripts" / "generate_sdks.sh", script_dir)
    (spec_dir / "apex-v1.openapi.json").write_text("{}")

    generator = root_bin / "openapi-typescript"
    generator.write_text("#!/bin/sh\nset -eu\ntest \"$2\" = '-o'\nprintf 'generated\\n' > \"$3\"\n")
    generator.chmod(0o755)

    completed = subprocess.run(
        ["bash", str(script_dir / "generate_sdks.sh")],
        cwd=tmp_path,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
    assert (package_dir / "src" / "schema.d.ts").read_text() == "generated\n"
