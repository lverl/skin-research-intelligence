"""Private Telegram research chat and resumable daily digest delivery."""
import json
import os
import re
import time
import urllib.request
import urllib.error
from urllib.parse import urlparse

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
    if text.startswith('Skin Research · ') and text.count('\n\n• ') > 5:
        header, *items = text.split('\n\n• ')
        return [part for offset in range(0, len(items), 5)
                for part in chunks(header + '\n\n• ' + '\n\n• '.join(items[offset:offset+5]), limit)]
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
    payload = {'chat_id': chat_id, 'text': text, 'disable_web_page_preview': True}
    if text.startswith('Skin Research · '):
        entities = []
        bullets = list(re.finditer(r'^• ([^\n]+)', text, re.MULTILINE))
        for index, match in enumerate(bullets):
            end = bullets[index+1].start() if index+1 < len(bullets) else len(text)
            url = re.search(r'^https?://\S+', text[match.end():end], re.MULTILINE)
            if url:
                entities.append({'type':'text_link', 'offset':utf16_length(text[:match.start(1)]),
                                 'length':utf16_length(match.group(1)), 'url':url.group()})
        if entities:
            payload['entities'] = entities
    return api('sendMessage', payload)


def format_digest(day, content):
    parts = [f'Skin Research · {day}']
    if not content.get('items') or content.get('coverage_status') in {'partial','incomplete'}:
        parts.append(content.get('message', 'Сегодня значимых обновлений нет.'))
    items = content.get('items', [])
    if items:
        parts.append('Материалов в дайджесте: ' + str(len(items)))
    for item in items:
        lines = ['• ' + str(item.get('summary') or item.get('title', ''))]
        if item.get('limitations'):
            lines.append(str(item['limitations']))
        for source in item.get('sources', []):
            url = str(source.get('url', ''))
            name = str(source.get('name') or urlparse(url).hostname or 'Источник')
            if source.get('published_at'):
                name+=' · публикация '+str(source['published_at'])[:4]+' года'
            role=source.get('source_role')
            if role in {'reference','professional_information'}: name+=' · справочный материал'
            if role=='systematic_review_summary': name+=' · публичное резюме обзора'
            if source.get('scope')=='abstract': name+=' · изучена аннотация'
            elif source.get('scope')=='excerpt': name+=' · доступный фрагмент'
            lines.append(name + '\n' + url)
        parts.append('\n'.join(lines))
    return '\n\n'.join(parts)


def format_answer(content):
    state=research.answer_state(content)
    parts=[research.ANSWER_MESSAGES[state]]
    if content.get('clarification'):
        parts.append(str(content['clarification']))
    if state in {'completed','partial'}:
        parts.extend(str(claim['text']) for claim in content.get('claims', []))
    for key, title in (('limitations', 'Ограничения'), ('contradictions', 'Противоречия'), ('missing_data', 'Чего не хватает')):
        if content.get(key):
            lines=[re.sub(r'\b[A-Za-z][A-Za-z0-9_]*(?:Error|Exception)\b','ошибка проверки',str(value)) for value in content[key]]
            parts.append(title + ':\n' + '\n'.join('• ' + value for value in lines))
    sources, seen = [], set()
    for source in content.get('sources', []) if state in {'completed','partial'} else []:
        url = source.get('url')
        if url and url not in seen:
            seen.add(url)
            roles={'reference':'Дерматологическая справка','professional_information':'Профессиональное объяснение для пациентов',
                   'systematic_review':'Обзор исследований','systematic_review_summary':'Публичное резюме обзора'}
            kinds={'research':'Исследование','review':'Обзор','guideline':'Рекомендация','regulatory':'Регуляторный материал',
                   'news':'Новость','commercial':'Коммерческий материал','expert':'Экспертный материал','experience':'Личный опыт','product':'Данные о продукте'}
            scopes={'abstract':'изучена аннотация','excerpt':'доступный фрагмент','metadata':'только метаданные','full_text':'полный текст'}
            label=roles.get(source.get('source_role')) or kinds.get(source.get('kind'),'Источник')
            suffix=' · '+scopes[source['scope']] if source.get('scope') in scopes else ''
            sources.append(str(source.get('title', 'Источник')) + ' · '+label+ suffix + '\n' + str(url))
    if sources:
        parts.append('Источники:\n' + '\n\n'.join(sources))
    labels={'success':'проверен','empty':'подходящих результатов нет','no_text':'нет доступного текста',
            'partial':'проверен частично','failed':'проверка не завершена','blocked':'лимит исчерпан',
            'disabled':'планово выключен','deferred':'подключение отложено','out_of_scope':'в каталоге нет страниц для этой темы'}
    if content.get('source_checks'):
        parts.append('Проверка источников:\n'+'\n'.join(str(check.get('name') or 'Источник')+': '+labels.get(check.get('status'),'проверка не завершена')+
            (' · не входил в эту проверку' if check.get('planned') is False else '') for check in content['source_checks'][:20]))
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


def format_model_budget(summary):
    def money(value):
        if value is None:
            return 'не задан'
        text = format(value, 'f')
        return '$' + (text.rstrip('0').rstrip('.') if '.' in text else text)

    return '\n'.join((
        'Бюджет модели за текущий месяц (учёт приложения):',
        'Месячный лимит: ' + money(summary['monthly_limit_usd']),
        'Известная стоимость: ' + money(summary['actual_usd']),
        'Резервы с неизвестной стоимостью: ' + money(summary['unknown_reserved_usd'])
            + ' (вызовов: ' + str(summary['unknown_calls']) + ')',
        'Учитывается для лимита: ' + money(summary['accounted_usd']),
        'Доступный остаток: ' + money(summary['available_usd']),
        'Резерв следующего вызова: ' + money(summary['next_reserve_usd']),
        'По бюджету сейчас: ' + ('вызов разрешён' if summary['allowed'] else 'вызов заблокирован'),
        'Причина: ' + summary['reason']))


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
        owner=os.environ.get('OWNER_TELEGRAM_ID','').strip()
        if type(chat_id) is int and owner.isdigit() and int(owner)>0 and chat_id==int(owner):
            try:
                lines.append(format_model_budget(llm.budget_summary(conn)))
            except Exception:
                lines.append('Сводка бюджета модели временно недоступна.')
            topics=conn.execute("""SELECT topic,attempts,last_error FROM topic_refresh
                WHERE state IN ('pending','running','failed') ORDER BY updated_at LIMIT 5""").fetchall()
            if topics:
                from net import ERROR_REASONS
                lines.append('Обновление общих выводов ожидается отдельно; ответы на вопросы его не ждут.')
                for topic,attempts,error in topics:
                    reason=ERROR_REASONS.get(error.get('code'),'Обновление ожидается') if isinstance(error,dict) else 'Обновление ожидается'
                    lines.append(str(topic)[:120]+': '+reason+'; попыток '+str(attempts))
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
    return format_answer(research.answer(conn, text, owner_telegram_id=chat_id))


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
                miniapp_url=os.environ.get('WEBAPP_URL','').strip()
                if miniapp_url:
                    parsed=urlparse(miniapp_url)
                    if parsed.scheme!='https' or not parsed.netloc:
                        db.log('bot','WEBAPP_URL должен быть публичным HTTPS-адресом')
                    else:
                        try:
                            api('setChatMenuButton', {'menu_button': {'type':'web_app','text':'Skin Research','web_app':{'url':miniapp_url}}})
                            db.log('bot','Кнопка Mini App настроена')
                        except Exception as error:
                            db.log('bot','Не удалось настроить кнопку Mini App: '+type(error).__name__)
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
