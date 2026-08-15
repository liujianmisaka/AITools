from __future__ import annotations

import json
import unittest

from multi_agent.coordination.models import ContractCheckRequest, DataContract
from multi_agent.coordination.pi import PiContractAdvisor
from multi_agent.coordination.pi_rpc import PiPromptResult
from multi_agent.domain.errors import CoordinatorOutputError


class _FakeClient:
    def __init__(self, text: str) -> None:
        self.text = text
        self.prompt_text = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc_info):
        return None

    async def prompt(self, message: str) -> PiPromptResult:
        self.prompt_text = message
        return PiPromptResult(
            session_id="pi-session",
            text=self.text,
            events=({"type": "agent_settled"},),
        )


def request() -> ContractCheckRequest:
    return ContractCheckRequest(
        phase="input",
        contract=DataContract(
            name="task_input",
            value_type="object",
            required_fields=["goal"],
            allowed_fields=["goal"],
        ),
        value={"goal": "Analyze"},
        candidate_next_steps=[
            {"id": "analysis", "description": "Use the predeclared analysis path"}
        ],
    )


class PiContractAdvisorFakeTests(unittest.IsolatedAsyncioTestCase):
    async def test_parses_narrow_schema_constrained_advice(self) -> None:
        client = _FakeClient(
            json.dumps(
                {
                    "action": "admit",
                    "reason_codes": ["contract_satisfied"],
                    "explanation": "The input matches the contract.",
                    "recommended_next_step_ids": ["analysis"],
                }
            )
        )
        advisor = PiContractAdvisor(client_factory=lambda: client)

        result = await advisor.evaluate(request())

        self.assertEqual(result.draft.action, "admit")
        self.assertEqual(result.advisor_session_id, "pi-session")
        self.assertIn("Never create or modify workflows or tasks", client.prompt_text)
        self.assertIn("Never trigger execution", client.prompt_text)
        self.assertNotIn("available_providers", client.prompt_text)

    async def test_rejects_task_or_workflow_fields_from_pi(self) -> None:
        client = _FakeClient(
            json.dumps(
                {
                    "action": "admit",
                    "reason_codes": ["contract_satisfied"],
                    "explanation": "Invalid extra authority.",
                    "tasks": [{"id": "invented"}],
                }
            )
        )
        advisor = PiContractAdvisor(client_factory=lambda: client)

        with self.assertRaises(CoordinatorOutputError):
            await advisor.evaluate(request())
