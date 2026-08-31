"""CodeAgent must honour ``sandbox_mode = "execd"``.

``eval_for_code_agent`` used to know only about e2b/docker, so an execd
deployment — where every other Python channel runs in the sandbox — silently
executed CodeAgent's generated code in the agent process instead. That is a
lost sandbox boundary, not a missing feature, so it is worth pinning: the
degradation is invisible at runtime and would come back unnoticed after a
refactor of the mode-selection block.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, Mock

import pytest

from cuga.backend.cuga_graph.state.agent_state import AgentState, VariablesManager
from cuga.backend.cuga_graph.nodes.cuga_lite.executors import CodeExecutor
from cuga.config import settings


CODE = 'print(json.dumps({"variable_name": "x", "description": "d", "value": 1}))'


@pytest.fixture
def mock_state() -> AgentState:
    state = Mock(spec=AgentState)
    state.variables_manager = VariablesManager()
    return state


@pytest.fixture
def no_local_execution(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the local fallback loud: reaching it is the bug under test."""

    async def _fail(*_args, **_kwargs):  # pragma: no cover - only runs on regression
        raise AssertionError("CodeAgent fell back to in-process execution")

    monkeypatch.setattr(CodeExecutor, "_execute_locally_for_code_agent", _fail)


@pytest.mark.asyncio
async def test_execd_mode_sends_code_agent_to_the_sandbox(
    mock_state: AgentState, monkeypatch: pytest.MonkeyPatch, no_local_execution: None
) -> None:
    monkeypatch.setattr(settings.advanced_features, "sandbox_mode", "execd")
    executor = Mock()
    executor.execute_for_code_agent = AsyncMock(return_value="remote output")
    monkeypatch.setattr(CodeExecutor, "_get_execd_executor", classmethod(lambda cls: executor))

    result, new_vars = await CodeExecutor.eval_for_code_agent(code=CODE, state=mock_state)

    assert result == "remote output"
    assert new_vars == {}
    executor.execute_for_code_agent.assert_awaited_once()


def _local_spy(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    """Record local execution instead of running it — these tests assert routing."""
    spy = AsyncMock(return_value=("local output", {}))
    monkeypatch.setattr(CodeExecutor, "_execute_locally_for_code_agent", spy)
    return spy


def _forbid_execd(monkeypatch: pytest.MonkeyPatch, why: str) -> None:
    def _unreachable(cls):  # pragma: no cover - only runs on regression
        raise AssertionError(why)

    monkeypatch.setattr(CodeExecutor, "_get_execd_executor", classmethod(_unreachable))


@pytest.mark.asyncio
async def test_explicit_mode_still_outranks_the_setting(
    mock_state: AgentState, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``mode='local'`` is how callers opt out; execd must not override it."""
    monkeypatch.setattr(settings.advanced_features, "sandbox_mode", "execd")
    _forbid_execd(monkeypatch, "explicit mode='local' was overridden by sandbox_mode")
    spy = _local_spy(monkeypatch)

    await CodeExecutor.eval_for_code_agent(code=CODE, state=mock_state, mode='local')

    spy.assert_awaited_once()


@pytest.mark.asyncio
async def test_other_modes_are_unaffected(mock_state: AgentState, monkeypatch: pytest.MonkeyPatch) -> None:
    """Non-execd deployments keep the previous local-fallback behaviour."""
    monkeypatch.setattr(settings.advanced_features, "sandbox_mode", "opensandbox")
    monkeypatch.setattr(settings.advanced_features, "e2b_sandbox", False)
    _forbid_execd(monkeypatch, "execd executor used outside execd mode")
    spy = _local_spy(monkeypatch)

    await CodeExecutor.eval_for_code_agent(code=CODE, state=mock_state)

    spy.assert_awaited_once()
