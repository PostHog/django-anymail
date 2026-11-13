import hashlib
import re
from dataclasses import dataclass
from urllib.parse import urljoin

import requests

from .._version import __version__
from ..exceptions import AnymailRequestsAPIError
from ..message import AnymailRecipientStatus
from ..utils import get_anymail_setting, update_deep
from .base_requests import AnymailRequestsBackend, RequestsPayload

TEMPLATE_KEY_MAX_LENGTH = 256
TEMPLATE_KEY_PATTERN = re.compile(rf"[a-zA-Z0-9_-]{{1,{TEMPLATE_KEY_MAX_LENGTH}}}")

SEND_ENDPOINT = "api/projects/@current/messaging/send/email/"
UPLOADS_ENDPOINT = "api/projects/@current/messaging/attachments/uploads/"

NOT_RETRYABLE_429_CODES = {"quota_limited", "upload_quota_exceeded"}
STORAGE_RETRY_STATUSES = {429, 500, 502, 503, 504}
STORAGE_ERROR_MESSAGE = re.compile(r"<Message>([^<]{1,500})</Message>")

AUTOMATIC_HEADERS = {"message-id", "date", "mime-version", "x-mailer"}

RENDERED_CONTENT_FIELDS = (
    "from",
    "subject",
    "text",
    "html",
    "reply_to",
    "cc",
    "bcc",
    "attachments",
)

# PostHog refuses these on a template send only when they carry something.
COPY_AND_FILE_FIELDS = ("cc", "bcc", "attachments")

RENDERED_ONLY_OPTIONS = ("template_key", "capture_engagement", "tracking", "sensitive")


def is_automatic_header(name):
    name = name.lower()
    return name in AUTOMATIC_HEADERS or name.startswith("content-")


def is_valid_template_key(template_key):
    return isinstance(template_key, str) and bool(
        TEMPLATE_KEY_PATTERN.fullmatch(template_key)
    )


def is_message_id(value):
    return isinstance(value, str) and bool(value)


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
        files = payload.files_to_upload()
        if files:
            for upload in self.ask_where_to_upload(files, payload, message):
                self.upload(upload, payload, message)
        try:
            return super().post_to_esp(payload, message)
        except AnymailRequestsAPIError as error:
            describe_api_error(error)
            raise

    def ask_where_to_upload(self, files, payload, message):
        """
        POST each file's hash and size to PostHog's upload route, and return
        the files PostHog doesn't have yet with where to upload them.
        """
        headers = dict(payload.headers)
        headers.pop("Idempotency-Key", None)
        body = {"files": [{"sha256": file.sha256, "size": file.size} for file in files]}
        try:
            response = self.session.request(
                method="POST",
                url=urljoin(self.api_url, UPLOADS_ENDPOINT),
                data=payload.serialize_json(body),
                headers=headers,
                timeout=self.timeout,
            )
        except requests.RequestException as err:
            error = request_error(
                "Error asking PostHog where to upload",
                err,
                email_message=message,
                payload=payload,
                backend=self,
            )
            describe_api_error(error)
            raise error from err
        if not 200 <= response.status_code < 300:
            error = AnymailRequestsAPIError(
                email_message=message, payload=payload, response=response, backend=self
            )
            describe_api_error(error)
            if response.status_code == 404 and error.code is None:
                error.code = "not_found"
            error.attr = attachment_field(error.attr, files)
            raise error
        return self.parse_upload_slots(response, files, payload, message)

    def parse_upload_slots(self, response, files, payload, message):
        body = parse_error_body(response)
        slots = body.get("files")
        if not isinstance(slots, list) or len(slots) != len(files):
            raise self.invalid_upload_answer(response, payload, message)
        uploads = []
        for slot, file in zip(slots, files):
            if not isinstance(slot, dict) or slot.get("sha256") != file.sha256:
                raise self.invalid_upload_answer(response, payload, message)
            if slot.get("status") == "present":
                continue
            url, method, headers = (
                slot.get("url"),
                slot.get("method"),
                slot.get("headers"),
            )
            if not (
                slot.get("status") == "upload"
                and isinstance(url, str)
                and isinstance(method, str)
                and isinstance(headers, dict)
                and all(isinstance(value, str) for value in headers.values())
            ):
                raise self.invalid_upload_answer(response, payload, message)
            if not url.lower().startswith(
                "https://"
            ) and not self.api_url.lower().startswith("http://"):
                error = AnymailRequestsAPIError(
                    f"PostHog answered an upload URL that isn't https for"
                    f" attachment {file.attachment_index}, so Anymail uploaded nothing",
                    email_message=message,
                    payload=payload,
                    response=response,
                    backend=self,
                )
                describe_api_error(error)
                error.retryable = False
                error.attr = f"attachments__{file.attachment_index}"
                raise error
            uploads.append(
                PostHogUpload(file=file, url=url, method=method, headers=headers)
            )
        return uploads

    def invalid_upload_answer(self, response, payload, message):
        error = AnymailRequestsAPIError(
            "Invalid PostHog upload route response: no upload slot for each file",
            email_message=message,
            payload=payload,
            response=response,
            backend=self,
        )
        describe_api_error(error)
        error.retryable = False
        return error

    def upload(self, upload, payload, message):
        """
        Send a file's bytes to PostHog's file storage, with exactly the headers
        PostHog gave and without the secret key.
        """
        index = upload.file.attachment_index
        # A prepared request skips the session's headers, auth and .netrc
        # credentials, which are for PostHog's API, not for the storage host.
        try:
            request = requests.Request(
                method=upload.method,
                url=upload.url,
                data=upload.file.content,
                headers=upload.headers,
            ).prepare()
        except requests.RequestException as err:
            error = request_error(
                f"PostHog answered an upload URL for attachment {index}"
                " that Anymail can't send to, so Anymail sent no email",
                err,
                email_message=message,
                payload=payload,
                backend=self,
            )
            describe_storage_error(error, index)
            # The same PostHog would answer the same URL again.
            error.retryable = False
            raise error from err
        transport = self.session.merge_environment_settings(
            request.url, proxies={}, stream=False, verify=None, cert=None
        )
        try:
            response = self.session.send(
                request, timeout=self.timeout, allow_redirects=False, **transport
            )
        except requests.RequestException as err:
            error = request_error(
                f"Error uploading attachment {index} to PostHog's file storage",
                err,
                email_message=message,
                payload=payload,
                backend=self,
            )
            describe_storage_error(error, index)
            raise error from err
        if not 200 <= response.status_code < 300:
            error = AnymailRequestsAPIError(
                f"PostHog's file storage refused attachment {index}, so Anymail sent no email",
                email_message=message,
                payload=payload,
                response=response,
                backend=self,
            )
            describe_storage_error(error, index)
            raise error

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
        message_id = (
            parsed_response.get("message_id")
            if isinstance(parsed_response, dict)
            else None
        )
        if not is_message_id(message_id):
            raise AnymailRequestsAPIError(
                "Invalid PostHog API response format",
                email_message=message,
                payload=payload,
                response=response,
                backend=self,
            )
        return message_id


@dataclass(frozen=True)
class PostHogFile:
    sha256: str
    content: bytes
    attachment_index: int

    @property
    def size(self):
        return len(self.content)


@dataclass(frozen=True)
class PostHogUpload:
    file: PostHogFile
    url: str
    method: str
    headers: dict


def request_error(description, err, **kwargs):
    """An error that is both AnymailRequestsAPIError and err's requests type"""
    error_class = type(
        "AnymailRequestsAPIError", (AnymailRequestsAPIError, type(err)), {}
    )
    return error_class(description, **kwargs)


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
        error.retryable = True
    elif response.status_code == 429:
        error.retryable = error.code not in NOT_RETRYABLE_429_CODES
        if error.retryable:
            error.retry_after = parse_retry_after(response)
    else:
        error.retryable = response.status_code in (409, 503)


def describe_storage_error(error, attachment_index):
    """
    Describe a failed upload to PostHog's file storage. A retry of the send asks
    for a fresh upload URL, so a passing fault is retryable.
    """
    response = error.response
    error.code = "attachment_upload_failed"
    error.attr = f"attachments__{attachment_index}"
    error.detail = None
    error.retry_after = None
    if response is None:
        error.retryable = True
        return
    found = STORAGE_ERROR_MESSAGE.search(response.text or "")
    error.detail = found.group(1) if found else None
    error.retryable = response.status_code in STORAGE_RETRY_STATUSES
    if error.retryable:
        error.retry_after = parse_retry_after(response)


def attachment_field(attr, files):
    """Name the attachment behind the upload route's files__<n> attr"""
    if isinstance(attr, str) and attr.startswith("files__"):
        index = attr.split("__")[1]
        if index.isdigit() and int(index) < len(files):
            return f"attachments__{files[int(index)].attachment_index}"
    return "attachments"


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
        self.files_by_sha256 = {}
        headers = kwargs.pop("headers", {})
        headers["Authorization"] = f"Bearer {backend.secret_key}"
        headers["Content-Type"] = "application/json"
        headers["Accept"] = "application/json"
        headers["X-PostHog-Integration"] = f"django-anymail/{__version__}"
        super().__init__(message, defaults, backend, headers=headers, *args, **kwargs)
        if "template_id" in self.data:
            self.check_template_form()
        else:
            self.finish_rendered_form()

    @property
    def recipients(self):
        """The recipients the request carries, for parse_recipient_status"""
        return [
            email
            for field, emails in self.recipients_by_field.items()
            if field in self.data
            for email in emails
        ]

    def files_to_upload(self):
        """Each file the request names, once, in the order it first appears"""
        attachments = self.data.get("attachments")
        sha256s = dict.fromkeys(
            att.get("sha256")
            for att in (attachments if isinstance(attachments, list) else [])
            if isinstance(att, dict)
        )
        # A reference from esp_extra names a file uploaded earlier, so it has
        # no bytes here to upload.
        return [
            self.files_by_sha256[sha256]
            for sha256 in sha256s
            if sha256 in self.files_by_sha256
        ]

    def get_api_endpoint(self):
        return SEND_ENDPOINT

    def serialize_data(self):
        return self.serialize_json(self.data)

    def check_template_form(self):
        found = [
            field
            for field in RENDERED_CONTENT_FIELDS
            if (
                self.data.get(field)
                if field in COPY_AND_FILE_FIELDS
                else field in self.data
            )
        ]
        if found:
            self.unsupported_feature(
                "template_id with from_email, subject, body, reply_to, cc, bcc"
                f" or attachments (found {', '.join(found)})"
            )
            for field in found:
                del self.data[field]
        for option in RENDERED_ONLY_OPTIONS:
            self.data.pop(option, None)

    def finish_rendered_form(self):
        if "message_data" in self.data:
            self.unsupported_feature("merge_global_data without template_id")
            del self.data["message_data"]
        self.finish_tracking()
        self.data.setdefault("template_key", "django-anymail")

    def finish_tracking(self):
        """
        Check tracking after esp_extra merged into it, and keep
        capture_engagement from travelling beside it, which PostHog refuses.
        """
        tracking = self.data.get("tracking")
        if "tracking" in self.data and "capture_engagement" in self.data:
            capture_engagement = self.data.pop("capture_engagement")
            # A boolean tracking already sets engagement.
            if isinstance(tracking, dict):
                tracking.setdefault("engagement", capture_engagement)
        if (
            isinstance(tracking, dict)
            and tracking.get("clicks") is True
            and tracking.get("opens") is False
        ):
            self.unsupported_feature("track_clicks without track_opens")
            del tracking["clicks"]

    def init_payload(self):
        self.data = {}

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
            if not is_automatic_header(name):
                self.unsupported_feature(f"custom header {name!r}")

    def set_text_body(self, body):
        if body:
            self.data["text"] = body

    def set_html_body(self, body):
        if body:
            self.data["html"] = body

    def make_attachment(self, index, attachment):
        content = attachment.content
        if isinstance(content, str):
            content = content.encode("utf-8")
        sha256 = hashlib.sha256(content).hexdigest()
        self.files_by_sha256.setdefault(sha256, PostHogFile(sha256, content, index))
        att = {
            "sha256": sha256,
            "filename": attachment.name,
            "content_type": attachment.mimetype,
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
                self.make_attachment(index, attachment)
                for index, attachment in enumerate(attachments)
            ]

    def set_envelope_sender(self, email):
        if self.from_email is None or email.addr_spec != self.from_email.addr_spec:
            self.unsupported_feature(
                "envelope_sender other than the from_email address"
            )

    def set_track_opens(self, track_opens):
        self.data.setdefault("tracking", {})["opens"] = track_opens

    def set_track_clicks(self, track_clicks):
        self.data.setdefault("tracking", {})["clicks"] = track_clicks

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
        if template_key is not None and not is_valid_template_key(template_key):
            self.unsupported_feature(
                f"template_key {template_key!r} (use letters, numbers,"
                f" hyphens and underscores, at most {TEMPLATE_KEY_MAX_LENGTH})"
            )
            del extra["template_key"]
        update_deep(self.data, extra)
