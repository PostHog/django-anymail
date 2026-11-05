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
    }

``template_key`` takes letters, numbers, hyphens and underscores, up to 400 characters.
To use one key for every message, set it in Anymail's
:ref:`global send defaults <send-defaults>`:

.. code-block:: python

    ANYMAIL = {
        ...
        "POSTHOG_SEND_DEFAULTS": {
            "esp_extra": {"template_key": "transactional"},
        },
    }

Any other ``esp_extra`` keys are merged into the JSON that Anymail sends to PostHog's
API, so you can use API fields Anymail doesn't know about yet.


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

**One engagement setting**
  PostHog captures opens and clicks together, with its ``capture_engagement`` option.
  :attr:`~anymail.message.AnymailMessage.track_opens` and
  :attr:`~anymail.message.AnymailMessage.track_clicks` set it when only one is set or
  both agree; different values are unsupported.

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

A template send leaves out ``template_key``, ``capture_engagement``,
:attr:`~anymail.message.AnymailMessage.track_opens` and
:attr:`~anymail.message.AnymailMessage.track_clicks`, so defaults you set for rendered
sends don't break template sends.

PostHog doesn't support batch sending, so
:attr:`~anymail.message.AnymailMessage.merge_data` is unsupported.


.. _posthog-errors:

Errors and retries
------------------

Anymail doesn't retry a failed send. An :exc:`~anymail.exceptions.AnymailAPIError`
from PostHog carries PostHog's ``code``, ``detail`` and ``attr`` (the field at fault),
plus ``retryable`` and ``retry_after``:

* ``retryable`` is ``True`` when the same send may be retried *with the same*
  ``idempotency_key``: a network error or timeout, a 503, a 409 (the same key is still
  being sent), or a 429 for throttling.
* ``retry_after`` is the number of seconds PostHog asked you to wait, for a throttled
  429.

A 503 or a network error can still deliver the email. Retrying without the same
``idempotency_key`` may send it twice. If you send through a task queue that retries,
set ``idempotency_key`` from a stable ID, such as the order ID plus the email's name.

A success response without a ``message_id`` raises an
:exc:`~anymail.exceptions.AnymailAPIError` that is not ``retryable``.

On success, :attr:`~anymail.message.AnymailMessage.anymail_status` has status
``"queued"`` and PostHog's ``message_id`` for every recipient PostHog was sent. With
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
