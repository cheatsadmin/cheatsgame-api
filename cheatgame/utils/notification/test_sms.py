from django.test import SimpleTestCase

from cheatgame.utils.notification.sms import _log_provider_failure


class SmsLoggingSecurityTests(SimpleTestCase):
    def test_provider_failure_log_never_contains_response_or_customer_values(self):
        with self.assertLogs(
            "cheatgame.utils.notification.sms",
            level="WARNING",
        ) as captured:
            _log_provider_failure(
                provider="faraz_pattern",
                pattern="recovery-pattern",
                recipient="09120000000",
                http_status=503,
                payload={
                    "verification_code": "123456",
                    "recipient": "09120000000",
                    "message": "provider detail",
                    "unexpected": {
                        "echoed_request": [
                            "otp=654321",
                            {"mobile": "+989120000000"},
                        ],
                    },
                },
            )

        message = captured.output[0]
        self.assertIn("sms_provider_failure", message)
        self.assertIn("http_status=503", message)
        self.assertIn("response_type=dict", message)
        self.assertNotIn("123456", message)
        self.assertNotIn("09120000000", message)
        self.assertNotIn("provider detail", message)
        self.assertNotIn("654321", message)
        self.assertNotIn("+989120000000", message)
        self.assertNotIn("echoed_request", message)
