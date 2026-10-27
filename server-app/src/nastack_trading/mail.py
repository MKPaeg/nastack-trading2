import smtplib
import ssl
from email.message import EmailMessage


def send_login_code(config, recipient, code, minutes):
    return send_email_message(
        config,
        [recipient],
        "NaStack Trading 로그인 인증 코드",
        f"로그인 인증 코드: {code}\n유효 시간: {minutes}분\n본인이 요청하지 않았다면 무시하세요. 코드를 다른 사람에게 전달하지 마세요.",
    )


def send_email_message(config, recipients, subject, body):
    if not config.smtp_host or not config.smtp_from_email:
        raise ValueError("SMTP_NOT_CONFIGURED")
    smtp = smtplib.SMTP_SSL if config.smtp_use_ssl else smtplib.SMTP
    kwargs = {"context": ssl.create_default_context()} if config.smtp_use_ssl else {}
    with smtp(config.smtp_host, config.smtp_port, timeout=15, **kwargs) as server:
        if config.smtp_use_tls and not config.smtp_use_ssl:
            server.starttls(context=ssl.create_default_context())
        if config.smtp_username:
            server.login(config.smtp_username, config.smtp_password.get_secret_value())
        for recipient in recipients:
            message = EmailMessage()
            message["Subject"] = subject
            message["From"] = config.smtp_from_email
            message["To"] = recipient
            message.set_content(body)
            if server.send_message(message):
                raise ValueError("SMTP_RECIPIENT_REJECTED")
    return len(recipients)
