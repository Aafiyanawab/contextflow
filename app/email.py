"""Transactional email — the SMTP seam.

Mirrors app/storage.py's env-driven backend selection:
  SMTP_HOST set    -> send via the standard library's smtplib (STARTTLS)
  otherwise (dev)  -> log the message (link included) to the app logger and
                      report "not sent", so callers can surface the link
                      locally for testing.

No new dependency: stdlib smtplib + email.message.
"""
import os
import smtplib
import ssl
from email.message import EmailMessage

from flask import current_app


def _smtp_configured():
    return bool(os.getenv("SMTP_HOST"))


def send_email(to, subject, body):
    """Send a plain-text email. → True if actually sent (prod), False in dev
    (logged instead). Raises only on a real SMTP failure."""
    if not _smtp_configured():
        current_app.logger.info(
            "[email:dev] SMTP not configured — would send to %s\n"
            "Subject: %s\n%s", to, subject, body)
        return False

    msg = EmailMessage()
    msg["From"] = os.getenv("SMTP_FROM") or os.getenv("SMTP_USER")
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(body)

    host = os.getenv("SMTP_HOST")
    port = int(os.getenv("SMTP_PORT", "587"))
    user = os.getenv("SMTP_USER")
    password = os.getenv("SMTP_PASSWORD")
    with smtplib.SMTP(host, port, timeout=15) as server:
        server.starttls(context=ssl.create_default_context())
        if user:
            server.login(user, password)
        server.send_message(msg)
    return True


def send_password_reset_email(to, reset_link):
    """The reset email: the link + the required security notice (expires in
    5 minutes, single-use, safe to ignore if not requested). → True if sent
    (prod), False in dev (logged)."""
    subject = "Reset your ContextFlow password"
    body = (
        "We received a request to reset your ContextFlow password.\n\n"
        f"Reset it using the link below:\n{reset_link}\n\n"
        "This link expires in 5 minutes and can only be used once.\n\n"
        "If you didn't request a password reset, you can safely ignore this "
        "email — no changes have been made to your account.\n"
    )
    return send_email(to, subject, body)
