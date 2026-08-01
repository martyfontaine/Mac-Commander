"""Hermetic tests must stay hermetic: never spawn the halo helper."""

import pytest

import server


@pytest.fixture(autouse=True)
def no_overlay(monkeypatch):
    monkeypatch.setitem(server.CONFIG, "overlay", False)
