"""Register xdist CLI options when the plugin is blocked for serial runs."""

from __future__ import annotations

import sys

import pytest


def _argv_blocks_xdist() -> bool:
    argv = sys.argv
    for index, arg in enumerate(argv):
        if arg == "-p" and index + 1 < len(argv) and argv[index + 1] == "no:xdist":
            return True
    return False


if _argv_blocks_xdist():

    def pytest_addoption(parser: pytest.Parser, pluginmanager: pytest.PytestPluginManager) -> None:
        """Keep ``pytest -p no:xdist`` working alongside the ``-n auto`` in addopts.

        Blocking the plugin also drops the ``-n`` option it defines, so the ini
        addopts would be rejected as unrecognized. Let xdist register its options
        anyway through its public hook; with the plugin blocked nothing reads
        them, so the run is serial exactly as requested.
        """
        if pluginmanager.hasplugin("xdist"):
            return
        try:
            from xdist.plugin import pytest_addoption as register_xdist_options
        except ImportError:  # xdist not installed: let pytest report the bad addopts
            return
        register_xdist_options(parser)
