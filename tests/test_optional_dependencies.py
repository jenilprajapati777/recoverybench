"""Regression tests for optional dependency isolation and on-demand loading (Phase 4.4 & 4.6).

Verifies Section 6 of Phase 4 directive:
- Importing the base package does not eagerly import CrewAI, AutoGen, or LangGraph.
- Framework adapters resolve on-demand via PEP 562 __getattr__.
- Missing optional dependencies produce clear, actionable ConfigurationErrors only when requested.
- Optional dependencies remain genuinely optional.
"""

import subprocess
import sys

import pytest

from recoverybench.errors import ConfigurationError


def test_import_recoverybench_does_not_eagerly_load_frameworks() -> None:
    """Verify that importing recoverybench in a clean process does not load external frameworks."""
    cmd = (
        "import recoverybench; import sys; "
        "forbidden = ['crewai', 'autogen', 'ag2', 'langgraph', 'langchain']; "
        "loaded = [pkg for pkg in forbidden if pkg in sys.modules]; "
        "assert loaded == [], f'Loaded: {loaded}'; "
        "print('CLEAN')"
    )
    result = subprocess.run([sys.executable, "-c", cmd], capture_output=True, text=True)
    assert result.returncode == 0, f"Import polluted sys.modules: {result.stderr or result.stdout}"
    assert "CLEAN" in result.stdout


def test_lazy_exports_resolve_via_top_level_getattr() -> None:
    """Verify that accessing adapters on recoverybench dynamically imports them."""
    import recoverybench as rb

    crew_cls = rb.CrewAIAdapter
    assert crew_cls.__name__ == "CrewAIAdapter"

    autogen_cls = rb.AutoGenAdapter
    assert autogen_cls.__name__ == "AutoGenAdapter"

    langgraph_cls = rb.LangGraphAdapter
    assert langgraph_cls.__name__ == "LangGraphAdapter"


def test_lazy_exports_resolve_via_adapters_subpackage() -> None:
    """Verify that accessing adapters on recoverybench.adapters dynamically imports them."""
    import recoverybench.adapters as adapters

    crew_cls = adapters.CrewAIAdapter
    assert crew_cls.__name__ == "CrewAIAdapter"

    autogen_cls = adapters.AutoGenAdapter
    assert autogen_cls.__name__ == "AutoGenAdapter"

    langgraph_cls = adapters.LangGraphAdapter
    assert langgraph_cls.__name__ == "LangGraphAdapter"


def test_crewai_adapter_missing_dependency_actionable_error() -> None:
    """Verify that requesting CrewAI when uninstalled produces an actionable ConfigurationError."""
    from recoverybench.adapters.crewai import CREWAI_AVAILABLE, CrewAIAdapter

    if not CREWAI_AVAILABLE:
        with pytest.raises(ConfigurationError) as exc_info:
            CrewAIAdapter(crew_factory=lambda t: None, require_crewai=True)
        err_msg = str(exc_info.value)
        assert "CrewAI is not installed" in err_msg
        assert "pip install recoverybench[crewai]" in err_msg


def test_autogen_adapter_missing_dependency_actionable_error() -> None:
    """Verify that requesting AutoGen when uninstalled produces an actionable ConfigurationError."""
    from recoverybench.adapters.autogen import AUTOGEN_AVAILABLE, AutoGenAdapter

    if not AUTOGEN_AVAILABLE:
        with pytest.raises(ConfigurationError) as exc_info:
            AutoGenAdapter(agent_factory=lambda t: None, require_autogen=True)
        err_msg = str(exc_info.value)
        assert "AutoGen is not installed" in err_msg
        assert "pip install recoverybench[autogen]" in err_msg


def test_mcp_package_has_no_external_dependencies() -> None:
    """Verify that the internal MCP server bridge requires no external MCP SDK packages."""
    import recoverybench.mcp as mcp

    assert hasattr(mcp, "RecoveryBenchMCPServer")
    assert hasattr(mcp, "MCPServerConfig")
    assert hasattr(mcp, "serve_mcp")
    assert "mcp" not in sys.modules or sys.modules["mcp"].__name__ == "recoverybench.mcp"
