"""Authenticated stdlib dashboard and JSON API. Deploy behind HTTPS."""
import base64
import binascii
import hmac
import json
import os
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

import db

MAX_BODY = 8 * 1024 * 1024
PAGE = Path(__file__).with_name('index.html')
MINIAPP_PAGE = Path(__file__).with_name('miniapp.html')


def request_error(error):
    """Only local validation messages are safe to reflect into an HTTP response."""
    messages={'Нужен объект JSON','Введите вопрос длиной до 2000 знаков','Некорректный материал',
        'Материал не найден','Укажите HTTP(S) адрес источника','Проверьте настройки источника',
        'Ключи и токены задаются только в окружении сервиса','Источник не найден',
        'Введите исправленный пересказ до 20000 знаков','Разбор материала не найден'}
    message=str(error)
    return message if message in messages else 'Запрос не прошёл проверку. Проверьте введённые данные.'


def authorized(header, token):
    if not token or not header:
        return False
    try:
        if header.startswith('Bearer '):
            candidate = header[7:]
        elif header.startswith('Basic '):
            decoded = base64.b64decode(header[6:], validate=True).decode('utf-8')
            candidate = decoded.split(':', 1)[1]
        else:
            return False
        return hmac.compare_digest(candidate.encode(), token.encode())
    except (ValueError, IndexError, UnicodeError, binascii.Error):
        return False


def rows(conn, sql, params=()):
    cursor = conn.execute(sql, params)
    names = [column.name for column in cursor.description]
    return [dict(zip(names, row)) for row in cursor.fetchall()]


def miniapp_page(query, maximum):
    params=parse_qs(query)
    limit=int(params.get('limit',[str(maximum)])[0])
    offset=int(params.get('offset',['0'])[0])
    if offset<0:
        raise ValueError('Неверные параметры списка')
    return min(max(limit,1),maximum),offset


def clean_config(config):
    if not isinstance(config, dict):
        return {}
    return {key: value for key, value in config.items()
            if not any(part in key.lower() for part in ('key', 'token', 'secret', 'password'))}


def list_materials(conn, query):
    text = query.get('q', [''])[0][:500]
    topic = query.get('topic', [''])[0][:200]
    kind = query.get('kind', [''])[0][:80]
    return rows(conn, """SELECT m.id, m.title, m.url, m.text_scope, m.status,
        m.published_at, m.collected_at, f.summary, f.kind, f.topics, f.verified,
        s.name AS source FROM materials m LEFT JOIN findings f ON f.material_id=m.id
        LEFT JOIN sources s ON s.id=m.source_id
        WHERE (%s='' OR m.title ILIKE %s OR f.summary ILIKE %s)
        AND (%s='' OR f.topics @> %s::jsonb) AND (%s='' OR f.kind=%s)
        ORDER BY m.collected_at DESC LIMIT 100""",
        (text, '%'+text+'%', '%'+text+'%', topic, json.dumps([topic]), kind, kind))


class Handler(BaseHTTPRequestHandler):
    server_version = 'SkinResearch'

    def miniapp_user(self):
        from web.telegram_auth import validate_init_data
        try:
            user = validate_init_data(self.headers.get('X-Telegram-Init-Data', ''))
        except RuntimeError:
            self.respond(503, {'error': 'Telegram Mini App не настроен на сервере'})
            return None
        except ValueError as error:
            self.respond(401, {'error': str(error)})
            return None

        owner = os.environ.get('OWNER_TELEGRAM_ID', '').strip()
        if not owner.isdigit() or int(owner) <= 0:
            self.respond(503, {'error': 'Владелец Mini App ещё не настроен'})
            return None
        telegram_id = user['id']
        if telegram_id != int(owner):
            self.respond(403, {'error': 'Доступ к Mini App пока разрешён только владелице'})
            return None
        with db.connect() as conn:
            participant = conn.execute(
                'SELECT role,active,miniapp_enabled FROM participants WHERE telegram_id=%s',
                (telegram_id,)).fetchone()
        if not participant or participant != ('owner', True, True):
            self.respond(403, {'error': 'Учётная запись Mini App не активна'})
            return None
        return telegram_id

    def respond(self, status, value, content_type='application/json; charset=utf-8', frameable=False):
        body = value if isinstance(value, bytes) else json.dumps(value, ensure_ascii=False, default=str).encode()
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        if not frameable:
            self.send_header('X-Frame-Options', 'DENY')
        self.send_header('Referrer-Policy', 'same-origin')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def authenticate(self, admin=False):
        token = os.environ.get('APP_TOKEN', '')
        if admin:
            token = os.environ.get('ADMIN_TOKEN') or token
        valid = authorized(self.headers.get('Authorization'), token)
        if not valid and not admin:
            valid = authorized(self.headers.get('Authorization'), os.environ.get('ADMIN_TOKEN', ''))
        if not valid:
            self.send_response(401)
            self.send_header('WWW-Authenticate', 'Basic realm="Skin Research", charset="UTF-8"')
            self.send_header('Content-Length', '0')
            self.end_headers()
        return valid

    def do_GET(self):
        parsed = urlsplit(self.path)
        if parsed.path == '/miniapp':
            self.respond(200, MINIAPP_PAGE.read_bytes(), 'text/html; charset=utf-8', frameable=True)
            return
        if parsed.path == '/health':
            try:
                with db.connect() as conn:
                    conn.execute('SELECT 1')
                self.respond(200, {'status': 'ok'})
            except Exception:
                self.respond(503, {'status': 'unavailable'})
            return
        if parsed.path.startswith('/api/miniapp/'):
            telegram_id = self.miniapp_user()
            if telegram_id is None:
                return
            try:
                with db.connect() as conn:
                    if parsed.path == '/api/miniapp/session':
                        data = {'telegram_id': telegram_id, 'role': 'owner'}
                    elif parsed.path == '/api/miniapp/answers':
                        limit,offset=miniapp_page(parsed.query,5)
                        data = rows(conn, 'SELECT id,question,content,created_at FROM answers WHERE owner_telegram_id=%s ORDER BY created_at DESC,id DESC LIMIT %s OFFSET %s', (telegram_id,limit,offset))
                    elif parsed.path == '/api/miniapp/feed':
                        from digest import current_findings
                        limit,offset=miniapp_page(parsed.query,10)
                        data=current_findings(conn,limit=limit,offset=offset)
                    elif parsed.path == '/api/miniapp/saved':
                        data = rows(conn, """SELECT m.id,m.title,m.url,m.text_scope,m.source_role,m.published_at,f.kind,s.name AS source,sm.saved_at
                            FROM saved_materials sm JOIN materials m ON m.id=sm.material_id
                            LEFT JOIN findings f ON f.material_id=m.id LEFT JOIN sources s ON s.id=m.source_id
                            WHERE sm.owner_telegram_id=%s ORDER BY sm.saved_at DESC LIMIT 100""", (telegram_id,))
                        from digest import current_findings
                        current={item['id']:item for item in current_findings(conn,[item['id'] for item in data],limit=100)}
                        data=[dict(current[item['id']],saved_at=item['saved_at'],review_status='eligible') if item['id'] in current else
                              dict(item,summary='',review_status='needs_review',notice='Сохранённый материал не входит в текущую подборку. Доступен первоисточник.') for item in data]
                    else:
                        self.respond(404, {'error': 'Страница не найдена'}); return
                self.respond(200, data)
            except ValueError:
                self.respond(400, {'error': 'Неверные параметры списка'})
            except Exception:
                self.respond(500, {'error': 'Не удалось загрузить личные данные'})
            return
        if not self.authenticate():
            return
        try:
            if parsed.path == '/':
                self.respond(200, PAGE.read_bytes(), 'text/html; charset=utf-8')
                return
            with db.connect() as conn:
                if parsed.path == '/api/materials':
                    data = list_materials(conn, parse_qs(parsed.query))
                elif parsed.path.startswith('/api/materials/'):
                    identifier = int(parsed.path.rsplit('/', 1)[1])
                    material = rows(conn, 'SELECT id,title,url,text,text_scope,doi,pmid,authors,status FROM materials WHERE id=%s', (identifier,))
                    if not material:
                        self.respond(404, {'error': 'Материал не найден'}); return
                    data = {'material': material[0],
                            'findings': rows(conn, 'SELECT summary,kind,topics,details,verified FROM findings WHERE material_id=%s', (identifier,)),
                            'facts': rows(conn, 'SELECT id,statement,quote,payload FROM facts WHERE material_id=%s AND active ORDER BY id', (identifier,)),
                            'corrections': rows(conn, 'SELECT before_value,after_value,created_at FROM corrections WHERE material_id=%s ORDER BY created_at DESC', (identifier,))}
                elif parsed.path == '/api/conclusions':
                    data = rows(conn, 'SELECT id,topic,version,content,reason,created_at FROM conclusions ORDER BY created_at DESC LIMIT 100')
                elif parsed.path == '/api/digests':
                    data = rows(conn, 'SELECT id,day,status,content,created_at FROM digests ORDER BY day DESC LIMIT 30')
                elif parsed.path == '/api/sources':
                    if not self.authenticate(admin=True): return
                    data = rows(conn, 'SELECT id,name,kind,url,active,config,interval_hours,last_checked_at,last_error,deferred_at,deferred_code,deferred_reason FROM sources ORDER BY id')
                    for source in data: source['config'] = clean_config(source['config'])
                elif parsed.path == '/api/runs':
                    if not self.authenticate(admin=True): return
                    data = rows(conn, 'SELECT id,service,status,started_at,finished_at,items,note FROM runs ORDER BY id DESC LIMIT 50')
                else:
                    self.respond(404, {'error': 'Страница не найдена'}); return
            self.respond(200, data)
        except ValueError:
            self.respond(400, {'error': 'Неверный идентификатор'})
        except Exception:
            self.respond(500, {'error': 'Не удалось загрузить данные. Проверьте доступность базы.'})

    def do_POST(self):
        path = urlsplit(self.path).path
        if path.startswith('/api/miniapp/'):
            telegram_id = self.miniapp_user()
            if telegram_id is None:
                return
            origin = self.headers.get('Origin')
            if origin and urlsplit(origin).netloc != self.headers.get('Host'):
                self.respond(403, {'error': 'Запрос с другого сайта запрещён'}); return
            try:
                length = int(self.headers.get('Content-Length', '0'))
                if not 0 < length <= MAX_BODY:
                    self.respond(413, {'error': 'Запрос должен быть не больше 8 МБ'}); return
                if self.headers.get('Content-Type', '').split(';')[0] != 'application/json':
                    self.respond(415, {'error': 'Нужен формат JSON'}); return
                payload = json.loads(self.rfile.read(length))
                if not isinstance(payload, dict):
                    raise ValueError('Нужен объект JSON')
                with db.connect() as conn:
                    if path == '/api/miniapp/ask':
                        question = payload.get('question', '')
                        if not isinstance(question, str) or not question.strip() or len(question) > 2000:
                            raise ValueError('Введите вопрос длиной до 2000 знаков')
                        from research import answer
                        data = answer(conn, question.strip(), owner_telegram_id=telegram_id)
                    elif path == '/api/miniapp/save':
                        try:
                            material_id = int(payload.get('material_id'))
                        except (ValueError, TypeError):
                            raise ValueError('Некорректный материал') from None
                        own=conn.execute('SELECT 1 FROM saved_materials WHERE owner_telegram_id=%s AND material_id=%s',
                                         (telegram_id,material_id)).fetchone()
                        if not own:
                            from digest import current_findings
                            if not current_findings(conn,[material_id],limit=1):
                                self.respond(404, {'error': 'Материал не найден'}); return
                            conn.execute('INSERT INTO saved_materials(owner_telegram_id,material_id) VALUES (%s,%s) ON CONFLICT DO NOTHING',
                                         (telegram_id,material_id))
                        data = {'material_id': material_id, 'saved': True}
                    else:
                        self.respond(404, {'error': 'Действие не найдено'}); return
                self.respond(200, data)
            except (ValueError, json.JSONDecodeError) as error:
                self.respond(400, {'error': request_error(error)})
            except Exception:
                self.respond(500, {'error': 'Не удалось обработать вопрос. Попробуйте позже.'})
            return
        if not self.authenticate(admin=path != '/api/ask'): return
        origin = self.headers.get('Origin')
        if origin and urlsplit(origin).netloc != self.headers.get('Host'):
            self.respond(403, {'error': 'Запрос с другого сайта запрещён'}); return
        try:
            length = int(self.headers.get('Content-Length', '0'))
            if not 0 < length <= MAX_BODY:
                self.respond(413, {'error': 'Допустим файл или текст до 8 МБ'}); return
            if self.headers.get('Content-Type', '').split(';')[0] != 'application/json':
                self.respond(415, {'error': 'Нужен формат JSON'}); return
            payload = json.loads(self.rfile.read(length))
            if not isinstance(payload, dict): raise ValueError('Нужен объект JSON')
            from psycopg.types.json import Jsonb
            with db.connect() as conn:
                if path == '/api/ask':
                    question = payload.get('question', '')
                    if not isinstance(question, str) or not question.strip() or len(question) > 2000:
                        raise ValueError('Введите вопрос длиной до 2000 знаков')
                    from research import answer
                    data = answer(conn, question.strip())
                elif path == '/api/ingest':
                    from collector.main import ingest_manual
                    data = {'id': ingest_manual(conn, payload)}
                elif path == '/api/sources':
                    identifier = int(payload['id'])
                    active = payload.get('active')
                    interval = int(payload.get('interval_hours', 24))
                    config = payload.get('config', {})
                    source_url = payload.get('url', '')
                    if not isinstance(source_url, str) or urlsplit(source_url).scheme not in ('http', 'https') or not urlsplit(source_url).hostname:
                        raise ValueError('Укажите HTTP(S) адрес источника')
                    if type(active) is not bool or not isinstance(config, dict) or not 1 <= interval <= 8760:
                        raise ValueError('Проверьте настройки источника')
                    # Config contains queries/options, never credentials; keep secrets in environment.
                    if clean_config(config) != config:
                        raise ValueError('Ключи и токены задаются только в окружении сервиса')
                    changed = conn.execute('''UPDATE sources SET active=%s,interval_hours=%s,config=%s,url=%s,
                        deferred_at=CASE WHEN %s THEN NULL ELSE deferred_at END,
                        deferred_code=CASE WHEN %s THEN NULL ELSE deferred_code END,
                        deferred_reason=CASE WHEN %s THEN NULL ELSE deferred_reason END
                        WHERE id=%s RETURNING id''',
                                           (active, interval, Jsonb(config), source_url, active, active, active, identifier)).fetchone()
                    if not changed: raise ValueError('Источник не найден')
                    data = {'id': identifier, 'saved': True}
                elif path == '/api/correct':
                    identifier = int(payload['material_id'])
                    summary = payload.get('summary')
                    if not isinstance(summary, str) or not summary.strip() or len(summary) > 20000:
                        raise ValueError('Введите исправленный пересказ до 20000 знаков')
                    with conn.transaction():
                        old = conn.execute('SELECT summary,topics,details FROM findings WHERE material_id=%s FOR UPDATE', (identifier,)).fetchone()
                        if not old: raise ValueError('Разбор материала не найден')
                        before = dict(zip(('summary','topics','details'), old))
                        after = dict(before, summary=summary.strip())
                        conn.execute('INSERT INTO corrections(material_id,before_value,after_value) VALUES(%s,%s,%s)', (identifier, Jsonb(before), Jsonb(after)))
                        conn.execute('UPDATE findings SET summary=%s WHERE material_id=%s', (summary.strip(), identifier))
                    data = {'saved': True}
                else:
                    self.respond(404, {'error': 'Действие не найдено'}); return
            self.respond(200, data)
        except (ValueError, KeyError, TypeError, json.JSONDecodeError) as error:
            self.respond(400, {'error': request_error(error)})
        except Exception:
            self.respond(500, {'error': 'Действие не выполнено. Проверьте журнал сервиса и повторите запрос.'})

    def log_message(self, fmt, *args):
        # Never log question, body, token, URL query, or model output.
        db.log('web', f'{self.command} {urlsplit(self.path).path}')


def main():
    app_token = os.environ.get('APP_TOKEN', '')
    if app_token and len(app_token) < 16:
        raise RuntimeError('Set APP_TOKEN to a private token of at least 16 characters')
    with db.connect() as conn:
        db.init_schema(conn)
    ThreadingHTTPServer(('0.0.0.0', int(os.environ.get('PORT', '8000'))), Handler).serve_forever()


if __name__ == '__main__':
    main()
