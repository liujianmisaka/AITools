from __future__ import annotations

import hashlib
import hmac
import os
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from multi_agent.main import create_app
from tests.helpers import EngineFixture


class WebhookApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = EngineFixture()
        self.client_context = TestClient(create_app(self.fixture.engine))
        self.client = self.client_context.__enter__()
        self.client.post(
            "/api/v1/templates",
            json={
                "id": "webhook_flow",
                "name": "webhook flow",
                "tasks": [
                    {
                        "id": "consume",
                        "provider": "fake",
                        "workspace_id": "repo",
                        "prompt_template": "webhook",
                    }
                ],
            },
        )

    def tearDown(self) -> None:
        self.client_context.__exit__(None, None, None)
        self.fixture._temp.cleanup()

    def _create_unsigned_binding(self, *, max_payload_bytes: int = 1_048_576) -> None:
        response = self.client.post(
            "/api/v1/triggers",
            json={
                "id": "webhook_binding",
                "name": "webhook binding",
                "source_type": "webhook",
                "event_type": "webhook.received",
                "source_key": "hook-1",
                "template_id": "webhook_flow",
                "source_config": {
                    "endpoint_key": "hook-1",
                    "require_signature": False,
                    "max_payload_bytes": max_payload_bytes,
                },
            },
        )
        self.assertEqual(response.status_code, 201, response.text)

    def test_signed_webhook_is_accepted_and_deduplicated(self) -> None:
        with patch.dict(
            os.environ,
            {"MULTI_AGENT_WEBHOOK_SECRET_HOOK_SIGNED": "secret"},
        ):
            created = self.client.post(
                "/api/v1/triggers",
                json={
                    "id": "signed_binding",
                    "name": "signed binding",
                    "source_type": "webhook",
                    "event_type": "webhook.received",
                    "source_key": "hook-signed",
                    "template_id": "webhook_flow",
                    "source_config": {
                        "endpoint_key": "hook-signed",
                        "secret_ref": "HOOK_SIGNED",
                        "require_signature": True,
                    },
                },
            )
            self.assertEqual(created.status_code, 201, created.text)
            body = b'{"hello":"world"}'
            signature = "sha256=" + hmac.new(
                b"secret", body, hashlib.sha256
            ).hexdigest()
            first = self.client.post(
                "/api/v1/hooks/webhook/hook-signed",
                content=body,
                headers={
                    "content-type": "application/json",
                    "x-hub-signature-256": signature,
                    "x-event-key": "evt-1",
                },
            )
            self.assertEqual(first.status_code, 202, first.text)
            self.assertFalse(first.json()["deduplicated"])
            duplicate = self.client.post(
                "/api/v1/hooks/webhook/hook-signed",
                content=body,
                headers={
                    "content-type": "application/json",
                    "x-hub-signature-256": signature,
                    "x-event-key": "evt-1",
                },
            )
            self.assertEqual(duplicate.status_code, 202)
            self.assertTrue(duplicate.json()["deduplicated"])

    def test_bad_signature_is_rejected(self) -> None:
        with patch.dict(
            os.environ,
            {"MULTI_AGENT_WEBHOOK_SECRET_HOOK_SIGNED": "secret"},
        ):
            created = self.client.post(
                "/api/v1/triggers",
                json={
                    "id": "signed_binding",
                    "name": "signed binding",
                    "source_type": "webhook",
                    "event_type": "webhook.received",
                    "source_key": "hook-signed",
                    "template_id": "webhook_flow",
                    "source_config": {
                        "endpoint_key": "hook-signed",
                        "secret_ref": "HOOK_SIGNED",
                    },
                },
            )
            self.assertEqual(created.status_code, 201)
            response = self.client.post(
                "/api/v1/hooks/webhook/hook-signed",
                content=b'{"hello":"world"}',
                headers={
                    "content-type": "application/json",
                    "x-hub-signature-256": "sha256=bad",
                },
            )
            self.assertEqual(response.status_code, 401)
            self.assertEqual(response.json()["code"], "webhook_signature_error")

    def test_payload_limit_rejects_before_business_processing(self) -> None:
        self._create_unsigned_binding(max_payload_bytes=16)
        response = self.client.post(
            "/api/v1/hooks/webhook/hook-1",
            content=b"x" * 128,
            headers={"content-type": "application/json"},
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["code"], "webhook_payload_error")

    def test_long_dedup_header_never_causes_a_500(self) -> None:
        endpoint_key = "e" * 63
        self.client.post(
            "/api/v1/triggers",
            json={
                "id": "long_binding",
                "name": "long binding",
                "source_type": "webhook",
                "event_type": "webhook.received",
                "source_key": endpoint_key,
                "template_id": "webhook_flow",
                "source_config": {
                    "endpoint_key": endpoint_key,
                    "require_signature": False,
                },
            },
        )
        response = self.client.post(
            f"/api/v1/hooks/webhook/{endpoint_key}",
            content=b"{}",
            headers={
                "content-type": "application/json",
                "x-event-key": "d" * 490,
            },
        )
        self.assertEqual(response.status_code, 202, response.text)
        self.assertLessEqual(len(response.json()["dedup_key"]), 500)

    def test_missing_dedup_header_uses_time_windowed_payload_hash(self) -> None:
        self._create_unsigned_binding()
        first = self.client.post(
            "/api/v1/hooks/webhook/hook-1",
            content=b'{"value": 1}',
            headers={"content-type": "application/json"},
        )
        second = self.client.post(
            "/api/v1/hooks/webhook/hook-1",
            content=b'{"value":1}',
            headers={"content-type": "application/json"},
        )
        self.assertEqual(first.status_code, 202)
        self.assertFalse(first.json()["deduplicated"])
        self.assertTrue(second.json()["deduplicated"])
        self.assertEqual(first.json()["dedup_key"], second.json()["dedup_key"])
