import re

from .._version import __version__
from ..exceptions import AnymailRequestsAPIError
from ..message import AnymailRecipientStatus
from ..utils import get_anymail_setting
from .base_requests import AnymailRequestsBackend, RequestsPayload

TEMPLATE_KEY_PATTERN = re.compile(r"^[a-zA-Z0-9_-]{1,400}$")

# Headers Django or a mail library adds on its own; PostHog writes its own.
IGNORED_HEADERS = {"message-id", "date", "mime-version", "x-mailer"}

# Rendered-form fields; PostHog's template form takes none of them.
CONTENT_FIELDS = (
    "from",
    "subject",
    "text",
    "html",
    "reply_to",
    "cc",
    "bcc",
    "attachments",
)


def is_ignored_header(name):
    name = name.lower()
    return name in IGNORED_HEADERS or name.startswith("content-")


class EmailBackend(AnymailRequestsBackend):
    """
    PostHog transactional email API Email Backend
    """

    esp_name = "PostHog"

    def __init__(self, **kwargs):
        """Init options from Django settings"""
        esp_name = self.esp_name
        self.secret_key = get_anymail_setting(
            "secret_key", esp_name=esp_name, kwargs=kwargs, allow_bare=True
        )
        api_url = get_anymail_setting(
            "api_url",
            esp_name=esp_name,
            kwargs=kwargs,
            default="https://us.posthog.com/",
        )
        if not api_url.endswith("/"):
            api_url += "/"
        super().__init__(api_url, **kwargs)

    def build_message_payload(self, message, defaults):
        return PostHogPayload(message, defaults, self)

    def post_to_esp(self, payload, message):
        try:
            return super().post_to_esp(payload, message)
        except AnymailRequestsAPIError as error:
            describe_api_error(error)
            raise

    def parse_recipient_status(self, response, payload, message):
        try:
            message_id = self.parse_message_id(response, payload, message)
        except AnymailRequestsAPIError as error:
            describe_api_error(error)
            raise
        status = AnymailRecipientStatus(message_id=message_id, status="queued")
        return {email.addr_spec: status for email in payload.recipients}

    def parse_message_id(self, response, payload, message):
        parsed_response = self.deserialize_json_response(response, payload, message)
        try:
            return parsed_response["message_id"]
        except (KeyError, TypeError) as err:
            raise AnymailRequestsAPIError(
                "Invalid PostHog API response format",
                email_message=message,
                payload=payload,
                response=response,
                backend=self,
            ) from err


def describe_api_error(error):
    """
    Add the PostHog error's code, detail and attr to an AnymailRequestsAPIError,
    and whether the same send may be retried with the same idempotency key.
    """
    response = error.response
    body = parse_error_body(response)
    error.code = body.get("code")
    error.detail = body.get("detail")
    error.attr = body.get("attr")
    error.retry_after = None
    if response is None:
        # A network error or timeout: the email may have gone out.
        error.retryable = True
    elif response.status_code == 429:
        error.retryable = error.code != "quota_limited"
        if error.retryable:
            error.retry_after = parse_retry_after(response)
    else:
        error.retryable = response.status_code in (409, 503)


def parse_error_body(response):
    try:
        body = response.json()
    except (AttributeError, ValueError):
        return {}
    return body if isinstance(body, dict) else {}


def parse_retry_after(response):
    try:
        return int(response.headers["Retry-After"])
    except (KeyError, ValueError):
        return None


class PostHogPayload(RequestsPayload):
    def __init__(self, message, defaults, backend, *args, **kwargs):
        self.recipients_by_field = {}
        self.from_email = None
        self.track_opens = None
        self.track_clicks = None
        headers = kwargs.pop("headers", {})
        headers["Authorization"] = f"Bearer {backend.secret_key}"
        headers["Content-Type"] = "application/json"
        headers["Accept"] = "application/json"
        headers["X-PostHog-Integration"] = f"django-anymail/{__version__}"
        super().__init__(message, defaults, backend, headers=headers, *args, **kwargs)

    @property
    def recipients(self):
        """The recipients the request carries, for parse_recipient_status"""
        return [
            email
            for field, emails in self.recipients_by_field.items()
            if field in self.data
            for email in emails
        ]

    def get_api_endpoint(self):
        return "api/projects/@current/messaging/send/email/"

    def serialize_data(self):
        if "template_id" in self.data:
            self.check_template_form()
        else:
            self.finish_rendered_form()
        return self.serialize_json(self.data)

    def check_template_form(self):
        found = [field for field in CONTENT_FIELDS if field in self.data]
        if found:
            self.unsupported_feature(
                "template_id with from_email, subject, body, reply_to, cc, bcc"
                f" or attachments (found {', '.join(found)})"
            )
            for field in found:
                del self.data[field]
        # Rendered-form options, maybe from send defaults; the template form
        # has no place for them.
        self.data.pop("template_key", None)
        self.data.pop("capture_engagement", None)

    def finish_rendered_form(self):
        if "message_data" in self.data:
            self.unsupported_feature("merge_global_data without template_id")
            del self.data["message_data"]
        capture_engagement = self.capture_engagement()
        if capture_engagement is not None:
            self.data.setdefault("capture_engagement", capture_engagement)
        self.data.setdefault("template_key", "django-anymail")

    def capture_engagement(self):
        tracking = {self.track_opens, self.track_clicks} - {None}
        if len(tracking) > 1:
            self.unsupported_feature(
                "track_opens and track_clicks set to different values"
            )
            return None
        return tracking.pop() if tracking else None

    #
    # Payload construction
    #

    def init_payload(self):
        self.data = {}  # becomes json

    def set_from_email(self, email):
        self.from_email = email
        self.data["from"] = email.format(idna_encode=self.backend.idna_encode)

    def bare_addresses(self, emails):
        return [
            email.format_addr_spec(idna_encode=self.backend.idna_encode)
            for email in emails
        ]

    def set_to(self, emails):
        if len(emails) != 1:
            self.unsupported_feature("more than one `to` address, or none")
        if emails:
            self.recipients_by_field["to"] = emails[:1]
            self.data["to"] = self.bare_addresses(emails[:1])[0]

    def set_recipients(self, recipient_type, emails):
        assert recipient_type in ["cc", "bcc"]
        if emails:
            self.recipients_by_field[recipient_type] = emails
            self.data[recipient_type] = self.bare_addresses(emails)

    def set_subject(self, subject):
        if subject:
            self.data["subject"] = subject

    def set_reply_to(self, emails):
        if emails:
            self.data["reply_to"] = self.bare_addresses(emails)

    def set_extra_headers(self, headers):
        for name in headers:
            if not is_ignored_header(name):
                self.unsupported_feature(f"custom header {name!r}")

    def set_text_body(self, body):
        if body:
            self.data["text"] = body

    def set_html_body(self, body):
        if body:
            self.data["html"] = body

    def make_attachment(self, attachment):
        # PostHog takes a bare MIME type, so text goes as UTF-8.
        att = {
            "filename": attachment.name,
            "content_type": attachment.mimetype,
            "content": attachment.b64content_utf8,
        }
        if attachment.inline:
            if not attachment.cid:
                self.unsupported_feature("inline attachments without Content-ID")
            else:
                att["content_id"] = attachment.cid
        return att

    def set_attachments(self, attachments):
        if attachments:
            self.data["attachments"] = [
                self.make_attachment(attachment) for attachment in attachments
            ]

    def set_envelope_sender(self, email):
        if self.from_email is None or email.addr_spec != self.from_email.addr_spec:
            self.unsupported_feature(
                "envelope_sender other than the from_email address"
            )

    def set_track_opens(self, track_opens):
        self.track_opens = track_opens

    def set_track_clicks(self, track_clicks):
        self.track_clicks = track_clicks

    def set_template_id(self, template_id):
        self.data["template_id"] = template_id

    def set_merge_global_data(self, merge_global_data):
        self.data["message_data"] = merge_global_data

    def set_esp_extra(self, extra):
        extra = dict(extra)
        idempotency_key = extra.pop("idempotency_key", None)
        if idempotency_key is not None:
            self.headers["Idempotency-Key"] = idempotency_key
        template_key = extra.get("template_key")
        if template_key is not None and not TEMPLATE_KEY_PATTERN.match(template_key):
            self.unsupported_feature(
                f"template_key {template_key!r} (use letters, numbers,"
                " hyphens and underscores, at most 400)"
            )
            del extra["template_key"]
        self.data.update(extra)
