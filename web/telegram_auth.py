"""Server-side validation for Telegram Mini App initData."""
import hashlib
import hmac
import json
import os
import time
from urllib.parse import parse_qsl


MAX_INIT_DATA_AGE_SECONDS = 24 * 60 * 60
MAX_FUTURE_SKEW_SECONDS = 60


def validate_init_data(init_data, bot_token=None, now=None):
    """Return the signed Telegram user object or raise ValueError.

    Validation follows Telegram's WebAppData HMAC scheme. The timestamp limit
    bounds replay of a captured payload; callers must still authorize the
    resulting Telegram ID against the server-side participant registry.
    """
    if not isinstance(init_data, str) or not init_data or len(init_data) > 16_384:
        raise ValueError('Некорректные данные Telegram Mini App')
    token = (bot_token if bot_token is not None else os.environ.get('TELEGRAM_TOKEN', '')).strip()
    if not token:
        raise RuntimeError('TELEGRAM_TOKEN is not configured')

    pairs = parse_qsl(init_data, keep_blank_values=True, strict_parsing=True)
    fields = {}
    for key, value in pairs:
        if key in fields:
            raise ValueError('Повторяющееся поле Telegram Mini App')
        fields[key] = value
    received_hash = fields.pop('hash', '')
    if len(received_hash) != 64:
        raise ValueError('Подпись Telegram Mini App отсутствует')

    data_check_string = '\n'.join(f'{key}={value}' for key, value in sorted(fields.items()))
    secret_key = hmac.new(b'WebAppData', token.encode('utf-8'), hashlib.sha256).digest()
    calculated_hash = hmac.new(secret_key, data_check_string.encode('utf-8'), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(calculated_hash, received_hash):
        raise ValueError('Подпись Telegram Mini App не прошла проверку')

    try:
        auth_date = int(fields['auth_date'])
        user = json.loads(fields['user'])
        telegram_id = user['id']
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        raise ValueError('В данных Telegram Mini App нет пользователя или даты') from None
    if type(telegram_id) is not int or telegram_id <= 0:
        raise ValueError('Некорректный Telegram ID')

    timestamp = int(time.time() if now is None else now)
    if auth_date > timestamp + MAX_FUTURE_SKEW_SECONDS:
        raise ValueError('Дата Telegram Mini App находится в будущем')
    if timestamp - auth_date > MAX_INIT_DATA_AGE_SECONDS:
        raise ValueError('Данные Telegram Mini App устарели; откройте приложение заново')
    return user
