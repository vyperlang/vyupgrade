from __future__ import annotations

from pathlib import Path

import pytest

from vyupgrade.models import Config


@pytest.fixture
def config():
    def make_config(**kwargs) -> Config:
        values = {"paths": (Path("contracts"),)}
        values.update(kwargs)
        return Config(**values)

    return make_config
