"""Private Telegram research chat and resumable daily digest delivery."""
import json
import os
import time
import urllib.request
import urllib.error

import db
import research
from db import jsonb as Jsonb

class TelegramError(RuntimeError):
    def __init__(self,code):
        self.code=int(code)
        super().__init__('Telegram request failed')


def utf16_length(text):
    return len(text.encode('utf-16-le')) // 2


def chunks(text, limit=3500):
    """Lossless splitting at paragraphs/words within Telegram UTF-16 limits."""
    if limit < 2:
        raise ValueError('Message limit must be at least two UTF-16 units')
    text = str(text)
    result = []
    while text:
        if utf16_length(text) <= limit:
            result.append(text)
            break
        units, end = 0, 0
        for index, character in enumerate(text):
            size = 2 if ord(character) > 0xffff else 1
            if units + size > limit:
                break
            units += size
            end = index + 1
        window = text[:end]
        split = window.rfind('\n\n')
        if split >= end // 2:
            end = split + 2
        else:
            split = max(window.rfind('\n'), window.rfind(' '))
            if split >= end // 2:
                end = split + 1
        result.append(text[:end])
        text = text[end:]
    return result or ['']


def configured_chats():
    return {int(value.strip()) for value in os.environ.get('TELEGRAM_CHAT_IDS', '').split(',') if value.strip()}


def api(method, payload):
    token = os.environ.get('TELEGRAM_TOKEN', '').strip()
    if not token:
        raise RuntimeError('TELEGRAM_TOKEN is not configured')
    request = urllib.request.Request(
        'https://api.telegram.org/bot' + token + '/' + method,
        data=json.dumps(payload, ensure_ascii=False).encode('utf-8'),
        headers={'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(request, timeout=65 if method == 'getUpdates' else 30) as response:
            result = json.load(response)
    except urllib.error.HTTPError as error:
        raise TelegramError(error.code) from None
    if result.get('ok') is not True:
        raise TelegramError(result.get('error_code',500))
    return result.get('result')


def send(chat_id, text):
    return api('sendMessage', {'chat_id': chat_id, 'text': text, 'disable_web_page_preview': True})


def format_digest(day, content):
    parts = [f'Skin Research · {day}']
    if not content.get('items'):
        parts.append(content.get('message', 'Сегодня значимых обновлений нет.'))
    for item in content.get('items', []):
        parts.append('\n' + '\n'.join(str(item.get(key, '')) for key in ('title', 'summary', 'significance', 'limitations')))
        parts.extend(str(source.get('url', '')) + ' · ' + str(source.get('kind', '')) for source in item.get('sources', []))
    for change in content.get('conclusion_updates', []):
        parts.append('\nОбновлён вывод: ' + str(change['topic']) + ' · версия ' + str(change['version']))
    return '\n'.join(parts)


def format_answer(content):
    if content.get('clarification'):
        return str(content['clarification'])
    parts = [str(claim['text']) for claim in content.get('claims', [])]
    if not parts:
        parts.append('Проверенных данных для содержательного ответа пока недостаточно.')
    for key, title in (('limitations', 'Ограничения'), ('contradictions', 'Противоречия'), ('missing_data', 'Чего не хватает')):
        if content.get(key):
            parts.append(title + ':\n' + '\n'.join('• ' + str(value) for value in content[key]))
    sources, seen = [], set()
    for source in content.get('sources', []):
        url = source.get('url')
        if url and url not in seen:
            seen.add(url)
            suffix = ' · изучена аннотация' if source.get('scope') == 'abstract' else ''
            sources.append(str(source.get('title', 'Источник')) + ' · '+str(source.get('kind','тип не указан'))+ suffix + '\n' + str(url))
    if sources:
        parts.append('Источники:\n' + '\n\n'.join(sources))
    parts.append('Справочная информация; индивидуальные назначения не составляются.')
    return '\n\n'.join(parts)


def deliver(conn):
    """Resume each recipient from the last confirmed chunk, without truncation.

    Telegram has no idempotency key: a network failure after acceptance but before
    acknowledgement can still cause one duplicated chunk on retry.
    """
    chats = configured_chats()
    if not os.environ.get('TELEGRAM_TOKEN') or not chats:
        return 0
    sent = 0
    for did, day, content in conn.execute('SELECT id,day,content FROM digests WHERE day >= CURRENT_DATE-1 ORDER BY day').fetchall():
        for chat in sorted(chats):
            conn.execute('INSERT INTO digest_deliveries(digest_id,chat_id) VALUES (%s,%s) ON CONFLICT DO NOTHING', (did, chat))
            state, next_part, delivered_revision = conn.execute('SELECT status,next_part,revision FROM digest_deliveries WHERE digest_id=%s AND chat_id=%s', (did, chat)).fetchone()
            revision=int(content.get('revision',1))
            if delivered_revision!=revision:
                conn.execute("UPDATE digest_deliveries SET revision=%s,next_part=0,status='pending',error=NULL WHERE digest_id=%s AND chat_id=%s",(revision,did,chat))
                state,next_part='pending',0
            if state in ('sent','abandoned'):
                continue
            try:
                parts = chunks(format_digest(day, content))
                for index in range(next_part, len(parts)):
                    send(chat, parts[index])
                    # Autocommit after each confirmed send; a later failure does
                    # not roll back progress for earlier parts.
                    conn.execute("UPDATE digest_deliveries SET next_part=%s,status='sending',error=NULL WHERE digest_id=%s AND chat_id=%s", (index + 1, did, chat))
                conn.execute("UPDATE digest_deliveries SET status='sent',sent_at=now(),error=NULL WHERE digest_id=%s AND chat_id=%s", (did, chat))
                sent += 1
            except Exception as error:
                state='abandoned' if isinstance(error,TelegramError) and error.code in (400,401,403,404) else 'failed'
                conn.execute('UPDATE digest_deliveries SET status=%s,error=%s WHERE digest_id=%s AND chat_id=%s', (state,type(error).__name__, did, chat))
    return sent


HELP = ('Задайте вопрос об уходе, ингредиентах или процедурах — я проверю накопленные данные и при необходимости выполню поиск.\n\n'
        '/digest — последний ежедневный дайджест\n/status — состояние базы\n/id — ваш ID для доступа\n/help — эта подсказка')


def command_response(conn, chat_id, text, chat_type='private'):
    """Authorize before invoking research or exposing any stored material."""
    command = text.strip().split(maxsplit=1)[0].split('@')[0].lower() if text.strip() else ''
    if chat_type != 'private':
        return 'Бот работает в личном чате. Напишите ему напрямую.'
    if command == '/id':
        return f'Ваш Telegram ID: {chat_id}. Передайте его владелице сервиса для предоставления доступа.'
    if chat_id not in configured_chats():
        return 'Доступ пока не настроен. Отправьте /id и передайте свой ID владелице сервиса.'
    if command in ('/start', '/help'):
        return HELP
    if command == '/status':
        materials, findings, pending = conn.execute("SELECT (SELECT count(*) FROM materials),(SELECT count(*) FROM findings),(SELECT count(*) FROM materials WHERE status IN ('new','failed'))").fetchone()
        import llm
        lines=[f'В базе материалов: {materials}.\nРазобрано: {findings}.\nОжидают разбора или повторной попытки: {pending}.']
        issues=llm.configuration_issues()
        if issues: lines.append('Настройки модели: '+'; '.join(issues))
        for reason,count in conn.execute("SELECT last_error,count(*) FROM materials WHERE status='failed' GROUP BY last_error ORDER BY count(*) DESC LIMIT 5").fetchall():
            lines.append(f'Ошибки обработки: {reason or "причина не записана"} — {count}')
        for name,error in conn.execute('SELECT name,last_error FROM sources WHERE active AND last_error IS NOT NULL ORDER BY name LIMIT 5').fetchall():
            lines.append('Источник '+name+': '+error)
        return '\n\n'.join(lines)
    if command == '/digest':
        row = conn.execute('SELECT day,content FROM digests ORDER BY day DESC LIMIT 1').fetchone()
        return format_digest(*row) if row else 'Первый ежедневный дайджест ещё не сформирован.'
    if command.startswith('/'):
        return 'Неизвестная команда. Отправьте /help или задайте вопрос обычным сообщением.'
    if not text.strip():
        return 'Пришлите вопрос текстом.'
    if len(text) > 2000:
        return 'Сократите вопрос до 2000 символов.'
    return format_answer(research.answer(conn, text))


def enqueue_updates(conn, updates):
    """Persist inbound events before advancing the durable Telegram cursor."""
    for update in updates:
        update_id = update.get('update_id')
        if type(update_id) is not int:
            continue
        message = update.get('message') or {}
        chat = message.get('chat') or {}
        chat_id = chat.get('id')
        if type(chat_id) is not int:
            # No reply is possible for this event type; still acknowledge it.
            chat_id = 0
        payload = {'text': message.get('text') or '', 'chat_type': chat.get('type'), 'has_message': bool(message)}
        with conn.transaction():
            conn.execute("INSERT INTO telegram_updates(update_id,chat_id,status,response) VALUES (%s,%s,'received',%s) ON CONFLICT(update_id) DO NOTHING", (update_id, chat_id, Jsonb(payload)))
            conn.execute("INSERT INTO telegram_state(key,value) VALUES ('offset',%s) ON CONFLICT(key) DO UPDATE SET value=GREATEST(telegram_state.value,EXCLUDED.value)", (update_id + 1,))


def process_updates(conn):
    rows = conn.execute("SELECT update_id,chat_id,status,response,next_part FROM telegram_updates WHERE status IN ('received','processing','ready') AND (next_retry_at IS NULL OR next_retry_at <= now()) ORDER BY update_id LIMIT 20").fetchall()
    for update_id, chat_id, status, response, next_part in rows:
        if status in ('received', 'processing'):
            restricted=chat_id in configured_chats() and response.get('chat_type')=='private' and (response.get('text','').strip().split(maxsplit=1) or [''])[0].split('@')[0].lower()!='/id'
            if status == 'processing':
                # A previous process died after claiming the event. Do not pay
                # for the same question again when the cursor is replayed.
                text = 'Предыдущий разбор прервался. Повторите вопрос новым сообщением, если ответ всё ещё нужен.'
            elif not response.get('has_message') or not chat_id:
                conn.execute("UPDATE telegram_updates SET status='sent' WHERE update_id=%s", (update_id,))
                continue
            else:
                conn.execute("UPDATE telegram_updates SET status='processing' WHERE update_id=%s", (update_id,))
                try:
                    text = command_response(conn, chat_id, response['text'], response.get('chat_type'))
                except Exception as error:
                    db.log('bot', 'question failed: ' + type(error).__name__)
                    text = 'Не удалось завершить разбор. Попробуйте позже; повторная отправка этого сообщения системой не запускает новый платный анализ.'
            response = {'parts': chunks(text),'restricted':restricted}
            conn.execute("UPDATE telegram_updates SET status='ready',response=%s WHERE update_id=%s", (Jsonb(response), update_id))
        if response.get('restricted') and chat_id not in configured_chats():
            conn.execute("UPDATE telegram_updates SET status='abandoned' WHERE update_id=%s",(update_id,))
            continue
        try:
            parts = response.get('parts', [])
            for index in range(next_part, len(parts)):
                send(chat_id, parts[index])
                conn.execute('UPDATE telegram_updates SET next_part=%s WHERE update_id=%s', (index + 1, update_id))
            conn.execute("UPDATE telegram_updates SET status='sent' WHERE update_id=%s", (update_id,))
        except Exception as error:
            db.log('bot', 'reply pending: ' + type(error).__name__)
            permanent=isinstance(error,TelegramError) and error.code in (400,401,403,404)
            conn.execute("UPDATE telegram_updates SET attempts=attempts+1,status=CASE WHEN %s OR attempts>=4 THEN 'abandoned' ELSE 'ready' END,next_retry_at=now()+interval '1 minute' WHERE update_id=%s",(permanent,update_id))


def poll_once(conn):
    cursor = conn.execute("SELECT value FROM telegram_state WHERE key='offset'").fetchone()
    updates = api('getUpdates', {'offset': cursor[0] if cursor else 0, 'timeout': 25, 'limit': 20, 'allowed_updates': ['message']})
    enqueue_updates(conn, updates or [])
    process_updates(conn)


def main():
    db.log('bot','Telegram-сервис запущен')
    while True:
        try:
            with db.connect() as conn:
                db.init_schema(conn)
                if not os.environ.get('TELEGRAM_TOKEN','').strip():
                    db.log('bot','TELEGRAM_TOKEN не задан: укажите токен своего бота в Coolify и выполните Redeploy')
                    time.sleep(30)
                    continue
                if not conn.execute('SELECT pg_try_advisory_lock(4212)').fetchone()[0]:
                    db.log('bot', 'Another bot instance is already running')
                    time.sleep(10)
                    continue
                identity=api('getMe',{})
                db.log('bot','Подключён @'+str(identity.get('username',''))+'; разрешённых чатов: '+str(len(configured_chats()))+'; ожидаю сообщения, /id доступна до настройки списка чатов')
                # Drain durable replies before waiting for additional updates.
                process_updates(conn)
                while True:
                    deliver(conn)
                    poll_once(conn)
        except Exception as error:
            message=type(error).__name__
            if isinstance(error,TelegramError):
                message+=' HTTP '+str(error.code)
                if error.code==409:
                    message+=': Telegram уже используется другим процессом или настроен webhook'
                elif error.code==401:
                    message+=': проверьте TELEGRAM_TOKEN'
            db.log('bot',message)
        time.sleep(10)
