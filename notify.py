#!/usr/bin/env python3
"""Envoi de notification Gmail pour le sync MPG."""

import smtplib
import sys
from email.mime.text import MIMEText

from dotenv import load_dotenv
import os

load_dotenv()


def send(subject: str, body: str) -> None:
    gmail = os.environ["GMAIL_FROM"]
    password = os.environ["GMAIL_APP_PASSWORD"]
    to = os.environ["GMAIL_TO"]

    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = gmail
    msg["To"] = to

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
        smtp.login(gmail, password)
        smtp.send_message(msg)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: notify.py <subject> [body]")
        sys.exit(1)
    subject = sys.argv[1]
    body = sys.argv[2] if len(sys.argv) > 2 else ""
    send(subject, body)
