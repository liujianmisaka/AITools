from __future__ import annotations

import asyncio
import json
from typing import Any

from multi_agent.coordination.base import ContractAdvisor
from multi_agent.coordination.models import (
    AdmissionDecision,
    AdvisorAction,
    ContractCheckRequest,
    ContractCheckResult,
    ContractValueType,
    DataContract,
)
from multi_agent.domain.errors import CoordinatorContractError, CoordinatorOutputError

_MAX_ADVISOR_INPUT_BYTES = 1_048_576


class CoordinationService:
    """Deterministic gate around untrusted Pi contract advice."""

    def __init__(
        self,
        *,
        advisor: ContractAdvisor,
        max_concurrency: int = 2,
    ) -> None:
        self.advisor = advisor
        self._semaphore = asyncio.Semaphore(max_concurrency)

    def describe(self) -> dict[str, Any]:
        result = self.advisor.describe()
        result.update(
            {
                "authority": "contract_advice_only",
                "workflow_engine": "deterministic",
                "can_create_templates": False,
                "can_submit_instances": False,
                "execution_entrypoint": "/api/v1/instances",
                "supported_phases": ["input", "output"],
            }
        )
        return result

    async def evaluate(self, request: ContractCheckRequest) -> ContractCheckResult:
        self._enforce_advisor_input_limit(request.value)
        async with self._semaphore:
            envelope = await self.advisor.evaluate(request)

        draft = envelope.draft
        allowed_steps = {item.id for item in request.candidate_next_steps}
        unknown_steps = set(draft.recommended_next_step_ids) - allowed_steps
        if unknown_steps:
            raise CoordinatorOutputError(
                f"advisor recommended unknown next steps: {sorted(unknown_steps)}"
            )

        adjusted = draft.action == AdvisorAction.revise
        candidate = draft.normalized_value if adjusted else request.value
        violations = self._contract_violations(request.contract, candidate)
        admitted = draft.action != AdvisorAction.reject and not violations
        reason_codes = list(draft.reason_codes)
        if violations and "deterministic_contract_violation" not in reason_codes:
            reason_codes.append("deterministic_contract_violation")

        return ContractCheckResult(
            advisor=self.advisor.name,
            advisor_session_id=envelope.advisor_session_id,
            event_count=envelope.event_count,
            phase=request.phase,
            advisor_action=draft.action,
            decision=(
                AdmissionDecision.admitted
                if admitted
                else AdmissionDecision.rejected
            ),
            adjusted=adjusted and admitted,
            effective_value=candidate if admitted else None,
            reason_codes=reason_codes,
            explanation=draft.explanation,
            recommended_next_step_ids=(
                draft.recommended_next_step_ids if admitted else []
            ),
            contract_violations=violations,
        )

    @staticmethod
    def _enforce_advisor_input_limit(value: Any) -> None:
        try:
            size = len(
                json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8")
            )
        except (TypeError, ValueError) as exc:
            raise CoordinatorContractError("value must be JSON serializable") from exc
        if size > _MAX_ADVISOR_INPUT_BYTES:
            raise CoordinatorContractError(
                f"value is {size} bytes; advisor input limit is {_MAX_ADVISOR_INPUT_BYTES}"
            )

    @staticmethod
    def _contract_violations(contract: DataContract, value: Any) -> list[str]:
        violations: list[str] = []
        expected = contract.value_type
        type_matches = {
            ContractValueType.object: isinstance(value, dict),
            ContractValueType.array: isinstance(value, list),
            ContractValueType.string: isinstance(value, str),
            ContractValueType.number: isinstance(value, (int, float))
            and not isinstance(value, bool),
            ContractValueType.boolean: isinstance(value, bool),
        }[expected]
        if not type_matches:
            violations.append(f"expected_{expected.value}")
        elif expected == ContractValueType.object:
            assert isinstance(value, dict)
            missing = sorted(set(contract.required_fields) - set(value))
            if missing:
                violations.append(f"missing_required_fields:{','.join(missing)}")
            if contract.allowed_fields is not None:
                unexpected = sorted(set(value) - set(contract.allowed_fields))
                if unexpected:
                    violations.append(f"unexpected_fields:{','.join(unexpected)}")

        try:
            size = len(
                json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8")
            )
        except (TypeError, ValueError):
            violations.append("not_json_serializable")
        else:
            if size > contract.max_serialized_bytes:
                violations.append(
                    f"serialized_size_exceeded:{size}>{contract.max_serialized_bytes}"
                )
        return violations
