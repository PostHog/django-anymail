import hashlib
import json
import os
import re
import tempfile
from base64 import b64decode, b64encode
from datetime import datetime, timezone
from email.message import MIMEPart
from email.mime.image import MIMEImage
from unittest.mock import patch

import django
import requests
import responses
from django.core import mail
from django.test import SimpleTestCase, tag

from anymail import __version__ as ANYMAIL_VERSION
from anymail.backends.posthog import EmailBackend
from anymail.exceptions import (
    AnymailAPIError,
    AnymailConfigurationError,
    AnymailUnsupportedFeature,
)
from anymail.message import AnymailMessage

from .mock_requests_backend import RequestsBackendMockAPITestCase
from .utils import (
    AnymailTestMixin,
    create_text_attachment,
    override_settings,
)

POSTHOG_BASE_OPTIONS = {"secret_key": "phs_test_secret_key"}
SEND_URL = "https://us.posthog.com/api/projects/@current/messaging/send/email/"
UPLOADS_URL = (
    "https://us.posthog.com/api/projects/@current/messaging/attachments/uploads/"
)
STORAGE_URL = "https://attachments.s3.us-east-1.amazonaws.com/team/1/"
INVOICE = b64decode("JVBERi0xLjQK")
LOGO = b64decode("iVBORw0KGgo=")
TEMPLATE_ID = "0190f0c2-7d1e-7a3b-9c2e-5f1a2b3c4d5e"


def sha256_hex(content):
    return hashlib.sha256(content).hexdigest()


def upload_slot(file):
    digest = bytes.fromhex(file["sha256"])
    return {
        "sha256": file["sha256"],
        "status": "upload",
        "url": f"{STORAGE_URL}{file['sha256']}?X-Amz-Signature=abc",
        "method": "PUT",
        "headers": {
            "Content-Length": str(file["size"]),
            "x-amz-checksum-sha256": b64encode(digest).decode(),
        },
        "url_expires_at": "2026-11-13T09:15:00Z",
    }


def present_slot(file):
    return {
        "sha256": file["sha256"],
        "status": "present",
        "expires_at": "2026-12-16T09:00:00Z",
    }


class SessionCredentialsBackend(EmailBackend):
    """A backend whose session carries credentials for some other API"""

    def create_session(self):
        session = super().create_session()
        session.headers["Authorization"] = "Bearer session-wide-credential"
        session.auth = ("proxy-user", "proxy-password")
        return session


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
        # File storage is faked at the transport, so a test sees the upload
        # exactly as the storage host would.
        self.storage = responses.RequestsMock(assert_all_requests_are_fired=False)
        self.storage.add_callback(
            responses.PUT, re.compile(r"https?://.*"), callback=self.answer_storage
        )
        self.storage.start()
        self.addCleanup(self.storage.stop)
        self.storage_answer = None
        self.urls_in_order = []

    def answer_storage(self, request):
        self.urls_in_order.append(request.url)
        answer = self.storage_answer
        if answer is None or isinstance(answer, Exception):
            return answer or (200, {}, b"")
        return answer.status_code, dict(answer.headers), answer.content

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

    def response(self, status_code, json_data=None, raw=b"", headers=None):
        if json_data is not None:
            raw = json.dumps(json_data).encode()
        response = self.MockResponse(
            status_code,
            raw,
            "application/json" if json_data is not None else None,
            test_case=self,
        )
        response.headers.update(headers or {})
        return response

    def fake_posthog(self, present=(), uploads=None, storage=None):
        """Answer the upload route, the file storage and the send like PostHog.

        uploads and storage replace PostHog's answer on that route; storage
        may be an exception to raise instead.
        """

        def request(session, method, url, **kwargs):
            if url in (UPLOADS_URL, SEND_URL):
                self.urls_in_order.append(url)
            if url == UPLOADS_URL:
                if uploads is not None:
                    return uploads
                files = json.loads(kwargs["data"])["files"]
                slots = [
                    (
                        present_slot(file)
                        if file["sha256"] in present
                        else upload_slot(file)
                    )
                    for file in files
                ]
                return self.response(200, {"files": slots})
            if url == SEND_URL:
                return self.response(202, json.loads(self.DEFAULT_RAW_RESPONSE))
            raise AssertionError(f"PostHog has no route {url}")

        self.mock_request.side_effect = request
        self.storage_answer = storage

    def calls(self, url_prefix):
        """Every request to a URL starting with url_prefix, in order.

        A storage upload shows the request as it went out.
        """
        to_posthog = [call.kwargs for call in self.mock_request.call_args_list]
        to_storage = [
            {
                "method": call.request.method,
                "url": call.request.url,
                "headers": dict(call.request.headers),
                "data": call.request.body,
            }
            for call in self.storage.calls
        ]
        return [
            call
            for call in to_posthog + to_storage
            if call["url"].startswith(url_prefix)
        ]

    def assert_not_sent(self):
        self.assertEqual(self.calls(SEND_URL), [])


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
        self.fake_posthog()
        message = AnymailMessage(
            subject="Your order shipped",
            body="On its way.",
            from_email="Shop <shop@example.com>",
            to=["Ana <ana@example.com>"],
        )
        message.attach_alternative('<img src="cid:logo">', "text/html")
        message.attach("invoice.pdf", INVOICE, "application/pdf")
        attach_inline(message, LOGO, "logo.png", "png", "<logo>")
        message.send()
        self.assert_sent(
            {
                **self.RENDERED,
                "html": '<img src="cid:logo">',
                "attachments": [
                    {
                        "sha256": sha256_hex(INVOICE),
                        "filename": "invoice.pdf",
                        "content_type": "application/pdf",
                    },
                    {
                        "sha256": sha256_hex(LOGO),
                        "filename": "logo.png",
                        "content_type": "image/png",
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
                "tracking": {"opens": False, "clicks": False},
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

    def test_08_refuse_template_key_with_trailing_newline(self):
        self.message.esp_extra = {"template_key": "order-shipped\n"}
        self.assert_refused("template_key 'order-shipped\\n'")

    def test_08_refuse_template_key_longer_than_posthog_takes(self):
        self.message.esp_extra = {"template_key": "k" * 257}
        self.assert_refused("at most 256")

    def test_08_template_key_at_posthog_limit_sends(self):
        self.message.esp_extra = {"template_key": "k" * 256}
        self.message.send()
        self.assert_sent({**self.RENDERED, "template_key": "k" * 256})

    def test_08_refuse_template_key_not_a_string(self):
        self.message.esp_extra = {"template_key": 42}
        self.assert_refused("template_key 42")

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

    def test_14_tracking_maps_each_control(self):
        cases = [
            ((True, True), {"opens": True, "clicks": True}),
            ((False, False), {"opens": False, "clicks": False}),
            ((True, False), {"opens": True, "clicks": False}),
            ((True, None), {"opens": True}),
            ((False, None), {"opens": False}),
            ((None, True), {"clicks": True}),
            ((None, False), {"clicks": False}),
        ]
        for (track_opens, track_clicks), tracking in cases:
            with self.subTest(track_opens=track_opens, track_clicks=track_clicks):
                message = self.order_shipped()
                message.track_opens = track_opens
                message.track_clicks = track_clicks
                message.send()
                self.assert_sent({**self.RENDERED, "tracking": tracking})

    def test_14_no_tracking_controls_send_no_tracking(self):
        self.message.send()
        body = self.get_api_call_json()
        self.assertNotIn("tracking", body)
        self.assertNotIn("capture_engagement", body)

    def test_14_refuse_clicks_without_opens(self):
        self.message.track_opens = False
        self.message.track_clicks = True
        self.assert_refused("track_clicks without track_opens")

    @override_settings(ANYMAIL_IGNORE_UNSUPPORTED_FEATURES=True)
    def test_14_ignored_clicks_without_opens_send_opens_off(self):
        self.message.track_opens = False
        self.message.track_clicks = True
        self.message.send()
        self.assert_sent({**self.RENDERED, "tracking": {"opens": False}})

    def test_14_capture_engagement_in_esp_extra_joins_tracking(self):
        # PostHog refuses capture_engagement beside tracking, so it travels
        # as tracking.engagement.
        self.message.track_opens = True
        self.message.esp_extra = {"capture_engagement": False}
        self.message.send()
        self.assert_sent(
            {**self.RENDERED, "tracking": {"opens": True, "engagement": False}}
        )

    def test_14_capture_engagement_alone_passes_through(self):
        self.message.esp_extra = {"capture_engagement": False}
        self.message.send()
        self.assert_sent({**self.RENDERED, "capture_engagement": False})

    def test_14_boolean_tracking_drops_capture_engagement(self):
        # PostHog refuses capture_engagement beside tracking, and a boolean
        # tracking already sets engagement.
        for tracking in (True, False):
            with self.subTest(tracking=tracking):
                message = self.order_shipped()
                message.esp_extra = {
                    "tracking": tracking,
                    "capture_engagement": not tracking,
                }
                message.send()
                self.assert_sent({**self.RENDERED, "tracking": tracking})

    def test_14_tracking_in_esp_extra_merges_with_controls(self):
        self.message.track_clicks = False
        self.message.esp_extra = {"tracking": {"engagement": False}}
        self.message.send()
        self.assert_sent(
            {**self.RENDERED, "tracking": {"clicks": False, "engagement": False}}
        )

    def test_14_sensitive_passes_through(self):
        self.message.track_clicks = True
        self.message.esp_extra = {"sensitive": True}
        self.message.send()
        self.assert_sent(
            {**self.RENDERED, "sensitive": True, "tracking": {"clicks": True}}
        )

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

    def test_17_template_with_empty_copies_and_files_sends(self):
        # PostHog refuses cc, bcc and attachments on a template send only when
        # they carry something.
        self.message = AnymailMessage(
            to=["ana@example.com"],
            template_id=TEMPLATE_ID,
            esp_extra={"cc": None, "bcc": [], "attachments": None},
        )
        self.message.from_email = None
        self.message.send()
        self.assert_sent(
            {
                "to": "ana@example.com",
                "template_id": TEMPLATE_ID,
                "cc": None,
                "bcc": [],
                "attachments": None,
            }
        )

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
        self.assertEqual(
            set(self.message.anymail_status.recipients), {"ana@example.com"}
        )

    @override_settings(ANYMAIL_IGNORE_UNSUPPORTED_FEATURES=True)
    def test_ignored_template_cc_is_not_reported_sent(self):
        self.message = AnymailMessage(
            to=["ana@example.com"], cc=["bo@example.com"], template_id=TEMPLATE_ID
        )
        self.message.from_email = None
        self.message.send()
        self.assert_sent({"to": "ana@example.com", "template_id": TEMPLATE_ID})
        self.assertEqual(
            set(self.message.anymail_status.recipients), {"ana@example.com"}
        )

    @override_settings(ANYMAIL_IGNORE_UNSUPPORTED_FEATURES=True)
    def test_ignore_unsupported_features_drops_them(self):
        self.message.to = ["ana@example.com", "bo@example.com"]
        self.message.tags = ["receipts"]
        self.message.extra_headers = {"X-Campaign": "spring"}
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
                    "esp_extra": {
                        "template_key": "transactional",
                        "sensitive": True,
                        "tracking": {"engagement": False},
                    },
                },
            },
        },
    },
)
class PostHogTemplateDefaultsTests(PostHogBackendMockAPITestCase):
    """The template form leaves out rendered-form options, even from defaults"""

    def test_template_ignores_conflicting_tracking_controls(self):
        # track_clicks=True comes from the defaults; a template send carries
        # no tracking, so the clicks-need-opens rule doesn't apply.
        self.message = AnymailMessage(
            to=["ana@example.com"], template_id=TEMPLATE_ID, track_opens=False
        )
        self.message.from_email = None
        self.message.send()
        self.assert_sent({"to": "ana@example.com", "template_id": TEMPLATE_ID})

    def test_template_omits_template_key_tracking_and_sensitive(self):
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
        self.fake_posthog()
        self.message.attach(
            create_text_attachment(
                "pièce jointe\n", charset="iso-8859-1", filename="note.txt"
            )
        )
        self.message.send()
        content = "pièce jointe\n".encode()
        (upload,) = self.calls(STORAGE_URL)
        self.assertEqual(upload["data"], content)
        attachment = self.get_api_call_json()["attachments"][0]
        self.assertEqual(attachment["content_type"], "text/plain")
        self.assertEqual(attachment["sha256"], sha256_hex(content))

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

    def test_anymail_status_follows_esp_extra_recipients(self):
        self.message.cc = ["bo@example.com"]
        self.message.esp_extra = {"to": "carol@example.com", "cc": []}
        self.message.send()
        self.assertEqual(
            set(self.message.anymail_status.recipients), {"carol@example.com"}
        )

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
class PostHogAttachmentUploadTests(PostHogBackendMockAPITestCase):
    """Files go to PostHog's upload route and file storage before the send"""

    def setUp(self):
        super().setUp()
        self.message.attach("invoice.pdf", INVOICE, "application/pdf")
        self.message.attach("logo.png", LOGO, "image/png")

    def test_asks_where_to_upload_then_uploads_then_sends(self):
        self.fake_posthog()
        self.message.esp_extra = {"idempotency_key": "order-981-shipped"}
        self.message.send()
        self.assertEqual(
            [url.split("?")[0] for url in self.urls_in_order],
            [
                UPLOADS_URL,
                STORAGE_URL + sha256_hex(INVOICE),
                STORAGE_URL + sha256_hex(LOGO),
                SEND_URL,
            ],
        )
        (ask,) = self.calls(UPLOADS_URL)
        self.assertEqual(ask["method"], "POST")
        self.assertEqual(
            json.loads(ask["data"]),
            {
                "files": [
                    {"sha256": sha256_hex(INVOICE), "size": len(INVOICE)},
                    {"sha256": sha256_hex(LOGO), "size": len(LOGO)},
                ]
            },
        )
        self.assertEqual(ask["headers"]["Authorization"], "Bearer phs_test_secret_key")
        self.assertEqual(
            ask["headers"]["X-PostHog-Integration"], f"django-anymail/{ANYMAIL_VERSION}"
        )
        self.assertNotIn("Idempotency-Key", ask["headers"])
        self.assertEqual(
            self.get_api_call_headers()["Idempotency-Key"], "order-981-shipped"
        )

    def test_uploads_bytes_with_given_headers_and_no_key(self):
        self.fake_posthog()
        self.message.send()
        invoice_upload = self.calls(STORAGE_URL)[0]
        self.assertEqual(invoice_upload["method"], "PUT")
        slot = upload_slot({"sha256": sha256_hex(INVOICE), "size": len(INVOICE)})
        self.assertEqual(invoice_upload["url"], slot["url"])
        self.assertEqual(invoice_upload["data"], INVOICE)
        self.assertEqual(invoice_upload["headers"], slot["headers"])

    def test_present_file_is_not_uploaded(self):
        self.fake_posthog(present={sha256_hex(INVOICE)})
        self.message.send()
        self.assertEqual(
            [call["url"].split("?")[0] for call in self.calls(STORAGE_URL)],
            [STORAGE_URL + sha256_hex(LOGO)],
        )
        self.assertEqual(len(self.get_api_call_json()["attachments"]), 2)

    def test_same_bytes_twice_upload_once(self):
        self.fake_posthog()
        self.message.attach("invoice-copy.pdf", INVOICE, "application/pdf")
        self.message.send()
        (ask,) = self.calls(UPLOADS_URL)
        self.assertEqual(len(json.loads(ask["data"])["files"]), 2)
        self.assertEqual(len(self.calls(STORAGE_URL)), 2)
        attachments = self.get_api_call_json()["attachments"]
        self.assertEqual(
            [att["filename"] for att in attachments],
            ["invoice.pdf", "logo.png", "invoice-copy.pdf"],
        )
        self.assertEqual(attachments[2]["sha256"], sha256_hex(INVOICE))

    def test_no_attachments_skip_the_upload_route(self):
        self.fake_posthog()
        self.message.attachments = []
        self.message.send()
        self.assertEqual(self.calls(UPLOADS_URL), [])
        self.assertNotIn("attachments", self.get_api_call_json())

    @override_settings(ANYMAIL_IGNORE_UNSUPPORTED_FEATURES=True)
    def test_ignored_template_attachments_are_not_uploaded(self):
        self.fake_posthog()
        message = AnymailMessage(to=["ana@example.com"], template_id=TEMPLATE_ID)
        message.from_email = None
        message.attach("invoice.pdf", INVOICE, "application/pdf")
        message.send()
        self.assertEqual(self.calls(UPLOADS_URL), [])
        self.assertEqual(
            self.get_api_call_json(),
            {"to": "ana@example.com", "template_id": TEMPLATE_ID},
        )

    def assert_upload_error(self, code, *, status, retryable, attr, retry_after=None):
        with self.assertRaises(AnymailAPIError) as cm:
            self.message.send()
        error = cm.exception
        self.assertEqual(error.status_code, status)
        self.assertEqual(error.code, code)
        self.assertEqual(error.attr, attr)
        self.assertIs(error.retryable, retryable)
        self.assertEqual(error.retry_after, retry_after)
        self.assert_not_sent()
        return error

    def test_upload_route_refusal_names_the_attachment(self):
        self.fake_posthog(
            uploads=self.response(
                400,
                {
                    "type": "invalid_request",
                    "code": "message_too_large",
                    "detail": "Too large",
                    "attr": "files__1__size",
                },
            )
        )
        self.assert_upload_error(
            "message_too_large", status=400, retryable=False, attr="attachments__1"
        )
        self.assertEqual(self.calls(STORAGE_URL), [])

    def test_upload_route_names_first_attachment_of_a_shared_file(self):
        self.message.attachments = []
        self.message.attach("logo.png", LOGO, "image/png")
        self.message.attach("invoice.pdf", INVOICE, "application/pdf")
        self.message.attach("logo-again.png", LOGO, "image/png")
        self.message.attach("terms.pdf", b"%PDF terms", "application/pdf")
        self.fake_posthog(
            uploads=self.response(
                400, {"code": "message_too_large", "attr": "files__2__size"}
            )
        )
        self.assert_upload_error(
            "message_too_large", status=400, retryable=False, attr="attachments__3"
        )

    def test_daily_upload_quota_is_not_retryable(self):
        self.fake_posthog(
            uploads=self.response(
                429,
                {"code": "upload_quota_exceeded", "attr": None},
                headers={"Retry-After": "3600"},
            )
        )
        self.assert_upload_error(
            "upload_quota_exceeded", status=429, retryable=False, attr="attachments"
        )

    def test_upload_route_missing(self):
        self.fake_posthog(uploads=self.response(404, {"detail": "Not found."}))
        self.assert_upload_error(
            "not_found", status=404, retryable=False, attr="attachments"
        )

    def test_upload_route_unavailable_is_retryable(self):
        self.fake_posthog(
            uploads=self.response(503, {"code": "delivery_unavailable", "attr": None})
        )
        self.assert_upload_error(
            "delivery_unavailable", status=503, retryable=True, attr="attachments"
        )

    def test_upload_route_unreachable_is_retryable(self):
        def request(session, method, url, **kwargs):
            raise requests.ConnectionError("unreachable")

        self.mock_request.side_effect = request
        with self.assertRaises(AnymailAPIError) as cm:
            self.message.send()
        self.assertIs(cm.exception.retryable, True)
        self.assertIsNone(cm.exception.status_code)
        self.assertEqual(self.mock_request.call_count, 1)

    def test_upload_answer_without_a_slot_per_file(self):
        for answer in [
            {},
            {"files": []},
            {"files": [{"sha256": "0" * 64, "status": "present"}] * 2},
            {"files": [{"status": "upload"}, {"status": "upload"}]},
        ]:
            with self.subTest(answer=answer):
                self.mock_request.reset_mock()
                self.fake_posthog(uploads=self.response(200, answer))
                with self.assertRaisesMessage(AnymailAPIError, "upload") as cm:
                    self.message.send()
                self.assertIs(cm.exception.retryable, False)
                self.assertEqual(self.calls(STORAGE_URL), [])
                self.assert_not_sent()

    def test_refuses_plain_http_upload_url(self):
        slot = upload_slot({"sha256": sha256_hex(INVOICE), "size": len(INVOICE)})
        slot["url"] = "http://attachments.example.com/upload"
        self.fake_posthog(
            uploads=self.response(
                200,
                {
                    "files": [
                        slot,
                        present_slot({"sha256": sha256_hex(LOGO)}),
                    ]
                },
            )
        )
        with self.assertRaisesMessage(AnymailAPIError, "https") as cm:
            self.message.send()
        self.assertIs(cm.exception.retryable, False)
        self.assertEqual(cm.exception.attr, "attachments__0")
        self.assertEqual(self.calls("http://attachments.example.com"), [])
        self.assert_not_sent()

    def test_malformed_upload_url_is_an_api_error(self):
        slot = upload_slot({"sha256": sha256_hex(INVOICE), "size": len(INVOICE)})
        slot["url"] = "https://"
        self.fake_posthog(
            uploads=self.response(
                200, {"files": [slot, present_slot({"sha256": sha256_hex(LOGO)})]}
            )
        )
        with self.assertRaises(AnymailAPIError) as cm:
            self.message.send()
        self.assertEqual(cm.exception.code, "attachment_upload_failed")
        self.assertEqual(cm.exception.attr, "attachments__0")
        self.assertIs(cm.exception.retryable, False)
        self.assert_not_sent()

    def test_malformed_upload_url_fails_silently(self):
        slot = upload_slot({"sha256": sha256_hex(INVOICE), "size": len(INVOICE)})
        slot["url"] = "https://"
        self.fake_posthog(
            uploads=self.response(
                200, {"files": [slot, present_slot({"sha256": sha256_hex(LOGO)})]}
            )
        )
        self.assertEqual(self.message.send(fail_silently=True), 0)
        self.assert_not_sent()

    def test_storage_refusal_is_not_retryable(self):
        self.fake_posthog(
            storage=self.response(
                403,
                raw=b"<Error><Code>SignatureDoesNotMatch</Code>"
                b"<Message>The request signature does not match</Message></Error>",
            )
        )
        error = self.assert_upload_error(
            "attachment_upload_failed",
            status=403,
            retryable=False,
            attr="attachments__0",
        )
        self.assertEqual(error.detail, "The request signature does not match")
        self.assertEqual(len(self.calls(STORAGE_URL)), 1)

    def test_expired_upload_url_is_retryable(self):
        # A retry of the send asks PostHog for a fresh upload URL.
        self.fake_posthog(
            storage=self.response(
                403,
                raw=b"<Error><Code>AccessDenied</Code>"
                b"<Message>Request has expired</Message></Error>",
            )
        )
        self.assert_upload_error(
            "attachment_upload_failed",
            status=403,
            retryable=True,
            attr="attachments__0",
        )

    def test_unnamed_inline_image_gets_a_file_name(self):
        self.fake_posthog()
        self.message.attachments = []
        cid = self.message.attach_inline_image(LOGO, subtype="png")
        self.message.send()
        (attachment,) = self.get_api_call_json()["attachments"]
        self.assertEqual(attachment["filename"], "attachment.png")
        self.assertEqual(attachment["content_id"], cid)

    def test_storage_fault_is_retryable(self):
        self.fake_posthog(
            storage=self.response(503, raw=b"", headers={"Retry-After": "2"})
        )
        self.assert_upload_error(
            "attachment_upload_failed",
            status=503,
            retryable=True,
            attr="attachments__0",
            retry_after=2,
        )

    def test_storage_redirect_is_a_failed_upload(self):
        # Following the redirect would carry the file somewhere PostHog didn't name.
        self.fake_posthog(
            storage=self.response(307, headers={"Location": "https://elsewhere/"})
        )
        self.assert_upload_error(
            "attachment_upload_failed",
            status=307,
            retryable=False,
            attr="attachments__0",
        )
        self.assertEqual(len(self.storage.calls), 1)

    def test_storage_unreachable_or_cut_off_is_retryable(self):
        for fault in [
            requests.ConnectionError("blocked by firewall"),
            requests.exceptions.ChunkedEncodingError("connection broken"),
        ]:
            with self.subTest(fault=fault):
                self.fake_posthog(storage=fault)
                with self.assertRaises(AnymailAPIError) as cm:
                    self.message.send()
                error = cm.exception
                self.assertIsNone(error.status_code)
                self.assertEqual(error.code, "attachment_upload_failed")
                self.assertEqual(error.attr, "attachments__0")
                self.assertIs(error.retryable, True)
                self.assert_not_sent()

    def test_secret_key_never_goes_to_storage(self):
        self.fake_posthog()
        self.message.send()
        for upload in self.calls(STORAGE_URL):
            self.assertNotIn("phs_test_secret_key", json.dumps(upload["headers"]))
            self.assertNotIn("Authorization", upload["headers"])

    @override_settings(
        MAILERS={
            "default": {
                "BACKEND": "tests.test_posthog_backend.SessionCredentialsBackend",
                "OPTIONS": POSTHOG_BASE_OPTIONS,
            },
        },
    )
    def test_session_credentials_never_go_to_storage(self):
        self.fake_posthog()
        self.message.send()
        uploads = self.calls(STORAGE_URL)
        self.assertEqual(len(uploads), 2)
        for upload in uploads:
            self.assertNotIn("Authorization", upload["headers"])
            self.assertNotIn("session-wide-credential", json.dumps(upload["headers"]))

    def test_netrc_credentials_never_go_to_storage(self):
        self.fake_posthog()
        with tempfile.TemporaryDirectory() as home:
            netrc = os.path.join(home, ".netrc")
            with open(netrc, "w") as file:
                file.write(
                    "machine attachments.s3.us-east-1.amazonaws.com"
                    " login user password netrc-password\n"
                )
            with patch.dict(os.environ, {"NETRC": netrc}):
                self.message.send()
        uploads = self.calls(STORAGE_URL)
        self.assertEqual(len(uploads), 2)
        for upload in uploads:
            self.assertNotIn("Authorization", upload["headers"])

    def test_esp_extra_attachment_reference_sends_without_upload(self):
        # A file uploaded earlier through the API can be named by hash alone.
        self.fake_posthog()
        self.message.attachments = []
        reference = {"sha256": sha256_hex(INVOICE), "filename": "invoice.pdf"}
        self.message.esp_extra = {"attachments": [reference]}
        self.message.send()
        self.assertEqual(self.calls(UPLOADS_URL), [])
        self.assertEqual(self.get_api_call_json()["attachments"], [reference])

    def test_esp_extra_null_attachments_send(self):
        self.fake_posthog()
        self.message.attachments = []
        self.message.esp_extra = {"attachments": None}
        self.message.send()
        self.assertEqual(self.calls(UPLOADS_URL), [])
        self.assertIsNone(self.get_api_call_json()["attachments"])

    def test_esp_extra_clicks_without_opens_refused_before_upload(self):
        self.fake_posthog()
        self.message.track_opens = False
        self.message.esp_extra = {"tracking": {"clicks": True}}
        with self.assertRaisesMessage(
            AnymailUnsupportedFeature, "track_clicks without track_opens"
        ):
            self.message.send()
        self.assertEqual(self.calls(UPLOADS_URL), [])
        self.assertEqual(self.calls(STORAGE_URL), [])
        self.assert_not_sent()

    @override_settings(ANYMAIL_IGNORE_UNSUPPORTED_FEATURES=True)
    def test_ignored_esp_extra_clicks_without_opens_send_opens_off(self):
        self.fake_posthog()
        self.message.track_opens = False
        self.message.esp_extra = {"tracking": {"clicks": True}}
        self.message.send()
        self.assertEqual(self.get_api_call_json()["tracking"], {"opens": False})


@tag("posthog")
@override_settings(
    MAILERS={
        "default": {
            "BACKEND": "anymail.backends.posthog.EmailBackend",
            "OPTIONS": {**POSTHOG_BASE_OPTIONS, "api_url": "http://localhost:8010"},
        },
    },
)
class PostHogLocalUploadTests(PostHogBackendMockAPITestCase):
    def test_plain_http_upload_url_allowed_for_plain_http_host(self):
        local_storage = "http://localhost:19000/attachments/"

        def request(session, method, url, **kwargs):
            if url.endswith("/attachments/uploads/"):
                slot = upload_slot({"sha256": sha256_hex(INVOICE), "size": 8})
                slot["url"] = local_storage + sha256_hex(INVOICE)
                return self.response(200, {"files": [slot]})
            return self.response(202, json.loads(self.DEFAULT_RAW_RESPONSE))

        self.mock_request.side_effect = request
        self.message.attach("invoice.pdf", INVOICE, "application/pdf")
        self.message.send()
        self.assertEqual(len(self.calls(local_storage)), 1)
        self.assertEqual(self.message.anymail_status.status, {"queued"})


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

    def test_202_with_unusable_message_id(self):
        for message_id in [None, "", ["0190f0c2"], {"id": "0190f0c2"}]:
            with self.subTest(message_id=message_id):
                self.assert_bad_success_response(json_data={"message_id": message_id})

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

    def test_answer_cut_off_or_late_is_retryable(self):
        # PostHog may have queued the email; a retry with the same key is safe.
        for fault in [
            requests.exceptions.ChunkedEncodingError("connection broken"),
            requests.ReadTimeout("read timed out"),
        ]:
            with self.subTest(fault=fault):
                self.mock_request.side_effect = fault
                with self.assertRaises(AnymailAPIError) as cm:
                    self.message.send()
                self.assertIsNone(cm.exception.status_code)
                self.assertIs(cm.exception.retryable, True)

    def test_request_that_cannot_go_out_is_not_retryable(self):
        self.patch_request.stop()
        self.message.esp_extra = {"idempotency_key": "order-981\nshipped"}
        with self.assertRaises(AnymailAPIError) as cm:
            self.message.send()
        self.assertIsNone(cm.exception.status_code)
        self.assertIs(cm.exception.retryable, False)

    @override_settings(
        MAILERS={
            "default": {
                "BACKEND": "anymail.backends.posthog.EmailBackend",
                "OPTIONS": {**POSTHOG_BASE_OPTIONS, "api_url": "us.posthog.com"},
            },
        },
    )
    def test_api_url_without_scheme_is_not_retryable(self):
        self.patch_request.stop()
        with self.assertRaises(AnymailAPIError) as cm:
            self.message.send()
        self.assertIs(cm.exception.retryable, False)

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
