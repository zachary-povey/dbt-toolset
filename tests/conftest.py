from pathlib import Path

import pytest

from dbt_toolset.artifact import Artifact
from dbt_toolset.compiler import compile_project
from dbt_toolset.config import CompileConfig

PROJECT = Path(__file__).parent / "fixtures" / "sample_project"


@pytest.fixture(scope="session")
def artifact() -> Artifact:
    return compile_project(PROJECT, CompileConfig.load(PROJECT / "toolset.yml"), profiles_dir=PROJECT)


@pytest.fixture(scope="session")
def orders(artifact):
    return artifact.tool("orders")
