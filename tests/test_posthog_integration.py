import os
import unittest
import uuid

from django.test import SimpleTestCase, tag

from anymail.exceptions import AnymailAPIError
from anymail.message import AnymailMessage

from .utils import AnymailTestMixin, override_settings, sample_image_content

ANYMAIL_TEST_POSTHOG_SECRET_KEY = os.getenv("ANYMAIL_TEST_POSTHOG_SECRET_KEY")
ANYMAIL_TEST_POSTHOG_API_URL = os.getenv(
    "ANYMAIL_TEST_POSTHOG_API_URL", "https://us.posthog.com/"
)
# A sending domain verified in the PostHog project. Without it, the tests send
# from the project's default sender.
ANYMAIL_TEST_POSTHOG_DOMAIN = os.getenv("ANYMAIL_TEST_POSTHOG_DOMAIN")
ANYMAIL_TEST_POSTHOG_RECIPIENT = os.getenv(
    "ANYMAIL_TEST_POSTHOG_RECIPIENT", "test+to1@anymail.dev"
)
ANYMAIL_TEST_POSTHOG_TEMPLATE_ID = os.getenv("ANYMAIL_TEST_POSTHOG_TEMPLATE_ID")
SENDER = (
    f"Anymail test <from@{ANYMAIL_TEST_POSTHOG_DOMAIN}>"
    if ANYMAIL_TEST_POSTHOG_DOMAIN
    else None
)


def recipient_variant(tag):
    username, domain = ANYMAIL_TEST_POSTHOG_RECIPIENT.split("@")
    return f"{username.split('+')[0]}+{tag}@{domain}"


@tag("posthog", "live")
@unittest.skipUnless(
    ANYMAIL_TEST_POSTHOG_SECRET_KEY,
    "Set ANYMAIL_TEST_POSTHOG_SECRET_KEY environment variable"
    " to run PostHog integration tests",
)
@override_settings(
    MAILERS={
        "default": {
            "BACKEND": "anymail.backends.posthog.EmailBackend",
            "OPTIONS": {
                "secret_key": ANYMAIL_TEST_POSTHOG_SECRET_KEY,
                "api_url": ANYMAIL_TEST_POSTHOG_API_URL,
            },
        },
    },
)
class PostHogBackendIntegrationTests(AnymailTestMixin, SimpleTestCase):
    """PostHog API integration tests

    These tests run against the **live** PostHog API, using the
    environment variable `ANYMAIL_TEST_POSTHOG_SECRET_KEY` as the project
    secret API key (with the transactional_email:write scope).
    If that variable is not set, these tests won't run.
    """

    def setUp(self):
        super().setUp()
        self.message = self.order_shipped("Anymail PostHog integration test")

    @staticmethod
    def order_shipped(subject):
        message = AnymailMessage(
            subject=subject,
            body="This is a text-only test message sent by Anymail.",
            to=[f"Recipient <{ANYMAIL_TEST_POSTHOG_RECIPIENT}>"],
        )
        message.from_email = SENDER
        message.attach_alternative(
            "<p>This is an <strong>HTML</strong> test message.</p>", "text/html"
        )
        return message

    def test_simple_send(self):
        sent_count = self.message.send()
        self.assertEqual(sent_count, 1)
        anymail_status = self.message.anymail_status
        self.assertEqual(anymail_status.status, {"queued"})
        self.assertRegex(anymail_status.message_id, r"\S+")
        self.assertEqual(
            anymail_status.recipients[ANYMAIL_TEST_POSTHOG_RECIPIENT].message_id,
            anymail_status.message_id,
        )

    def test_all_options(self):
        message = AnymailMessage(
            subject="Anymail PostHog all-options integration test",
            body="This is a text-only test message sent by Anymail.",
            to=[f"Recipient <{ANYMAIL_TEST_POSTHOG_RECIPIENT}>"],
            cc=[f"Copy <{recipient_variant('cc')}>"],
            bcc=[recipient_variant("bcc")],
            reply_to=["reply1@example.com", "Reply 2 <reply2@example.com>"],
            track_opens=False,
            track_clicks=False,
            esp_extra={
                "distinct_id": "anymail-integration-test",
                "template_key": "anymail-integration-test",
            },
        )
        message.from_email = SENDER
        message.attach("attachment1.txt", "Here is some\ntext for you", "text/plain")
        cid = message.attach_inline_image(sample_image_content(), "sample_image.png")
        message.attach_alternative(
            f'<p>Inline image: <img src="cid:{cid}"></p>', "text/html"
        )

        message.send()
        self.assertEqual(message.anymail_status.status, {"queued"})
        self.assertEqual(len(message.anymail_status.recipients), 3)

    def test_idempotent_retry_sends_once(self):
        idempotency_key = f"anymail-integration-{uuid.uuid4()}"
        first = self.order_shipped("Anymail PostHog idempotency integration test")
        first.esp_extra = {"idempotency_key": idempotency_key}
        first.send()
        retry = self.order_shipped("Anymail PostHog idempotency integration test")
        retry.esp_extra = {"idempotency_key": idempotency_key}
        retry.send()
        self.assertEqual(
            retry.anymail_status.message_id, first.anymail_status.message_id
        )
        self.assertIs(retry.anymail_status.esp_response.json()["replayed"], True)

    @unittest.skipUnless(
        ANYMAIL_TEST_POSTHOG_TEMPLATE_ID,
        "Set ANYMAIL_TEST_POSTHOG_TEMPLATE_ID to test PostHog templates",
    )
    def test_template(self):
        message = AnymailMessage(
            to=[ANYMAIL_TEST_POSTHOG_RECIPIENT],
            template_id=ANYMAIL_TEST_POSTHOG_TEMPLATE_ID,
            merge_global_data={"order": "981"},
        )
        message.from_email = None
        message.send()
        self.assertEqual(message.anymail_status.status, {"queued"})

    @override_settings(
        MAILERS={
            "default": {
                "BACKEND": "anymail.backends.posthog.EmailBackend",
                "OPTIONS": {
                    "secret_key": "phs_this_is_not_a_valid_key",
                    "api_url": ANYMAIL_TEST_POSTHOG_API_URL,
                },
            },
        },
    )
    def test_invalid_secret_key(self):
        with self.assertRaises(AnymailAPIError) as cm:
            self.message.send()
        error = cm.exception
        self.assertEqual(error.status_code, 401)
        self.assertIs(error.retryable, False)
