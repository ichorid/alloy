"""BeadsClient project-memory helpers (bd memories/remember/forget).

These use the fake ``bd`` stub so they stay deterministic and do not require
a real Beads install.
"""

from __future__ import annotations

import logging

import pytest

from alloy.beads import BeadsClient, BeadsError


@pytest.fixture
def memory_client(fake_bd):
    return BeadsClient(repo=fake_bd.repo, binary=str(fake_bd.binary))


def test_memories_returns_mapping_minus_schema_version(memory_client, fake_bd):
    fake_bd.configure(
        {"memories": {"schema_version": 1, "k1": "v1", "k2": "v2"}},
    )

    assert memory_client.memories() == {"k1": "v1", "k2": "v2"}


def test_memories_returns_empty_when_subcommand_missing(memory_client, fake_bd, caplog):
    fake_bd.configure({"memories_unsupported": True})

    with caplog.at_level(logging.WARNING, logger="alloy.beads"):
        assert memory_client.memories() == {}
        assert memory_client.memories() == {}

    memory_logs = [record for record in caplog.records if "memories" in record.message.lower()]
    assert len(memory_logs) == 1


def test_remember_invokes_bd_with_expected_args(memory_client, fake_bd):
    memory_client.remember("k", "v")

    assert fake_bd.calls == [
        {"command": "remember", "argv": ["remember", "v", "--key", "k"]},
    ]


def test_forget_invokes_bd_with_expected_args(memory_client, fake_bd):
    memory_client.forget("k")

    assert fake_bd.calls == [
        {"command": "forget", "argv": ["forget", "k"]},
    ]


def test_remember_raises_beads_error_on_failure(memory_client, fake_bd):
    fake_bd.configure({"remember_fail": True})

    with pytest.raises(BeadsError):
        memory_client.remember("k", "v")
