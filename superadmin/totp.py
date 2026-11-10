"""Одноразовые коды из приложения-аутентификатора (TOTP, RFC 6238) и резервные коды.

Совместимо с Google Authenticator, Microsoft Authenticator, 1Password и т.п.: SHA-1, 6 цифр, шаг 30 секунд.
"""
import base64
import hashlib
import hmac
import secrets
import struct
import time
from urllib.parse import quote

DIGITS = 6
STEP = 30
# Допуск ±1 шаг: часы телефона и сервера расходятся на несколько секунд
WINDOW = 1
ISSUER = 'TimePoint'
BACKUP_CODES = 10


def new_secret():
    """Случайный секрет 160 бит в base32 (без '=' — так его вводят вручную)."""
    return base64.b32encode(secrets.token_bytes(20)).decode().rstrip('=')


def _key(secret):
    padded = secret.upper() + '=' * (-len(secret) % 8)
    return base64.b32decode(padded)


def code_at(secret, step):
    digest = hmac.new(_key(secret), struct.pack('>Q', step), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    number = struct.unpack('>I', digest[offset:offset + 4])[0] & 0x7FFFFFFF
    return str(number % 10 ** DIGITS).zfill(DIGITS)


def current_step(now=None):
    return int((time.time() if now is None else now) // STEP)


def match_step(secret, code, now=None):
    """Шаг, которому соответствует код (с допуском WINDOW), или None."""
    code = ''.join(ch for ch in str(code or '') if ch.isdigit())
    if len(code) != DIGITS:
        return None
    step = current_step(now)
    for candidate in range(step - WINDOW, step + WINDOW + 1):
        if hmac.compare_digest(code_at(secret, candidate), code):
            return candidate
    return None


def otpauth_url(secret, account):
    """Ссылка для QR-кода: приложение само заполнит название и секрет."""
    label = quote(f'{ISSUER}:{account}')
    return f'otpauth://totp/{label}?secret={secret}&issuer={quote(ISSUER)}&digits={DIGITS}&period={STEP}'


def new_backup_codes():
    """10 кодов вида 'abcd-efgh' (буквы и цифры без похожих 0/o, 1/l)."""
    alphabet = 'abcdefghjkmnpqrstuvwxyz23456789'
    codes = []
    for _ in range(BACKUP_CODES):
        raw = ''.join(secrets.choice(alphabet) for _ in range(8))
        codes.append(f'{raw[:4]}-{raw[4:]}')
    return codes


def normalize_backup(code):
    return ''.join(ch for ch in str(code or '').lower() if ch.isalnum())


def hash_backup(code):
    """Резервные коды храним только хешем: 40 бит случайности + SHA-256 с солью из SECRET_KEY."""
    from django.utils.crypto import salted_hmac
    return salted_hmac('superadmin-backup', normalize_backup(code)).hexdigest()
