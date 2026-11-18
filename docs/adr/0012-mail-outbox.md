# 0012. Mail through an outbox, sent by a worker

- Status: Accepted
- Date: 2026-11-17

## Context

Invitations and password resets must not be lost if the SMTP server is slow or down, and
must not be sent if the transaction that created them rolls back.

## Decision

- Code never calls SMTP from a request, except the explicit "Send test email" action.
- `mail.service.queue(session, to, template, context)` renders the message and inserts a
  `mail_outbox` row in the caller's transaction. After commit, a Celery task
  `mail.send_pending` is enqueued; a periodic sweep (every minute) also picks up pending
  rows, so a broker outage only delays mail.
- The sender takes rows with `SELECT … FOR UPDATE SKIP LOCKED`, sends with `smtplib`
  (STARTTLS, implicit TLS, or plain only for local relays), and records `sent`, or
  `failed` with an error *category* (`auth_failed`, `connection_failed`, `rejected`)
  after 5 attempts with backoff.
- Email bodies are rendered from Jinja templates in `app/mail/templates/` (plain text and
  HTML). Links in emails contain the token only in the path, never other personal data.

## Consequences

Mail delivery is at-least-once; a duplicate is possible only if the worker dies between
SMTP acceptance and marking the row sent.
