from __future__ import annotations

import unittest

from multi_agent.providers.utils import redact_payload


class ProviderUtilsTests(unittest.TestCase):
    def test_redacts_credentials_but_preserves_usage_counts(self) -> None:
        payload = redact_payload(
            {
                "api_key": "secret-value",
                "nested": {"accessToken": "hidden", "input_tokens": 42},
            }
        )

        self.assertEqual(payload["api_key"], "***")
        self.assertEqual(payload["nested"]["accessToken"], "***")
        self.assertEqual(payload["nested"]["input_tokens"], 42)

    def test_depth_limit_does_not_fall_back_to_secret_bearing_repr(self) -> None:
        nested = {"api_key": "must-not-leak"}
        for _ in range(10):
            nested = {"child": nested}

        payload = redact_payload(nested)

        self.assertNotIn("must-not-leak", repr(payload))
        self.assertIn("<max-depth>", repr(payload))
