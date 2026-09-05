"""
LANEON PoC — 6시간 Report 이메일 발송 (Gmail SMTP + TLS)

Credential(App Password 등)은 소스코드/Git/JSON 설정파일에 절대 넣지 않고,
권한이 제한된 secret 파일(/home/jb/.config/laneon/report.env, chmod 600) 또는
환경변수에서만 읽는다. 이 모듈은 env_reporter.py(운영 프로세스와 분리된 별도
실행)에서만 호출되며, 실패해도 DB 저장/Fusion/MANUAL/AUTO 제어에는 영향이 없다.

secret 파일 형식 (KEY=VALUE, 한 줄에 하나):
    GMAIL_SMTP_USER=het120615@gmail.com
    GMAIL_APP_PASSWORD=xxxx xxxx xxxx xxxx
    REPORT_EMAIL_TO=eyby@naver.com
"""
import os
import smtplib
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

DEFAULT_ENV_FILE = '/home/jb/.config/laneon/report.env'
SMTP_HOST = 'smtp.gmail.com'
SMTP_PORT = 587


def _load_env_file(path: str) -> dict:
    creds = {}
    try:
        with open(path, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith('#') or '=' not in line:
                    continue
                k, v = line.split('=', 1)
                creds[k.strip()] = v.strip().strip('"').strip("'")
    except FileNotFoundError:
        pass
    except Exception as e:
        print(f'[report_mailer] secret 파일 읽기 실패({path}): {e}')
    return creds


def _get_config() -> dict:
    env_file = os.environ.get('LANEON_REPORT_ENV_FILE', DEFAULT_ENV_FILE)
    file_creds = _load_env_file(env_file)

    def pick(key):
        return os.environ.get(key) or file_creds.get(key)

    return {
        'user':     pick('GMAIL_SMTP_USER'),
        'password': pick('GMAIL_APP_PASSWORD'),
        'to':       pick('REPORT_EMAIL_TO'),
    }


def send_report(subject: str, summary_text: str, report_md: str, report_path: str) -> bool:
    """성공하면 True, 실패해도 예외를 던지지 않고 False를 반환한다."""
    cfg = _get_config()
    if not cfg['user'] or not cfg['password'] or not cfg['to']:
        print(f"[report_mailer] SMTP credential 미설정({DEFAULT_ENV_FILE} 또는 환경변수) "
              f"— 이메일 발송 생략. Report 파일은 이미 저장되어 있음: {report_path}")
        return False

    msg = MIMEMultipart()
    msg['From'] = cfg['user']
    msg['To'] = cfg['to']
    msg['Subject'] = subject

    body = summary_text + '\n\n' + ('=' * 60) + '\n\n' + report_md
    msg.attach(MIMEText(body, 'plain', 'utf-8'))

    try:
        with open(report_path, 'rb') as f:
            part = MIMEApplication(f.read(), _subtype='markdown')
            part.add_header('Content-Disposition', 'attachment',
                             filename=os.path.basename(report_path))
            msg.attach(part)
    except Exception as e:
        print(f'[report_mailer] 첨부파일 준비 실패(본문만 발송 시도): {e}')

    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=15) as server:
            server.starttls()
            server.login(cfg['user'], cfg['password'])
            server.sendmail(cfg['user'], [cfg['to']], msg.as_string())
        print(f"[report_mailer] 이메일 발송 성공 -> {cfg['to']}")
        return True
    except Exception as e:
        print(f'[report_mailer] 이메일 발송 실패: {e}')
        return False
