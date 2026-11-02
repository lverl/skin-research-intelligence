"""Separate triage, extraction and verification; bounded retry with atomic writes."""
import json
import os
import time
import db
import llm
from evidence import validate_facts
from db import jsonb as Jsonb

KINDS={'research','review','guideline','regulatory','news','commercial','expert','experience','product'}

def process_material(conn,row,force=False):
    mid=row[0]
    locked=conn.execute('SELECT pg_try_advisory_lock(%s,%s)',(42,mid)).fetchone()[0]
    if not locked:
        return
    try:
        current=conn.execute('SELECT id,title,text,text_scope FROM materials WHERE id=%s',(mid,)).fetchone()
        return _process_material(conn,current or row,force)
    finally:
        conn.execute('SELECT pg_advisory_unlock(%s,%s)',(42,mid))

def _process_material(conn,row,force=False):
    mid,title,text,scope=row
    prior=conn.execute('SELECT topics FROM findings WHERE material_id=%s',(mid,)).fetchone()
    previous_topics=prior[0] if prior else []
    if not text:
        raise ValueError('Нет текста для анализа')
    triage=llm.call(conn,'triage','Оцени релевантность косметологии и уходу. Верни {relevant:bool, important:bool, kind:str, summary:str, topics:[str]}. kind: research/review/guideline/regulatory/news/commercial/expert/experience/product. summary — краткий пересказ по-русски; topics — до 5 устойчивых русских названий тем.',{'title':title,'text':text[:12000],'scope':scope})
    if type(triage.get('relevant')) is not bool:
        raise ValueError('Некорректный отбор')
    if not triage['relevant']:
        conn.execute("UPDATE materials SET status='skipped' WHERE id=%s",(mid,))
        conn.execute('UPDATE facts SET active=FALSE WHERE material_id=%s',(mid,))
        from research import synthesize
        for topic in previous_topics:
            synthesize(conn,topic)
        return
    if triage.get('kind') not in KINDS or not isinstance(triage.get('summary'),str) or not isinstance(triage.get('topics'),list):
        raise ValueError('Некорректная классификация')
    topics=list(dict.fromkeys(t.strip() for t in triage['topics'] if isinstance(t,str) and t.strip()))[:5]
    details={'text_scope':scope,'limitations':['Изучена только аннотация'] if scope=='abstract' else [],'facts':[]}
    deep=force or triage.get('important') is True
    if deep:
        for offset in range(0,len(text),18000):
            chunk=text[offset:offset+18000]
            extracted=llm.call(conn,'extract','Извлеки только явно указанные данные. Верни {facts:[{statement:str,quote:str,payload:{}}],limitations:[str]}. quote — дословный фрагмент. В payload укажи доступные дизайн, выборку, протокол, сравнение, показатель, число, единицу, сроки, риски, финансирование. Неизвестное не заполняй. Отдельно различай данные ингредиента и готового продукта; отзывы не доказательство эффективности.',{'title':title,'text':chunk,'scope':scope})
            facts=validate_facts(extracted.get('facts',[]),chunk)
            checked=llm.call(conn,'verify','Проверь, следует ли каждое statement из quote в данном контексте. Верни {accepted_indices:[int],limitations:[str]}. Индексы с нуля. Отклоняй подмену протокола, чисел, группы, срока, причинности, перенос эффекта ингредиента на продукт.',{'text':chunk,'facts':facts})
            indices=checked.get('accepted_indices')
            if not isinstance(indices,list) or any(type(i) is not int or i<0 or i>=len(facts) for i in indices):
                raise ValueError('Некорректный результат проверки')
            details['facts'].extend(facts[i] for i in sorted(set(indices)))
            details['limitations'].extend(str(x) for x in extracted.get('limitations',[]))
            details['limitations'].extend(str(x) for x in checked.get('limitations',[]))
    with conn.transaction():
        current=conn.execute('SELECT text FROM materials WHERE id=%s FOR UPDATE',(mid,)).fetchone()
        if not current or current[0]!=text:
            raise ValueError('Текст изменился во время обработки')
        old=conn.execute('SELECT details FROM findings WHERE material_id=%s',(mid,)).fetchone()
        if old:
            conn.execute('INSERT INTO corrections(material_id,before_value,after_value) VALUES (%s,%s,%s)',(mid,Jsonb(old[0]),Jsonb(details)))
        conn.execute('INSERT INTO findings(material_id,summary,model,prompt_version,topics,kind,details,verified) VALUES (%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(material_id) DO UPDATE SET summary=EXCLUDED.summary,model=EXCLUDED.model,prompt_version=EXCLUDED.prompt_version,topics=EXCLUDED.topics,kind=EXCLUDED.kind,details=EXCLUDED.details,verified=EXCLUDED.verified,created_at=now()', (mid,triage['summary'],os.environ.get('MODEL',''), 'skin-v1',Jsonb(topics),triage['kind'],Jsonb(details),deep))
        conn.execute('UPDATE facts SET active=FALSE WHERE material_id=%s',(mid,))
        for fact in details['facts']:
            conn.execute('INSERT INTO facts(material_id,statement,quote,payload) VALUES (%s,%s,%s,%s) ON CONFLICT(material_id,statement,quote) DO UPDATE SET active=TRUE,payload=EXCLUDED.payload',(mid,fact['statement'],fact['quote'],Jsonb(fact.get('payload',{}))))
        conn.execute("UPDATE materials SET status='done',last_error=NULL WHERE id=%s",(mid,))
    from research import synthesize
    for topic in sorted(set(topics+previous_topics)):
        synthesize(conn,topic)

def process_new(conn):
    issues=llm.configuration_issues()
    if issues:
        db.log('worker','Обработка приостановлена: '+'; '.join(issues)+'. Исправьте Environment Variables в Coolify.')
        return
    # Earlier versions exhausted retries even when MODEL or its key was missing.
    conn.execute("UPDATE materials SET attempts=0,status='new',last_error=NULL WHERE status='failed' AND last_error='RuntimeError'")
    pending=conn.execute("SELECT id,title,text,text_scope FROM materials WHERE status IN ('new','failed') AND attempts<3 ORDER BY id LIMIT 30").fetchall()
    if not pending:
        counts=conn.execute("SELECT count(*) FILTER(WHERE status='done'),count(*) FILTER(WHERE status='failed') FROM materials").fetchone()
        db.log('worker',f'Очередь без новых задач: обработано {counts[0]}, ошибок {counts[1]}')
        return
    for row in pending:
        db.log('worker',f'Разбор материала {row[0]}')
        conn.execute('UPDATE materials SET attempts=attempts+1 WHERE id=%s',(row[0],))
        try:
            process_material(conn,row)
            db.log('worker',f'Материал {row[0]}: этап обработки завершён')
        except llm.BudgetExceeded:
            conn.execute('UPDATE materials SET attempts=attempts-1 WHERE id=%s',(row[0],))
            raise
        except Exception as error:
            reason=llm.safe_error(error)
            conn.execute("UPDATE materials SET status='failed',last_error=%s WHERE id=%s",(reason,row[0]))
            db.log('worker',f'Материал {row[0]}: '+reason)

def main():
    from collector.main import collect
    from digest import generate
    db.log('worker','Сервис запущен: сбор и обработка каждые 60 секунд; время ежедневного выпуска '+os.environ.get('DIGEST_HOUR','19')+':00 '+os.environ.get('DIGEST_TIMEZONE','Europe/Moscow'))
    ready=False
    while True:
        try:
            with db.connect() as conn:
                db.init_schema(conn)
                if not ready:
                    db.log('worker','Подключение к БД установлено; MODEL '+('задан' if os.environ.get('MODEL') else 'не задан')+'; ключ модели '+('задан' if os.environ.get('OPENROUTER_API_KEY') else 'не задан')+'; месячный лимит '+os.environ.get('MODEL_MONTHLY_USD','0'))
                    ready=True
                collect(conn)
                try:
                    process_new(conn)
                finally:
                    generate(conn)
        except Exception as error:
            db.log('worker',type(error).__name__)
        time.sleep(60)
