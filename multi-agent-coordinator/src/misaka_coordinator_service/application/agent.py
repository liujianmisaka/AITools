from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, cast
from urllib.parse import urlparse

from agent_framework import Agent, AgentResponse, AgentSession, FunctionTool
from agent_framework.exceptions import AgentFrameworkException
from agent_framework.openai import OpenAIChatClient, OpenAIChatOptions

from misaka_coordinator_service.application.decision import (
    COORDINATOR_DECISION_RESPONSE_FORMAT,
    CoordinatorDecision,
)
from misaka_coordinator_service.domain._serialization import ensure_text
from misaka_coordinator_service.domain.errors import CoordinatorDomainError

DEFAULT_COORDINATOR_INSTRUCTIONS = """You are the cognitive coordinator for Multi-Agent V3.
Decide exactly one next orchestration action for each activation step. Treat V3 as the only source
of execution truth. Never claim that a delegation ran unless the supplied facts prove it. Revise a
plan without changing tasks that already have execution references. Accept a result only after the
supplied facts satisfy its acceptance criteria, and complete a goal only after every node is
accepted. Keep the rationale concise and return only the configured structured response. Do not
reveal hidden chain of thought. Use request_input when a user decision is required and wait when an
external event is needed.

Follow this action contract exactly:
- When an active goal has no plan, use create_plan before delegate or dispatch_ready_nodes.
- create_plan and revise_plan carry the complete task list. Use parent_task_id only for a real
  dependency; independent tasks use null. Their selection is either null or one selection that is
  valid for every supplied task.
- delegate carries exactly one task and exactly one selection. To use different providers for
  independent tasks, create the full plan first and then issue one delegate decision per task in
  successive decision steps. Those delegations can still execute concurrently.
- dispatch_ready_nodes carries no tasks. It may carry one selection only when that same selection
  applies to every currently proposed ready task.
- wait, review, accept_result, respond, request_input, complete_goal, and stop carry no tasks and no
  selection. review and accept_result require target_node_id. respond and request_input require a
  message.
- send_message and cancel_delegation carry no tasks or selection and require target_node_id plus a
  message.
- Fields unused by an action must be [] for tasks and null for selection, target_node_id, or
  message.
- When current facts contain previous_decision_feedback, correct that invalid action in the next
  decision step. Do not convert a correctable dispatch contract error into wait or request_input.
"""

_SESSION_LEDGER_KEY = "misaka.coordinator.decision_ledger"


class CoordinatorReasoningEffort(StrEnum):
    NONE = "none"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    XHIGH = "xhigh"


class CoordinatorAgentError(RuntimeError):
    """Base error for Coordinator Agent execution."""


class CoordinatorDecisionStepError(CoordinatorAgentError):
    """Raised when an activation step is invalid or out of order."""


class CoordinatorDecisionLimitExceeded(CoordinatorDecisionStepError):
    """Raised when one activation exceeds its configured decision limit."""


class CoordinatorModelUnavailableError(CoordinatorAgentError):
    """Raised when the configured model endpoint cannot complete a request."""


class CoordinatorStructuredResponseError(CoordinatorAgentError):
    """Raised when a model response does not satisfy the decision contract."""


@dataclass(frozen=True, slots=True)
class CoordinatorAgentConfig:
    model: str
    api_key: str = field(repr=False)
    base_url: str | None = None
    reasoning_effort: CoordinatorReasoningEffort = CoordinatorReasoningEffort.MEDIUM
    max_output_tokens: int = 8_000
    max_decision_steps: int = 16
    max_structured_response_attempts: int = 2
    request_timeout_seconds: float = 120.0
    agent_id: str = "multi-agent-v3-coordinator"
    agent_name: str = "Multi-Agent V3 Coordinator"
    instructions: str = DEFAULT_COORDINATOR_INSTRUCTIONS

    def __post_init__(self) -> None:
        object.__setattr__(self, "model", ensure_text(self.model, "model"))
        object.__setattr__(self, "api_key", ensure_text(self.api_key, "api_key"))
        object.__setattr__(self, "agent_id", ensure_text(self.agent_id, "agent_id"))
        object.__setattr__(self, "agent_name", ensure_text(self.agent_name, "agent_name"))
        object.__setattr__(self, "instructions", ensure_text(self.instructions, "instructions"))
        if self.base_url is not None:
            normalized_url = ensure_text(self.base_url, "base_url").rstrip("/")
            parsed = urlparse(normalized_url)
            if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                raise CoordinatorAgentError("base_url must be an absolute HTTP(S) URL")
            object.__setattr__(self, "base_url", normalized_url)
        if self.max_output_tokens < 1:
            raise CoordinatorAgentError("max_output_tokens must be greater than zero")
        if self.max_decision_steps < 1:
            raise CoordinatorAgentError("max_decision_steps must be greater than zero")
        if not 1 <= self.max_structured_response_attempts <= 3:
            raise CoordinatorAgentError("max_structured_response_attempts must be between 1 and 3")
        if self.request_timeout_seconds <= 0:
            raise CoordinatorAgentError("request_timeout_seconds must be greater than zero")


@dataclass(frozen=True, slots=True)
class CoordinatorDecisionResult:
    decision: CoordinatorDecision
    response_id: str | None
    finish_reason: str | None


type DecisionInvoker = Callable[
    [str, AgentSession],
    Awaitable[AgentResponse[Any]],
]


class CoordinatorAgent:
    def __init__(
        self,
        *,
        config: CoordinatorAgentConfig,
        invoker: DecisionInvoker,
        framework_agent: Agent[OpenAIChatOptions] | None = None,
    ) -> None:
        self._config = config
        self._invoker = invoker
        self._framework_agent = framework_agent

    @classmethod
    def from_openai(
        cls,
        config: CoordinatorAgentConfig,
        *,
        tools: Sequence[FunctionTool] = (),
    ) -> CoordinatorAgent:
        client = OpenAIChatClient(
            model=config.model,
            api_key=config.api_key,
            base_url=config.base_url,
        )
        options: OpenAIChatOptions = {
            "max_tokens": config.max_output_tokens,
            "reasoning": {"effort": config.reasoning_effort.value},
            "response_format": cast(Mapping[str, Any], COORDINATOR_DECISION_RESPONSE_FORMAT),
            "store": False,
        }
        agent: Agent[OpenAIChatOptions] = Agent(
            client=client,
            id=config.agent_id,
            name=config.agent_name,
            instructions=config.instructions,
            default_options=options,
            tools=tuple(tools),
        )

        async def invoke(prompt: str, session: AgentSession) -> AgentResponse[Any]:
            response = await agent.run(prompt, session=session)
            return response

        return cls(config=config, invoker=invoke, framework_agent=agent)

    @property
    def config(self) -> CoordinatorAgentConfig:
        return self._config

    @property
    def framework_agent(self) -> Agent[OpenAIChatOptions] | None:
        return self._framework_agent

    @property
    def request_options(self) -> Mapping[str, object]:
        return {
            "model": self._config.model,
            "base_url": self._config.base_url,
            "reasoning_effort": self._config.reasoning_effort.value,
            "max_output_tokens": self._config.max_output_tokens,
            "max_decision_steps": self._config.max_decision_steps,
            "max_structured_response_attempts": (self._config.max_structured_response_attempts),
            "request_timeout_seconds": self._config.request_timeout_seconds,
            "store": False,
        }

    def create_session(self, *, session_id: str) -> AgentSession:
        return AgentSession(session_id=ensure_text(session_id, "session_id"))

    async def decide(
        self,
        prompt: str,
        *,
        session: AgentSession,
        activation_id: str,
        step: int,
    ) -> CoordinatorDecisionResult:
        normalized_prompt = ensure_text(prompt, "prompt")
        normalized_activation_id = ensure_text(activation_id, "activation_id")
        self._validate_step(session, activation_id=normalized_activation_id, step=step)
        invocation_prompt = self._build_invocation_prompt(
            normalized_prompt,
            activation_id=normalized_activation_id,
            step=step,
        )
        response: AgentResponse[Any] | None = None
        validation_error: CoordinatorDomainError | ValueError | None = None
        request_prompt = invocation_prompt
        for attempt in range(1, self._config.max_structured_response_attempts + 1):
            response = await self._invoke(request_prompt, session=session)
            try:
                value = cast(object, response.value)
                decision = CoordinatorDecision.from_value(value)
                break
            except (CoordinatorDomainError, ValueError) as error:
                validation_error = error
                if attempt == self._config.max_structured_response_attempts:
                    detail = self._structured_error_detail(response, error)
                    raise CoordinatorStructuredResponseError(
                        f"coordinator model returned an invalid structured decision: {detail}"
                    ) from error
                request_prompt = self._build_correction_prompt(
                    activation_id=normalized_activation_id,
                    step=step,
                    validation_error=error,
                )
        else:  # pragma: no cover - range is validated and the loop always returns or raises
            raise CoordinatorStructuredResponseError(
                "coordinator model returned an invalid structured decision"
            ) from validation_error

        assert response is not None

        self._record_step(session, activation_id=normalized_activation_id, step=step)
        finish_reason = None if response.finish_reason is None else str(response.finish_reason)
        return CoordinatorDecisionResult(
            decision=decision,
            response_id=response.response_id,
            finish_reason=finish_reason,
        )

    async def _invoke(
        self,
        prompt: str,
        *,
        session: AgentSession,
    ) -> AgentResponse[Any]:
        try:
            async with asyncio.timeout(self._config.request_timeout_seconds):
                return await self._invoker(prompt, session)
        except TimeoutError as error:
            timeout_seconds = self._config.request_timeout_seconds
            raise CoordinatorModelUnavailableError(
                f"coordinator model timed out after {timeout_seconds:g} seconds"
            ) from error
        except AgentFrameworkException as error:
            raise CoordinatorModelUnavailableError("coordinator model request failed") from error

    @staticmethod
    def _structured_error_detail(
        response: AgentResponse[Any],
        error: CoordinatorDomainError | ValueError,
    ) -> str:
        finish_reason = None if response.finish_reason is None else str(response.finish_reason)
        if finish_reason and finish_reason not in {"stop", "completed"}:
            return f"finish_reason={finish_reason}; {error}"
        return str(error)

    @staticmethod
    def _build_correction_prompt(
        *,
        activation_id: str,
        step: int,
        validation_error: CoordinatorDomainError | ValueError,
    ) -> str:
        context = {
            "activation_id": activation_id,
            "decision_step": step,
            "validation_error": str(validation_error),
        }
        return (
            "Your previous decision was rejected by deterministic validation. Correct that same "
            "decision without changing the goal or decision step. Follow the configured action "
            "contract and return only one structured decision. Validation context:\n"
            + json.dumps(
                context,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            )
        )

    def _validate_step(self, session: AgentSession, *, activation_id: str, step: int) -> None:
        if step < 1:
            raise CoordinatorDecisionStepError("decision step must be at least 1")
        if step > self._config.max_decision_steps:
            raise CoordinatorDecisionLimitExceeded(
                f"activation exceeds max_decision_steps={self._config.max_decision_steps}"
            )
        previous_activation_id, previous_step = self._read_ledger(session)
        if previous_activation_id == activation_id:
            expected_step = previous_step + 1
            if step != expected_step:
                raise CoordinatorDecisionStepError(
                    f"activation {activation_id} expects step {expected_step}, received {step}"
                )
        elif step != 1:
            raise CoordinatorDecisionStepError("a new activation must start at step 1")

    @staticmethod
    def _read_ledger(session: AgentSession) -> tuple[str | None, int]:
        value = cast(object, session.state.get(_SESSION_LEDGER_KEY))
        if value is None:
            return None, 0
        if not isinstance(value, dict):
            raise CoordinatorDecisionStepError("coordinator decision ledger is invalid")
        raw = cast(dict[object, object], value)
        activation_id = raw.get("activation_id")
        step = raw.get("step")
        if (
            not isinstance(activation_id, str)
            or isinstance(step, bool)
            or not isinstance(step, int)
        ):
            raise CoordinatorDecisionStepError("coordinator decision ledger is invalid")
        return activation_id, step

    @staticmethod
    def _record_step(session: AgentSession, *, activation_id: str, step: int) -> None:
        session.state[_SESSION_LEDGER_KEY] = {
            "activation_id": activation_id,
            "step": step,
        }

    @staticmethod
    def _build_invocation_prompt(prompt: str, *, activation_id: str, step: int) -> str:
        context = {
            "activation_id": activation_id,
            "decision_step": step,
            "input": prompt,
        }
        return "Decide the next orchestration action for this context:\n" + json.dumps(
            context,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )


def dump_agent_session(session: AgentSession) -> str:
    return json.dumps(
        session.to_dict(),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def load_agent_session(payload: str) -> AgentSession:
    try:
        value = cast(object, json.loads(payload))
    except json.JSONDecodeError as error:
        raise CoordinatorAgentError("agent session payload must be valid JSON") from error
    if not isinstance(value, dict):
        raise CoordinatorAgentError("agent session payload must be a JSON object")
    raw = cast(dict[object, object], value)
    if any(not isinstance(key, str) for key in raw):
        raise CoordinatorAgentError("agent session payload keys must be strings")
    try:
        return AgentSession.from_dict(cast(dict[str, Any], raw))
    except (KeyError, TypeError, ValueError) as error:
        raise CoordinatorAgentError("agent session payload is invalid") from error
