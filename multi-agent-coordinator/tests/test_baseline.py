from importlib.metadata import PackageNotFoundError, distribution
from importlib.util import find_spec

import pytest

from misaka_coordinator_service.baseline import verify_baseline


def test_selective_agent_framework_baseline() -> None:
    report = verify_baseline()

    assert report.agent_framework_core.startswith("1.15.")
    assert report.agent_framework_openai.startswith("1.14.")
    assert report.agent_framework_orchestrations.startswith("1.1.")
    assert report.session_round_trip
    assert report.openai_compatible_client
    assert report.mcp_stdio_tool
    assert report.mcp_streamable_http_tool
    assert report.workflow_builder


def test_full_agent_framework_meta_package_is_not_installed() -> None:
    with pytest.raises(PackageNotFoundError):
        distribution("agent-framework")


def test_v3_internal_packages_are_not_visible() -> None:
    assert find_spec("misaka_control_plane") is None
    assert find_spec("misaka_invocation_runtime") is None
