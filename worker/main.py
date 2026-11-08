"""Separate triage, extraction and verification; bounded retry with atomic writes."""
import json
import os
import time
import db
import llm
from evidence import validate_facts
from db import jsonb as Jsonb
from net import safe_error

KINDS={'research','review','guideline','regulatory','news','commercial','expert','experience','product'}

SOURCE_RULES = ('Учитывай source_role: reference и professional_information — справка и объяснения для пациентов, '
    'а не отдельное исследование или формальная клиническая рекомендация; systematic_review_summary — '
    'публичное резюме обзора, не полный текст. Оценки достоверности источника сохраняй с его авторством, '
    'не создавай собственных числовых баллов. Не переноси результаты между веществами, ретиноидами, '
    'устройствами, показаниями, группами, протоколами или сроками без явно указанных оснований.')


def bounded_kind(kind, source_role):
    if source_role in {'reference','professional_information'}:
        return 'expert'
    if source_role in {'systematic_review','systematic_review_summary'}:
        return 'review'
    return kind


def _synthesize_topics(conn, topics):
    """Facts are already committed. A later synthesis failure cannot retire them."""
    from research import synthesize
    errors=[]
    for topic in sorted(set(topics)):
        try:
            synthesize(conn,topic)
        except Exception as error:
            errors.append(safe_error(error,'synthesis'))
    return errors

def process_material(conn,row,force=False):
    mid=row[0]
    locked=conn.execute('SELECT pg_try_advisory_lock(%s,%s)',(42,mid)).fetchone()[0]
    if not locked:
        return {'status':'locked','facts_saved':0,'synthesis_errors':[]}
    try:
        current=conn.execute('SELECT id,title,text,text_scope,source_role FROM materials WHERE id=%s',(mid,)).fetchone()
        report=_process_material(conn,current or row,force)
        if report.get('synthesis_errors'):
            conn.execute("UPDATE materials SET last_error=%s WHERE id=%s AND status IN ('done','skipped')",
                         (json.dumps(report['synthesis_errors'],ensure_ascii=False),mid))
        return report
    finally:
        conn.execute('SELECT pg_advisory_unlock(%s,%s)',(42,mid))

def _process_material(conn,row,force=False):
    mid,title,text,scope=row[:4]
    source_role=row[4] if len(row)>4 else 'unknown'
    prior=conn.execute('SELECT topics FROM findings WHERE material_id=%s',(mid,)).fetchone()
    previous_topics=prior[0] if prior else []
    if scope=='metadata' or not isinstance(text,str) or not text.strip():
        with conn.transaction():
            current=conn.execute('SELECT text,text_scope FROM materials WHERE id=%s FOR UPDATE',(mid,)).fetchone()
            if not current or current[0]!=text or current[1]!=scope:
                raise ValueError('Текст изменился во время обработки')
            conn.execute("UPDATE materials SET status='no_text',last_error=NULL WHERE id=%s",(mid,))
            conn.execute('UPDATE facts SET active=FALSE WHERE material_id=%s',(mid,))
            conn.execute('UPDATE findings SET verified=FALSE WHERE material_id=%s',(mid,))
        return {'status':'no_text','facts_saved':0,'synthesis_errors':[]}
    triage=llm.call(conn,'triage','Оцени релевантность косметологии и уходу. Верни {relevant:bool, important:bool, kind:str, summary:str, topics:[str]}. kind: research/review/guideline/regulatory/news/commercial/expert/experience/product. summary — краткий пересказ по-русски; topics — до 5 устойчивых русских названий тем. '+SOURCE_RULES,{'title':title,'text':text[:12000],'scope':scope,'source_role':source_role})
    if type(triage.get('relevant')) is not bool:
        raise ValueError('Некорректный отбор')
    if not triage['relevant']:
        with conn.transaction():
            conn.execute("UPDATE materials SET status='skipped',last_error=NULL WHERE id=%s",(mid,))
            conn.execute('UPDATE facts SET active=FALSE WHERE material_id=%s',(mid,))
        return {'status':'skipped','facts_saved':0,'synthesis_errors':_synthesize_topics(conn,previous_topics)}
    if triage.get('kind') not in KINDS or not isinstance(triage.get('summary'),str) or not isinstance(triage.get('topics'),list):
        raise ValueError('Некорректная классификация')
    triage['kind']=bounded_kind(triage['kind'],source_role)
    topics=list(dict.fromkeys(t.strip() for t in triage['topics'] if isinstance(t,str) and t.strip()))[:5]
    limitations=['Изучена только аннотация'] if scope=='abstract' else ['Изучен доступный фрагмент'] if scope=='excerpt' else []
    if source_role in {'reference','professional_information'}:
        limitations.append('Справочный материал; не отдельное исследование или формальная рекомендация')
    if source_role=='systematic_review_summary':
        limitations.append('Публичное резюме обзора; полный текст не изучен')
    details={'text_scope':scope,'source_role':source_role,'limitations':limitations,'facts':[]}
    deep=force or triage.get('important') is True
    if deep:
        for offset in range(0,len(text),18000):
            chunk=text[offset:offset+18000]
            extracted=llm.call(conn,'extract','Извлеки только явно указанные данные. Верни {facts:[{statement:str,quote:str,payload:{}}],limitations:[str]}. quote — дословный фрагмент. В payload укажи доступные дизайн, выборку, протокол, сравнение, показатель, число, единицу, сроки, риски, финансирование. Неизвестное не заполняй. Отдельно различай данные ингредиента и готового продукта; отзывы не доказательство эффективности. '+SOURCE_RULES,{'title':title,'text':chunk,'scope':scope,'source_role':source_role})
            facts=validate_facts(extracted.get('facts',[]),chunk)
            checked=llm.call(conn,'verify','Проверь, следует ли каждое statement из quote в данном контексте. Верни {accepted_indices:[int],limitations:[str]}. Индексы с нуля. Отклоняй подмену протокола, чисел, группы, срока, причинности, перенос эффекта ингредиента на продукт. '+SOURCE_RULES,{'text':chunk,'facts':facts,'source_role':source_role})
            indices=checked.get('accepted_indices')
            if not isinstance(indices,list) or any(type(i) is not int or i<0 or i>=len(facts) for i in indices):
                raise ValueError('Некорректный результат проверки')
            details['facts'].extend(facts[i] for i in sorted(set(indices)))
            details['limitations'].extend(str(x) for x in extracted.get('limitations',[]))
            details['limitations'].extend(str(x) for x in checked.get('limitations',[]))
    # The same quote can occur in overlapping source sections. Report stored
    # unique facts, matching the database's material/statement/quote identity.
    details['facts']=list({(fact['statement'],fact['quote']):fact for fact in details['facts']}.values())
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
    return {'status':'processed','facts_saved':len(details['facts']),
            'synthesis_errors':_synthesize_topics(conn,topics+previous_topics)}

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
            report=process_material(conn,row)
            if report and report.get('status')=='locked':
                conn.execute('UPDATE materials SET attempts=attempts-1 WHERE id=%s',(row[0],))
            if report and report.get('synthesis_errors'):
                db.log('worker',f'Материал {row[0]}: факты сохранены, синтез не завершён')
            else:
                db.log('worker',f'Материал {row[0]}: этап обработки завершён')
        except llm.BudgetExceeded:
            conn.execute('UPDATE materials SET attempts=attempts-1 WHERE id=%s',(row[0],))
            raise
        except Exception as error:
            reason=json.dumps(safe_error(error,'processing'),ensure_ascii=False)
            conn.execute("""UPDATE materials SET status='failed',last_error=%s WHERE id=%s
                AND status<>'done' AND text IS NOT DISTINCT FROM %s AND text_scope=%s""",
                (reason,row[0],row[2],row[3]))
            db.log('worker',f'Материал {row[0]}: '+reason)

def main():
    from collector.main import collect,run_daily_search
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
                run_daily_search(conn)
                try:
                    process_new(conn)
                finally:
                    generate(conn)
        except Exception as error:
            db.log('worker',type(error).__name__)
        time.sleep(60)
