"""One daily edition; independent of whether scientific conclusions changed."""
import json
import os
from datetime import datetime,timedelta,timezone
from zoneinfo import ZoneInfo
import llm
import db
from db import jsonb as Jsonb

def configured_zone():
    name=os.environ.get('DIGEST_TIMEZONE','Europe/Moscow')
    return timezone.utc if name=='UTC' else ZoneInfo(name)

def generate(conn,now=None):
    try:
        return _generate(conn,now)
    except llm.BudgetExceeded:
        # A status edition is still produced without paid calls; never claim no news.
        zone=configured_zone()
        moment=(now or datetime.now(zone)).astimezone(zone)
        content={'items':[],'message':'Ежедневный разбор не завершён: исчерпан установленный лимит модели. Материалы остаются в базе для последующей обработки.'}
        return conn.execute("INSERT INTO digests(day,status,content) VALUES (%s,'limited',%s) ON CONFLICT(day) DO UPDATE SET day=EXCLUDED.day RETURNING id",(moment.date(),Jsonb(content))).fetchone()[0]

def _generate(conn,now=None):
    zone=configured_zone()
    now=now or datetime.now(zone)
    now=now.astimezone(zone)
    if now.hour<int(os.environ.get('DIGEST_HOUR','19')):
        return None
    with conn.transaction():
        conn.execute('SELECT pg_advisory_xact_lock(4210)')
        existing=conn.execute('SELECT id,content,created_at FROM digests WHERE day=%s',(now.date(),)).fetchone()
        if existing and (existing[1].get('items') or existing[1].get('conclusion_updates')):
            return existing[0]
        # Empty status editions can be replaced when processing catches up later
        # the same day. Their own timestamp is the refresh watermark, otherwise
        # every worker cycle would repeat the same paid digest generation.
        since=(existing[2] if existing else
               conn.execute('SELECT max(created_at) FROM digests WHERE id<>%s AND day<%s',
                            (0,now.date())).fetchone()[0] or now-timedelta(days=1))
        rows=conn.execute("SELECT m.id,m.title,m.url,f.summary,f.kind,f.details FROM findings f JOIN materials m ON m.id=f.material_id WHERE f.created_at>%s AND m.status='done' AND NOT EXISTS(SELECT 1 FROM digest_items di JOIN digests d ON d.id=di.digest_id WHERE di.material_id=m.id AND d.created_at>=f.created_at) ORDER BY f.created_at DESC LIMIT 100",(since,)).fetchall()
        candidates=[dict(zip(('id','title','url','summary','kind','details'),r)) for r in rows]
        updates=conn.execute('SELECT topic,version,content,reason FROM conclusions WHERE created_at>%s ORDER BY created_at DESC LIMIT 30',(since,)).fetchall()
        changes=[dict(zip(('topic','version','content','reason'),r)) for r in updates]
        if existing and not candidates and not changes:
            return existing[0]
        pending=conn.execute("SELECT count(*) FROM materials WHERE status IN ('new','failed')").fetchone()[0]
        if candidates:
            result=llm.call(conn,'digest','Собери ежедневный дайджест до 10 пунктов. Отбирай безопасность, исследования, уточнения прежних знаний, новинки, экспертные материалы. Изменение научного вывода необязательно. Убирай повторы и перепечатки. Верни {items:[{material_ids:[int],title:str,summary:str,significance:str,limitations:str}]}. Используй только кандидатов, не добавляй фактов. Для мнений и рекламы указывай тип.',candidates)
            items=result.get('items')
            allowed={r['id'] for r in candidates}
            if not isinstance(items,list) or len(items)>10:
                raise ValueError('Некорректный дайджест')
            used=set()
            for item in items:
                ids=item.get('material_ids',[])
                if not ids or any(type(i) is not int or i not in allowed or i in used for i in ids):
                    raise ValueError('Некорректные ссылки дайджеста')
                used.update(ids)
                item['sources']=[{'url':r['url'],'kind':r['kind']} for r in candidates if r['id'] in ids]
            check=llm.call(conn,'digest_verify','Проверь, что дайджест не добавляет неподтверждённых утверждений, не превращает отзывы/рекламу в доказательства, не содержит дублей и персональных назначений. Верни {valid:bool}.',{'items':items,'candidates':candidates})
            if check.get('valid') is not True:
                raise ValueError('Дайджест не прошёл проверку')
        else:
            items=[]
            used=set()
        revision=int(existing[1].get('revision',1))+1 if existing else 1
        content={'items':items,'conclusion_updates':changes,'revision':revision,'message':('Обновились выводы по темам.' if changes else '') if items or changes else ('Анализ части материалов ещё не завершён; значимые обновления пока не подтверждены.' if pending else 'Сегодня значимых обновлений нет.')}
        if existing:
            did=existing[0]
            conn.execute("UPDATE digests SET content=%s,status='ready',created_at=now() WHERE id=%s",(Jsonb(content),did))
            db.log('digest',f'refreshed edition {did}; revision {revision}; items {len(items)}')
        else:
            did=conn.execute('INSERT INTO digests(day,content) VALUES (%s,%s) RETURNING id',(now.date(),Jsonb(content))).fetchone()[0]
            db.log('digest',f'created edition {did}; revision {revision}; items {len(items)}')
        for mid in used:
            conn.execute('INSERT INTO digest_items VALUES (%s,%s) ON CONFLICT DO NOTHING',(did,mid))
        return did
