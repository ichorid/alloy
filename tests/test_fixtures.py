"""Regression tests for shared pytest fixtures (beads_template, beads_project).

These encode the session-scoped Beads template + per-test copytree contract.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from conftest import bd_create

from alloy.beads import CUSTOM_STATUSES, BeadsClient


def _read_project_id(beads_dir: Path) -> str:
    metadata = json.loads((beads_dir / "metadata.json").read_text(encoding="utf-8"))
    return metadata["project_id"]


def _custom_status_names() -> list[str]:
    return [entry.split(":")[0] for entry in CUSTOM_STATUSES.split(",")]


def test_beads_project_copy_shares_template_project_id(
    beads_project: Path,
    beads_template: Path,
) -> None:
    """beads_project/.beads/metadata.json project_id equals the session template's."""
    template_id = _read_project_id(beads_template)
    copy_id = _read_project_id(beads_project / ".beads")
    assert copy_id == template_id


def test_beads_project_supports_create_and_show(beads_project: Path) -> None:
    """bd_create and BeadsClient.show work against a copied .beads directory."""
    bead_id = bd_create(beads_project, "x")
    assert bead_id

    bead = BeadsClient(repo=beads_project).show(bead_id)
    assert bead.id == bead_id
    assert bead.title == "x"


def test_beads_project_copy_has_custom_statuses(beads_project: Path) -> None:
    """Custom statuses from ensure_statuses survive the copy of .beads.

    `bd config set status.custom` stores the value in the embedded Dolt
    database, not in config.yaml, so read it back through bd.
    """
    proc = subprocess.run(
        ["bd", "config", "get", "status.custom"],
        cwd=str(beads_project),
        check=True,
        capture_output=True,
        text=True,
    )
    configured = proc.stdout.strip()
    for status in _custom_status_names():
        assert status in configured, f"expected custom status {status!r} in {configured!r}"
