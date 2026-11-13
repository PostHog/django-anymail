.. _posthog-backend:

PostHog
=======

.. versionadded:: vNext

Anymail integrates with `PostHog`_ transactional email using PostHog's
`send email API <PostHog send API_>`_.

.. _PostHog: https://posthog.com/
.. _PostHog send API:
    https://posthog.com/docs/workflows/transactional-emails/api


Settings
--------

.. rubric:: EMAIL_BACKEND

To use Anymail's PostHog backend, set:

.. code-block:: python

    EMAIL_BACKEND = "anymail.backends.posthog.EmailBackend"

in your settings.py.


.. setting:: ANYMAIL_POSTHOG_SECRET_KEY

.. rubric:: POSTHOG_SECRET_KEY

Required. A PostHog *project secret API key* (it starts with ``phs_``) that has the
``transactional_email:write`` scope. (Not the ``phc_`` project token used to capture
events.)

.. code-block:: python

    ANYMAIL = {
        ...
        "POSTHOG_SECRET_KEY": "<your project secret API key>",
    }

Anymail will also look for ``POSTHOG_SECRET_KEY`` at the root of the settings file
if neither ``ANYMAIL["POSTHOG_SECRET_KEY"]`` nor ``ANYMAIL_POSTHOG_SECRET_KEY``
is set.


.. setting:: ANYMAIL_POSTHOG_API_URL

.. rubric:: POSTHOG_API_URL

The PostHog app host to call. The default is ``"https://us.posthog.com/"``
(PostHog US Cloud). For PostHog EU Cloud, use:

.. code-block:: python

    ANYMAIL = {
        ...
        "POSTHOG_API_URL": "https://eu.posthog.com/",
    }

A self-hosted PostHog uses its own app host. Use the app host, not an ingestion
host such as ``us.i.posthog.com``.


.. _posthog-esp-extra:

esp_extra support
-----------------

PostHog's send options ride in the message's
:attr:`~anymail.message.AnymailMessage.esp_extra`:

.. code-block:: python

    message.esp_extra = {
        # Attribute the send and its engagement events to this person
        # (the default is the recipient's email address):
        "distinct_id": "user_42",
        # Group this email's metrics and logs in PostHog
        # (the default is "django-anymail"):
        "template_key": "order-shipped",
        # Make retries safe; sent as the Idempotency-Key header:
        "idempotency_key": "order-981-shipped",
        # Mark an email that carries login links, reset tokens or codes:
        "sensitive": True,
    }

``template_key`` takes letters, numbers, hyphens and underscores, up to 256 characters.
To use one key for every message, set it in Anymail's
:ref:`global send defaults <send-defaults>`:

.. code-block:: python

    ANYMAIL = {
        ...
        "POSTHOG_SEND_DEFAULTS": {
            "esp_extra": {"template_key": "transactional"},
        },
    }

A ``sensitive`` email keeps its links as they are unless
:attr:`~anymail.message.AnymailMessage.track_clicks` is ``True``, and PostHog leaves its
subject off engagement events and its content out of logs. Without ``sensitive``,
PostHog uses its own default.

Any other ``esp_extra`` keys are merged into the JSON that Anymail sends to PostHog's
API, so you can use API fields Anymail doesn't know about yet. A dict merges into the
field Anymail builds, so ``{"tracking": {"engagement": False}}`` turns off PostHog's
engagement events and keeps ``track_opens`` and ``track_clicks``.


.. _posthog-quirks:

Limitations and quirks
----------------------

PostHog's send API carries one email to one recipient. Anymail raises an
:exc:`~anymail.exceptions.AnymailUnsupportedFeature` error, before calling the API,
for any part of a message the API can't carry. You can tell Anymail to drop those
parts and send anyway---see :ref:`unsupported-features`.

**Exactly one** ``to`` **address**
  A message must have exactly one ``to`` address. Use ``cc`` and ``bcc`` for copies,
  or send each recipient their own message.

**Recipient display names are dropped**
  PostHog takes bare addresses for ``to``, ``cc``, ``bcc`` and ``reply_to``, so
  Anymail sends ``Ana <ana@example.com>`` as ``ana@example.com``. The display name
  on ``from_email`` is kept.

**No custom headers**
  PostHog doesn't send custom headers, so any :attr:`~django.core.mail.EmailMessage.extra_headers`
  are unsupported. (``Message-ID``, ``Date``, ``MIME-Version``, ``Content-*`` and
  ``X-Mailer`` are ignored, because PostHog writes its own.)

**No tags, metadata, or delayed sending**
  PostHog's API does not yet support :attr:`~anymail.message.AnymailMessage.tags`,
  :attr:`~anymail.message.AnymailMessage.metadata`, or
  :attr:`~anymail.message.AnymailMessage.send_at`.

**Click tracking needs open tracking**
  :attr:`~anymail.message.AnymailMessage.track_opens` and
  :attr:`~anymail.message.AnymailMessage.track_clicks` set PostHog's
  ``tracking.opens`` and ``tracking.clicks``. A message that leaves one unset gets
  PostHog's default for it: opens are tracked, and clicks are tracked unless the email
  is ``sensitive``. ``track_clicks=True`` with ``track_opens=False`` is unsupported,
  because PostHog counts clicks only on emails it tracks opens for.

**Attachments upload first**
  PostHog takes an email's files before the email. For a message with attachments,
  Anymail sends each file's SHA-256 and size to PostHog's upload route, uploads the
  files PostHog doesn't have yet to the URLs it answers, then sends the email naming
  each file by its SHA-256. The same bytes in two attachments upload once, and a file
  your project uploaded recently uploads nothing. The upload goes to Amazon S3, not to
  your PostHog host: if your servers limit outgoing traffic, allow HTTPS to the
  upload host, a host that ends in ``.s3.us-east-1.amazonaws.com`` for US Cloud or
  ``.s3.eu-central-1.amazonaws.com`` for EU Cloud. Anymail never sends your secret key
  to the upload host, and refuses an upload URL that isn't https unless
  ``POSTHOG_API_URL`` is plain http too, as in local
  development. A failed upload sends no email. PostHog needs a name for each file,
  so an attachment without one, such as an inline image attached without a filename,
  goes as ``attachment`` with the extension of its type, such as ``attachment.png``.

**Attachment types have no parameters**
  PostHog takes an attachment's MIME type without parameters such as ``charset``,
  so Anymail sends text attachments encoded as UTF-8.

**Inline images need a Content-ID**
  Inline attachments are sent with their Content-ID. An inline attachment without one
  is unsupported.

**No envelope sender overrides**
  :attr:`~anymail.message.AnymailMessage.envelope_sender` is supported only when it is
  the ``from_email`` address.

**No AMP or other alternative parts**
  A message has at most one plain text and one HTML body.

**No merge features**
  PostHog does not support batch sending, so
  :attr:`~anymail.message.AnymailMessage.merge_data`,
  :attr:`~anymail.message.AnymailMessage.merge_headers`, and
  :attr:`~anymail.message.AnymailMessage.merge_metadata` are unsupported.

**Limits are PostHog's**
  Anymail leaves sizes and counts (such as the number of ``cc`` addresses and the
  total size of attachments) to PostHog, which answers with an API error.

**Without** ``from_email``
  Set ``message.from_email = None`` to send from the PostHog project's default sender.


.. _posthog-templates:

Batch sending/merge and ESP templates
-------------------------------------

PostHog can send an email template stored in PostHog. Set the message's
:attr:`~anymail.message.AnymailMessage.template_id` to the template's ID, and its
:attr:`~anymail.message.AnymailMessage.merge_global_data` to the data for the
template's placeholders. The template must be made transactional in PostHog first.

The template supplies the sender, subject and body, so a template message has no
``from_email``, subject, body, ``reply_to``, ``cc``, ``bcc`` or attachments. Because
Django fills in ``DEFAULT_FROM_EMAIL``, set ``from_email`` to ``None``:

.. code-block:: python

    message = AnymailMessage(
        to=["ana@example.com"],
        template_id="0190f0c2-7d1e-7a3b-9c2e-5f1a2b3c4d5e",  # PostHog template ID
        merge_global_data={"order": "981"},  # available as {{ variables.order }}
    )
    message.from_email = None  # use the template's sender
    message.send()

A template send leaves out ``template_key``, ``tracking``, ``sensitive``,
``capture_engagement``, :attr:`~anymail.message.AnymailMessage.track_opens` and
:attr:`~anymail.message.AnymailMessage.track_clicks`, because the template uses its
own settings. Defaults you set for rendered sends don't break template sends.

PostHog doesn't support batch sending, so
:attr:`~anymail.message.AnymailMessage.merge_data` is unsupported.


.. _posthog-errors:

Errors and retries
------------------

Anymail doesn't retry a failed send. An :exc:`~anymail.exceptions.AnymailAPIError`
from PostHog carries PostHog's ``code``, ``detail`` and ``attr`` (the field at fault),
plus ``retryable`` and ``retry_after``:

* ``retryable`` is ``True`` when the same send may be retried *with the same*
  ``idempotency_key``: a connection error, a timeout, an answer cut off, a 503, a 409
  (the same key is still being sent), or a 429 for throttling. A request Anymail can't
  build, such as one with a line break in ``idempotency_key``, is not.
* ``retry_after`` is the number of seconds PostHog asked you to wait, for a throttled
  429.

A message with attachments can also fail before the send, and then PostHog sends
nothing:

* ``code`` ``"attachment_upload_failed"``: PostHog's file storage refused an upload or
  didn't answer. ``attr`` names the attachment, such as ``"attachments__0"``, and
  ``detail`` carries the storage's reason. It is ``retryable`` when the storage was
  unreachable, answered a passing fault, or found the upload URL expired; a retry asks
  for a fresh upload URL.
* ``code`` ``"upload_quota_exceeded"`` (a 429): your project uploaded all the file bytes
  it can today. It is not ``retryable``; files PostHog already has don't count.
* Any other refusal of the upload route, such as ``"message_too_large"`` or
  ``"not_found"`` (this PostHog version or project can't take files), carries
  PostHog's ``code``, with ``attr`` naming the attachment when PostHog names a file.

A 503 or a network error can still deliver the email. Retrying without the same
``idempotency_key`` may send it twice. If you send through a task queue that retries,
set ``idempotency_key`` from a stable ID, such as the order ID plus the email's name.

A success response without a ``message_id`` raises an
:exc:`~anymail.exceptions.AnymailAPIError` that is not ``retryable``.

On success, :attr:`~anymail.message.AnymailMessage.anymail_status` has status
``"queued"`` and PostHog's ``message_id`` for every recipient PostHog was sent,
including recipients that ``esp_extra`` set. With
:setting:`ANYMAIL_IGNORE_UNSUPPORTED_FEATURES`, recipients Anymail left out
(extra ``to`` addresses, or ``cc`` and ``bcc`` on a template send) get no status.


.. _posthog-webhooks:

Status tracking webhooks
------------------------

Anymail doesn't yet support PostHog's delivery and engagement events. PostHog records
them as events in your project.


.. _posthog-inbound:

Inbound
-------

PostHog does not offer inbound email.
