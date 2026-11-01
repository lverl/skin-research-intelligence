"""Daily Telegram delivery to owner-configured chat IDs. Never auto-subscribes strangers."""
import json
import os
import time
import urllib.request
import db

def chunks(text,limit=3500):
    # One bounded message prevents replaying earlier chunks after a later failure.
    return [text if len(text)<=limit else text[:limit-80]+'\nПолный выпуск доступен в веб-интерфейсе.']

def format_digest(day,content):
    parts=[f'Skin Research · {day}']
    if not content.get('items'):
        parts.append(content.get('message','Сегодня значимых обновлений нет.'))
    for item in content.get('items',[]):
        parts.append('\n'+str(item.get('title',''))+'\n'+str(item.get('summary',''))+'\n'+str(item.get('significance',''))+'\n'+str(item.get('limitations','')))
        parts.extend(s['url']+' · '+s['kind'] for s in item.get('sources',[]))
    for change in content.get('conclusion_updates',[]):
        parts.append('\nОбновлён вывод: '+change['topic']+' · версия '+str(change['version']))
    return '\n'.join(parts)

def deliver(conn):
    token=os.environ.get('TELEGRAM_TOKEN','')
    chats=[int(x.strip()) for x in os.environ.get('TELEGRAM_CHAT_IDS','').split(',') if x.strip()]
    if not token or not chats:
        return 0
    sent=0
    for did,day,content in conn.execute("SELECT id,day,content FROM digests WHERE day >= CURRENT_DATE-1 ORDER BY day").fetchall():
        for chat in chats:
            with conn.transaction():
                conn.execute('SELECT pg_advisory_xact_lock(%s)',(4211,))
                conn.execute('INSERT INTO digest_deliveries(digest_id,chat_id) VALUES (%s,%s) ON CONFLICT DO NOTHING',(did,chat))
                state=conn.execute('SELECT status FROM digest_deliveries WHERE digest_id=%s AND chat_id=%s FOR UPDATE',(did,chat)).fetchone()[0]
                if state=='sent':
                    continue
                try:
                    for part in chunks(format_digest(day,content)):
                        req=urllib.request.Request('https://api.telegram.org/bot'+token+'/sendMessage',data=json.dumps({'chat_id':chat,'text':part,'disable_web_page_preview':True}).encode(),headers={'Content-Type':'application/json'})
                        with urllib.request.urlopen(req,timeout=30) as response:
                            if not json.load(response).get('ok'):
                                raise RuntimeError('Telegram rejected delivery')
                    conn.execute("UPDATE digest_deliveries SET status='sent',sent_at=now(),error=NULL WHERE digest_id=%s AND chat_id=%s",(did,chat))
                    sent+=1
                except Exception as error:
                    conn.execute("UPDATE digest_deliveries SET status='failed',error=%s WHERE digest_id=%s AND chat_id=%s",(type(error).__name__,did,chat))
    return sent

def main():
    while True:
        try:
            with db.connect() as conn:
                db.init_schema(conn)
                deliver(conn)
        except Exception as error:
            db.log('bot',type(error).__name__)
        time.sleep(60)
