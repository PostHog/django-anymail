from base64 import b64decode
from datetime import datetime, timezone
from email.message import MIMEPart
from email.mime.image import MIMEImage

import django
import requests
from django.core import mail
from django.test import SimpleTestCase, tag

from anymail import __version__ as ANYMAIL_VERSION
from anymail.exceptions import (
    AnymailAPIError,
    AnymailConfigurationError,
    AnymailUnsupportedFeature,
)
from anymail.message import AnymailMessage

from .mock_requests_backend import (
    RequestsBackendMockAPITestCase,
    SessionSharingTestCases,
)
from .utils import (
    AnymailTestMixin,
    create_text_attachment,
    decode_att,
    override_settings,
)

POSTHOG_BASE_OPTIONS = {"secret_key": "phs_test_secret_key"}
SEND_URL = "https://us.posthog.com/api/projects/@current/messaging/send/email/"
TEMPLATE_ID = "0190f0c2-7d1e-7a3b-9c2e-5f1a2b3c4d5e"


def attach_inline(message, content, filename, subtype, content_id=None):
    if django.VERSION >= (6, 0):
        part = MIMEPart()
        part.set_content(
            content,
            maintype="image",
            subtype=subtype,
            disposition="inline",
            filename=filename,
            cid=content_id,
        )
    else:
        part = MIMEImage(content, subtype)
        part.add_header("Content-Disposition", "inline", filename=filename)
        if content_id:
            part.add_header("Content-ID", content_id)
    message.attach(part)


@tag("posthog")
@override_settings(
    MAILERS={
        "default": {
            "BACKEND": "anymail.backends.posthog.EmailBackend",
            "OPTIONS": POSTHOG_BASE_OPTIONS,
        },
    },
)
class PostHogBackendMockAPITestCase(RequestsBackendMockAPITestCase):
    DEFAULT_RAW_RESPONSE = (
        b'{"message_id": "0190f0c2-aaaa-7bbb-8ccc-ddddeeeeffff", "replayed": false}'
    )
    DEFAULT_CONTENT_TYPE = "application/json"
    DEFAULT_STATUS_CODE = 202

    def setUp(self):
        super().setUp()
        self.message = self.order_shipped()

    @staticmethod
    def order_shipped(**kwargs):
        message = AnymailMessage(
            subject="Your order shipped",
            body="On its way.",
            from_email="Shop <shop@example.com>",
            to=["Ana <ana@example.com>"],
            **kwargs,
        )
        message.attach_alternative("<p>On its way.</p>", "text/html")
        return message

    def assert_sent(self, body, idempotency_key=None):
        self.assert_esp_called(SEND_URL)
        self.assertEqual(self.get_api_call_json(), body)
        headers = self.get_api_call_headers()
        self.assertEqual(
            headers["X-PostHog-Integration"], f"django-anymail/{ANYMAIL_VERSION}"
        )
        self.assertEqual(headers.get("Idempotency-Key"), idempotency_key)

    def assert_refused(self, feature):
        with self.assertRaisesMessage(AnymailUnsupportedFeature, feature):
            self.message.send()
        self.assert_esp_not_called()


@tag("posthog")
class PostHogConformanceTests(PostHogBackendMockAPITestCase):
    """The adapter contract's conformance cases that apply to Anymail"""

    RENDERED = {
        "to": "ana@example.com",
        "from": "Shop <shop@example.com>",
        "subject": "Your order shipped",
        "html": "<p>On its way.</p>",
        "text": "On its way.",
        "template_key": "django-anymail",
    }

    def test_01_rendered_minimal(self):
        self.message.send()
        self.assert_sent(self.RENDERED)

    def test_02_display_names(self):
        self.message.cc = ["Bo <bo@example.com>"]
        self.message.reply_to = ["Help <help@example.com>"]
        self.message.send()
        self.assert_sent(
            {
                **self.RENDERED,
                "cc": ["bo@example.com"],
                "reply_to": ["help@example.com"],
            }
        )

    def test_03_cc_bcc_reply_to(self):
        self.message.cc = ["bo@example.com"]
        self.message.bcc = ["audit@example.com"]
        self.message.reply_to = ["a@example.com", "b@example.com"]
        self.message.send()
        self.assert_sent(
            {
                **self.RENDERED,
                "cc": ["bo@example.com"],
                "bcc": ["audit@example.com"],
                "reply_to": ["a@example.com", "b@example.com"],
            }
        )

    def test_04_attachments_and_inline(self):
        message = AnymailMessage(
            subject="Your order shipped",
            body="On its way.",
            from_email="Shop <shop@example.com>",
            to=["Ana <ana@example.com>"],
        )
        message.attach_alternative('<img src="cid:logo">', "text/html")
        message.attach("invoice.pdf", b64decode("JVBERi0xLjQK"), "application/pdf")
        attach_inline(message, b64decode("iVBORw0KGgo="), "logo.png", "png", "<logo>")
        message.send()
        self.assert_sent(
            {
                **self.RENDERED,
                "html": '<img src="cid:logo">',
                "attachments": [
                    {
                        "filename": "invoice.pdf",
                        "content_type": "application/pdf",
                        "content": "JVBERi0xLjQK",
                    },
                    {
                        "filename": "logo.png",
                        "content_type": "image/png",
                        "content": "iVBORw0KGgo=",
                        "content_id": "logo",
                    },
                ],
            }
        )

    def test_05_posthog_options(self):
        self.message.esp_extra = {
            "distinct_id": "user_42",
            "template_key": "order-shipped",
            "idempotency_key": "order-981-shipped",
        }
        self.message.track_opens = False
        self.message.track_clicks = False
        self.message.send()
        self.assert_sent(
            {
                **self.RENDERED,
                "template_key": "order-shipped",
                "distinct_id": "user_42",
                "capture_engagement": False,
            },
            idempotency_key="order-981-shipped",
        )

    # 06-template-key-derived does not apply: Anymail has no name of its own
    # for a message, so the key goes from explicit to configured to fallback.

    @override_settings(
        MAILERS={
            "default": {
                "BACKEND": "anymail.backends.posthog.EmailBackend",
                "OPTIONS": {
                    **POSTHOG_BASE_OPTIONS,
                    "send_defaults": {"esp_extra": {"template_key": "transactional"}},
                },
            },
        },
    )
    def test_07_template_key_config_default(self):
        self.message.send()
        self.assert_sent({**self.RENDERED, "template_key": "transactional"})

    def test_08_refuse_invalid_template_key(self):
        self.message.esp_extra = {"template_key": "order shipped!"}
        self.assert_refused("template_key 'order shipped!'")

    def test_09_refuse_multiple_to(self):
        self.message.to = ["ana@example.com", "bo@example.com"]
        self.assert_refused("more than one `to` address")

    def test_10_refuse_inline_without_cid(self):
        self.message.attachments = []
        attach_inline(self.message, b64decode("iVBORw0KGgo="), "logo.png", "png")
        self.assert_refused("inline attachments without Content-ID")

    def test_11_headers_ignored_and_refused(self):
        self.message.extra_headers = {
            "Message-ID": "<x@example.com>",
            "X-Campaign": "spring",
        }
        self.assert_refused("custom header 'X-Campaign'")

    def test_11_ignored_headers_alone_send(self):
        self.message.extra_headers = {
            "Message-ID": "<x@example.com>",
            "Date": "Thu, 05 Nov 2026 12:00:00 +0000",
            "MIME-Version": "1.0",
            "Content-Language": "en",
            "X-Mailer": "Django",
        }
        self.message.send()
        self.assert_sent(self.RENDERED)

    def test_12_refuse_tags(self):
        self.message.tags = ["receipts"]
        self.assert_refused("tags")

    def test_12_refuse_metadata(self):
        self.message.metadata = {"order": "981"}
        self.assert_refused("metadata")

    def test_13_refuse_envelope_mismatch(self):
        self.message.envelope_sender = "bounce@example.com"
        self.assert_refused("envelope_sender other than the from_email address")

    def test_13_envelope_sender_matching_from_sends(self):
        self.message.envelope_sender = "shop@example.com"
        self.message.send()
        self.assert_sent(self.RENDERED)

    def test_14_refuse_mixed_tracking(self):
        self.message.track_opens = True
        self.message.track_clicks = False
        self.assert_refused("track_opens and track_clicks set to different values")

    def test_14_tracking_that_agrees(self):
        for tracking in (True, False):
            with self.subTest(tracking=tracking):
                message = self.order_shipped()
                message.track_opens = tracking
                message.track_clicks = tracking
                message.send()
                self.assert_sent({**self.RENDERED, "capture_engagement": tracking})

    def test_14_one_tracking_option_sets_capture_engagement(self):
        self.message.track_clicks = False
        self.message.send()
        self.assert_sent({**self.RENDERED, "capture_engagement": False})

    def test_15_template_form(self):
        self.message = AnymailMessage(
            to=["Ana <ana@example.com>"],
            template_id=TEMPLATE_ID,
            merge_global_data={"order": "981"},
            esp_extra={"distinct_id": "user_42"},
        )
        self.message.from_email = None
        self.message.send()
        self.assert_sent(
            {
                "to": "ana@example.com",
                "template_id": TEMPLATE_ID,
                "message_data": {"order": "981"},
                "distinct_id": "user_42",
            }
        )

    # 16-refuse-template-unsupported does not apply: Anymail carries templates.

    def test_17_refuse_template_with_content(self):
        self.message.template_id = TEMPLATE_ID
        self.assert_refused("template_id with from_email, subject, body")

    def test_17_refuse_template_with_content_in_esp_extra(self):
        self.message = AnymailMessage(
            to=["ana@example.com"],
            template_id=TEMPLATE_ID,
            esp_extra={"subject": "Your order shipped"},
        )
        self.message.from_email = None
        self.assert_refused("(found subject)")


@tag("posthog")
class PostHogRefusalTests(PostHogBackendMockAPITestCase):
    """Contract refusals beyond the conformance cases"""

    def test_refuse_no_to(self):
        self.message.to = []
        self.message.cc = ["bo@example.com"]
        self.assert_refused("more than one `to` address")

    def test_refuse_send_at(self):
        self.message.send_at = datetime(2026, 12, 1, tzinfo=timezone.utc)
        self.assert_refused("send_at")

    def test_refuse_merge_data(self):
        self.message.merge_data = {"ana@example.com": {"order": "981"}}
        self.assert_refused("merge_data")

    def test_refuse_amp_part(self):
        self.message.attach_alternative("<html amp4email></html>", "text/x-amp-html")
        self.assert_refused("alternative part with type 'text/x-amp-html'")

    def test_refuse_second_html_part(self):
        self.message.attach_alternative("<p>Again.</p>", "text/html")
        self.assert_refused("multiple html parts")

    def test_refuse_priority_header(self):
        self.message.extra_headers = {"X-Priority": "1"}
        self.assert_refused("custom header 'X-Priority'")

    def test_refuse_merge_global_data_without_template(self):
        self.message.merge_global_data = {"order": "981"}
        self.assert_refused("merge_global_data without template_id")

    @override_settings(ANYMAIL_IGNORE_UNSUPPORTED_FEATURES=True)
    def test_ignored_recipients_are_not_reported_sent(self):
        self.message.to = ["ana@example.com", "bo@example.com"]
        self.message.send()
        self.assertEqual(set(self.message.anymail_status.recipients), {"ana@example.com"})

    @override_settings(ANYMAIL_IGNORE_UNSUPPORTED_FEATURES=True)
    def test_ignored_template_cc_is_not_reported_sent(self):
        self.message = AnymailMessage(
            to=["ana@example.com"], cc=["bo@example.com"], template_id=TEMPLATE_ID
        )
        self.message.from_email = None
        self.message.send()
        self.assert_sent({"to": "ana@example.com", "template_id": TEMPLATE_ID})
        self.assertEqual(set(self.message.anymail_status.recipients), {"ana@example.com"})


    @override_settings(ANYMAIL_IGNORE_UNSUPPORTED_FEATURES=True)
    def test_ignore_unsupported_features_drops_them(self):
        self.message.to = ["ana@example.com", "bo@example.com"]
        self.message.tags = ["receipts"]
        self.message.extra_headers = {"X-Campaign": "spring"}
        self.message.track_opens = True
        self.message.track_clicks = False
        self.message.send()
        self.assert_sent(PostHogConformanceTests.RENDERED)


@tag("posthog")
@override_settings(
    MAILERS={
        "default": {
            "BACKEND": "anymail.backends.posthog.EmailBackend",
            "OPTIONS": {
                **POSTHOG_BASE_OPTIONS,
                "send_defaults": {
                    "track_clicks": True,
                    "esp_extra": {"template_key": "transactional"},
                },
            },
        },
    },
)
class PostHogTemplateDefaultsTests(PostHogBackendMockAPITestCase):
    """The template form leaves out rendered-form options, even from defaults"""

    def test_template_omits_template_key_and_tracking(self):
        self.message = AnymailMessage(
            to=["ana@example.com"],
            template_id=TEMPLATE_ID,
            track_opens=True,
            esp_extra={"capture_engagement": True},
        )
        self.message.from_email = None
        self.message.send()
        self.assert_sent({"to": "ana@example.com", "template_id": TEMPLATE_ID})


@tag("posthog")
class PostHogSendTests(PostHogBackendMockAPITestCase):
    def test_text_attachment_sends_bare_mime_type_and_utf8(self):
        # PostHog takes a MIME type with no parameters, so the charset can't
        # travel with it; the content goes as UTF-8.
        self.message.attach(
            create_text_attachment(
                "pièce jointe\n", charset="iso-8859-1", filename="note.txt"
            )
        )
        self.message.send()
        attachment = self.get_api_call_json()["attachments"][0]
        self.assertEqual(attachment["content_type"], "text/plain")
        self.assertEqual(decode_att(attachment["content"]).decode(), "pièce jointe\n")

    def test_send_mail(self):
        mail.send_mail("Subject", "Body", "from@example.com", ["to@example.com"])
        self.assert_sent(
            {
                "to": "to@example.com",
                "from": "from@example.com",
                "subject": "Subject",
                "text": "Body",
                "template_key": "django-anymail",
            }
        )
        self.assertEqual(
            self.get_api_call_headers()["Authorization"], "Bearer phs_test_secret_key"
        )

    def test_without_from_email_uses_project_sender(self):
        self.message.from_email = None
        self.message.send()
        self.assertNotIn("from", self.get_api_call_json())

    def test_other_esp_extra_fields_merge_into_request(self):
        self.message.esp_extra = {"future_field": "value"}
        self.message.send()
        self.assertEqual(self.get_api_call_json()["future_field"], "value")

    def test_anymail_status(self):
        self.message.cc = ["bo@example.com"]
        sent = self.message.send()
        self.assertEqual(sent, 1)
        status = self.message.anymail_status
        self.assertEqual(status.status, {"queued"})
        self.assertEqual(status.message_id, "0190f0c2-aaaa-7bbb-8ccc-ddddeeeeffff")
        self.assertEqual(set(status.recipients), {"ana@example.com", "bo@example.com"})

    @override_settings(
        MAILERS={
            "default": {
                "BACKEND": "anymail.backends.posthog.EmailBackend",
                "OPTIONS": {
                    **POSTHOG_BASE_OPTIONS,
                    "api_url": "https://eu.posthog.com",
                },
            },
        },
    )
    def test_api_url_selects_region(self):
        self.message.send()
        self.assert_esp_called(
            "https://eu.posthog.com/api/projects/@current/messaging/send/email/"
        )

    def test_no_internal_retry(self):
        self.set_mock_response(status_code=503, json_data={"code": "x"})
        with self.assertRaises(AnymailAPIError):
            self.message.send()
        self.assertEqual(self.mock_request.call_count, 1)


@tag("posthog")
class PostHogErrorMapTests(PostHogBackendMockAPITestCase):
    """One test per row of the contract's errors table"""

    def assert_api_error(self, status, code, *, retryable, retry_after=None, **kw):
        self.set_mock_response(
            status_code=status,
            json_data={
                "type": "invalid_request",
                "code": code,
                "detail": f"Detail for {code}",
                "attr": "to",
            },
            **kw,
        )
        with self.assertRaises(AnymailAPIError) as cm:
            self.message.send()
        error = cm.exception
        self.assertEqual(error.status_code, status)
        self.assertEqual(error.code, code)
        self.assertEqual(error.detail, f"Detail for {code}")
        self.assertEqual(error.attr, "to")
        self.assertIs(error.retryable, retryable)
        self.assertEqual(error.retry_after, retry_after)

    def test_202_sent(self):
        self.message.send()
        self.assertEqual(self.message.anymail_status.status, {"queued"})

    def assert_bad_success_response(self, **kw):
        self.set_mock_response(status_code=202, **kw)
        with self.assertRaisesMessage(AnymailAPIError, "PostHog API response") as cm:
            self.message.send()
        # A 2xx without a usable message_id is not a retryable status.
        error = cm.exception
        self.assertEqual(error.status_code, 202)
        self.assertIsNone(error.code)
        self.assertIs(error.retryable, False)
        self.assertIsNone(error.retry_after)

    def test_202_without_message_id(self):
        self.assert_bad_success_response(json_data={})

    def test_202_invalid_json(self):
        self.assert_bad_success_response(raw=b"<html>ok</html>")

    def test_400_invalid_request(self):
        self.assert_api_error(400, "invalid_input", retryable=False)

    def test_401_wrong_key(self):
        self.assert_api_error(401, "authentication_failed", retryable=False)

    def test_404_template_not_found(self):
        self.assert_api_error(404, "not_found", retryable=False)

    def test_409_idempotency_key_in_flight(self):
        self.assert_api_error(409, "idempotency_key_in_flight", retryable=True)

    def test_422_recipient_suppressed(self):
        self.assert_api_error(422, "recipient_suppressed", retryable=False)

    def test_429_quota_limited(self):
        self.assert_api_error(429, "quota_limited", retryable=False)

    def test_429_throttled(self):
        def respond_with_retry_after(*args, **kwargs):
            response = self.MockResponse(
                429,
                b'{"type": "throttled_error", "code": "throttled", '
                b'"detail": "Detail for throttled", "attr": "to"}',
                "application/json",
                test_case=self,
            )
            response.headers["Retry-After"] = "7"
            return response

        self.mock_request.side_effect = respond_with_retry_after
        self.assert_api_error(429, "throttled", retryable=True, retry_after=7)

    def test_503_may_still_go_out(self):
        self.assert_api_error(503, "delivery_unavailable", retryable=True)

    def test_other_5xx(self):
        self.assert_api_error(500, "error", retryable=False)

    def test_network_error(self):
        self.mock_request.side_effect = requests.ConnectionError("unreachable")
        with self.assertRaises(AnymailAPIError) as cm:
            self.message.send()
        error = cm.exception
        self.assertIsNone(error.status_code)
        self.assertIs(error.retryable, True)
        self.assertIsNone(error.retry_after)

    def test_error_body_without_json(self):
        self.set_mock_response(
            status_code=502, raw=b"Bad gateway", content_type="text/html"
        )
        with self.assertRaises(AnymailAPIError) as cm:
            self.message.send()
        self.assertIsNone(cm.exception.code)
        self.assertIs(cm.exception.retryable, False)


@tag("posthog")
@override_settings(
    MAILERS={"default": {"BACKEND": "anymail.backends.posthog.EmailBackend"}},
)
class PostHogBackendImproperlyConfiguredTests(AnymailTestMixin, SimpleTestCase):
    def test_missing_secret_key(self):
        with self.assertRaisesRegex(
            AnymailConfigurationError,
            r"'secret_key'|\bPOSTHOG_SECRET_KEY.*ANYMAIL_POSTHOG_SECRET_KEY",
        ):
            mail.send_mail("Subject", "Body", "from@example.com", ["to@example.com"])


@tag("posthog")
class PostHogBackendSessionSharingTestCase(
    SessionSharingTestCases, PostHogBackendMockAPITestCase
):
    """Requests session sharing tests"""
