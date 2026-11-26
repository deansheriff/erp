"""SMTP delivery. The only module that opens network connections to a mail server."""

import smtplib
import ssl
from dataclasses import dataclass
from email.message import EmailMessage
from email.utils import formataddr, make_msgid

TIMEOUT_SECONDS = 10


@dataclass(frozen=True, slots=True)
class SmtpConfig:
    host: str
    port: int
    security: str  # starttls | ssl | none
    username: str | None
    password: str | None
    from_name: str
    from_address: str
    reply_to: str | None


@dataclass(frozen=True, slots=True)
class Attachment:
    filename: str
    content_type: str  # e.g. application/pdf
    data: bytes


@dataclass(frozen=True, slots=True)
class Message:
    to: str
    subject: str
    text: str
    html: str
    attachment: Attachment | None = None


class SendFailed(Exception):
    """Delivery failed. `category` is safe to store and show; details are never kept."""

    def __init__(self, category: str) -> None:
        super().__init__(category)
        self.category = category


def _build(config: SmtpConfig, message: Message) -> EmailMessage:
    email = EmailMessage()
    email["From"] = formataddr((config.from_name, config.from_address))
    email["To"] = message.to
    email["Subject"] = message.subject
    email["Message-ID"] = make_msgid(domain=config.from_address.rsplit("@", 1)[-1])
    if config.reply_to:
        email["Reply-To"] = config.reply_to
    email.set_content(message.text)
    email.add_alternative(message.html, subtype="html")
    if message.attachment is not None:
        main, sub = message.attachment.content_type.split("/", 1)
        email.add_attachment(
            message.attachment.data,
            maintype=main,
            subtype=sub,
            filename=message.attachment.filename,
        )
    return email


def send(config: SmtpConfig, message: Message) -> None:
    context = ssl.create_default_context()
    try:
        smtp: smtplib.SMTP
        if config.security == "ssl":
            smtp = smtplib.SMTP_SSL(
                config.host, config.port, timeout=TIMEOUT_SECONDS, context=context
            )
        else:
            smtp = smtplib.SMTP(config.host, config.port, timeout=TIMEOUT_SECONDS)
        with smtp:
            if config.security == "starttls":
                smtp.starttls(context=context)
            if config.username:
                smtp.login(config.username, config.password or "")
            smtp.send_message(_build(config, message))
    except smtplib.SMTPAuthenticationError as exc:
        raise SendFailed("auth_failed") from exc
    except (smtplib.SMTPRecipientsRefused, smtplib.SMTPSenderRefused) as exc:
        raise SendFailed("rejected") from exc
    except (smtplib.SMTPException, OSError) as exc:
        raise SendFailed("connection_failed") from exc


FAILURE_MESSAGES = {
    "auth_failed": "The mail server rejected the username or password.",
    "rejected": "The mail server refused the sender or recipient address.",
    "connection_failed": "Couldn't connect to the mail server. Check the host, port and security.",
    "not_configured": "Mail isn't set up yet.",
}
