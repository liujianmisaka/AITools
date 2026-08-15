from __future__ import annotations

import unittest

from multi_agent.coordination.base import ContractAdvisor
from multi_agent.coordination.models import (
    AdvisorDraft,
    AdvisorEnvelope,
    ContractCheckRequest,
    DataContract,
)
from multi_agent.coordination.service import CoordinationService
from multi_agent.domain.errors import CoordinatorOutputError


class _FakeAdvisor(ContractAdvisor):
    name = "pi"

    def __init__(self, draft: AdvisorDraft) -> None:
        self.draft = draft
        self.requests: list[ContractCheckRequest] = []

    async def evaluate(self, request: ContractCheckRequest) -> AdvisorEnvelope:
        self.requests.append(request)
        return AdvisorEnvelope(
            draft=self.draft,
            advisor_session_id="pi-fake-session",
            event_count=4,
        )

    def describe(self):
        return {"name": self.name, "transport": "fake"}


def request(value, *, candidates=None) -> ContractCheckRequest:
    return ContractCheckRequest(
        phase="output",
        contract=DataContract(
            name="analysis_output",
            value_type="object",
            required_fields=["summary"],
            allowed_fields=["summary"],
        ),
        value=value,
        candidate_next_steps=candidates or [],
    )


class CoordinationServiceFakeTests(unittest.IsolatedAsyncioTestCase):
    async def test_admits_contract_valid_value_without_execution_authority(self) -> None:
        advisor = _FakeAdvisor(
            AdvisorDraft(
                action="admit",
                reason_codes=["contract_satisfied"],
                explanation="Required output is present.",
            )
        )
        service = CoordinationService(advisor=advisor)

        result = await service.evaluate(request({"summary": "done"}))

        self.assertEqual(result.decision, "admitted")
        self.assertEqual(result.effective_value, {"summary": "done"})
        self.assertFalse(result.adjusted)
        self.assertFalse(service.describe()["can_create_templates"])
        self.assertFalse(service.describe()["can_submit_instances"])

    async def test_accepts_small_revision_only_after_code_validates_it(self) -> None:
        advisor = _FakeAdvisor(
            AdvisorDraft(
                action="revise",
                reason_codes=["normalized_field_name"],
                explanation="Mapped result to the agreed field.",
                normalized_value={"summary": "done"},
            )
        )

        result = await CoordinationService(advisor=advisor).evaluate(
            request({"result": "done"})
        )

        self.assertEqual(result.decision, "admitted")
        self.assertTrue(result.adjusted)
        self.assertEqual(result.effective_value, {"summary": "done"})

    async def test_deterministic_gate_overrides_invalid_admit(self) -> None:
        advisor = _FakeAdvisor(
            AdvisorDraft(
                action="admit",
                reason_codes=["looks_acceptable"],
                explanation="Advisor accepted it.",
            )
        )

        result = await CoordinationService(advisor=advisor).evaluate(
            request({"wrong": "field"})
        )

        self.assertEqual(result.decision, "rejected")
        self.assertIsNone(result.effective_value)
        self.assertIn("deterministic_contract_violation", result.reason_codes)
        self.assertIn("missing_required_fields:summary", result.contract_violations)

    async def test_rejects_next_step_not_predeclared_by_application(self) -> None:
        advisor = _FakeAdvisor(
            AdvisorDraft(
                action="admit",
                reason_codes=["contract_satisfied"],
                explanation="Continue.",
                recommended_next_step_ids=["invented_task"],
            )
        )

        with self.assertRaises(CoordinatorOutputError):
            await CoordinationService(advisor=advisor).evaluate(
                request({"summary": "done"})
            )
