"""Grounded cross-document synthesis and question answering."""
import db
import llm
import re
import hashlib
import json
from datetime import datetime,timedelta,timezone
import relevance
from evidence import validate_claims
from db import jsonb as Jsonb
from net import safe_error, ERROR_REASONS

ANSWER_MESSAGES={
    'completed':'Ответ готов. Проверьте ограничения и первоисточники.',
    'partial':'Ответ получен по части данных. Проверка не завершена.',
    'insufficient_data':'Доступных проверенных данных для ответа недостаточно.',
    'failed':'Проверка не завершена. Попробуйте позже.',
    'needs_clarification':'Нужно уточнить предмет вопроса.',
}


def _save_answer(conn,question,result,owner_telegram_id):
    conn.execute('INSERT INTO answers(question,content,owner_telegram_id) VALUES (%s,%s,%s)',
                 (question,Jsonb(result),owner_telegram_id))
    return result


def answer_state(content):
    """Legacy history cannot claim readiness without actual grounded claims."""
    status=content.get('status')
    has_claims=any(isinstance(c,dict) and isinstance(c.get('text') or c.get('statement'),str)
                   and (c.get('text') or c.get('statement')).strip() for c in content.get('claims',[]))
    if content.get('clarification'):
        return 'needs_clarification'
    if status in ANSWER_MESSAGES:
        if status in {'completed','partial'} and not has_claims:
            return 'insufficient_data'
        return status
    if has_claims: return 'completed'
    if any(re.search(r'Error|Exception|не заверш|недоступ',str(value),re.I) for value in content.get('limitations',[])):
        return 'failed'
    return 'insufficient_data'


def _answer_status(result,collection,processing,errors,facts):
    checks=collection.get('checks',[])
    incomplete=bool(errors or collection.get('failed_sources'))
    incomplete=incomplete or any(c.get('planned',True) and (not c.get('complete',c.get('status') in {'success','empty','no_text'}) or c.get('status')=='partial') for c in checks)
    incomplete=incomplete or any(p.get('status') in {'failed','locked'} or p.get('synthesis_errors') for p in processing)
    if result.get('claims'):
        return 'partial' if incomplete or any(c.get('planned',True) and c.get('status')=='no_text' for c in checks) else 'completed'
    if incomplete:
        return 'failed'
    checked=bool(facts) or any(c.get('planned',True) and c.get('complete',c.get('status') in {'success','empty','no_text'}) for c in checks)
    # Preserve the old collector response contract in isolated older callers.
    checked=checked or bool(collection and 'checks' not in collection and not collection.get('failed_sources'))
    return 'insufficient_data' if checked else 'failed'

def fact_context(conn,query,*,reader_only=False):
    words=[w for w in re.findall(r'[\w-]+',query) if len(w)>1][:12]
    patterns=['%'+w+'%' for w in words] or ['%'+query+'%']
    gate=(' AND '+relevance.reader_condition('m')) if reader_only else ''
    params=(patterns,patterns,patterns)+( (relevance.load_profile()['version'],) if reader_only else () )
    rows=conn.execute("SELECT x.id,x.statement,x.quote,x.payload,m.id,m.title,m.url,m.text_scope,f.kind,m.source_role FROM facts x JOIN materials m ON m.id=x.material_id JOIN findings f ON f.material_id=m.id WHERE x.active AND m.status='done' AND f.verified AND (m.title ILIKE ANY(%s) OR x.statement ILIKE ANY(%s) OR f.topics::text ILIKE ANY(%s))"+gate+" ORDER BY x.id DESC LIMIT 80",params).fetchall()
    from worker.main import bounded_kind
    facts=[]
    for row in rows:
        fact=dict(zip(('id','statement','quote','payload','material_id','title','url','scope','kind','source_role'),row))
        fact.setdefault('source_role','unknown')
        # Old findings may predate deterministic role bounds. Keep the origin
        # restriction at read time as well as during new material processing.
        fact['kind']=bounded_kind(fact['kind'],fact['source_role'])
        facts.append(fact)
    return facts

PROMPT='Составь справочный ответ по-русски только по переданным фактам. Верни {claims:[{text:str,fact_ids:[int]}],limitations:[str],contradictions:[str],missing_data:[str]}. Все содержательные утверждения включая ограничения и противоречия должны быть в claims с fact_ids. Остальные списки только для пробелов и ограничений процесса. Не сравнивай несопоставимые показатели, не считай универсальный процент эффективности. Отделяй отзывы, экспертное мнение и рекламу от научных результатов. Не переноси доказательства ингредиента на продукт. Отмечай аннотации. Неизвестное обозначай. Числа используй только с условиями и сроками. Не назначай лечение.'

SOURCE_RULES=('Сохраняй source_role и вид доступного текста каждого факта. Справка и объяснения пациентам '
    '(reference, professional_information) не являются отдельным исследованием или формальной рекомендацией. '
    'systematic_review_summary — публичное резюме обзора, не полный текст. Оценку достоверности источника '
    'передавай с указанием его авторства; собственные числовые баллы не придумывай. Для широкого вопроса '
    'дай обзор разных задач и протоколов, представленных в фактах, сохраняя их различия и пробелы. '
    'Не переноси результаты между разными веществами, ретиноидами, устройствами, показаниями, группами или сроками.')
PROMPT+=' '+SOURCE_RULES

SEARCH_PROMPT='''Определи предмет исследования и преобразуй вопрос в поисковый запрос на английском,
русские/английские ключевые слова и синонимы. Для продукта сохрани точное название и бренд.
Широкий распознаваемый вопрос исследуй сразу обзором: «что известно о ретиноле?» или
«насколько Фотона эффективная?» означает разные задачи и протоколы, а не обязательное уточнение.
Отсутствие узкой цели, модели устройства или протокола само по себе не требует уточнения.
subject_identified=false только когда предмет невозможно определить, например «этот крем»
без названия, ссылки или других сведений. Тогда задай короткий вопрос clarification по-русски.
При известном предмете subject_identified=true, clarification пустая строка.
Верни {subject_identified:bool,query:str,terms:[str],clarification:str}.'''

def grounded(conn,stage,question,facts,*,activity=None):
    if not facts:
        return {'claims':[],'limitations':['В базе нет проверенных фактов для ответа.'],'contradictions':[],'missing_data':['Недостаточно данных'],'sources':[]}
    result=llm.call(conn,stage,PROMPT,{'question':question,'facts':facts})
    if activity is not None:
        activity['generated']=True
    validate_claims(result,{f['id'] for f in facts})
    if any(not isinstance(claim.get('text'),str) or not claim['text'].strip() for claim in result['claims']):
        from net import SourceError
        raise SourceError('validation_error')
    check=llm.call(conn,stage+'_verify','Проверь каждое утверждение ответа по фактам. Верни {valid:bool}. Отклоняй неподтверждённые числа, ложные прямые сравнения, персональные назначения, выводы из отсутствия данных и необоснованный перенос ингредиент→продукт. '+SOURCE_RULES,{'answer':result,'facts':facts})
    if check.get('valid') is not True:
        raise ValueError('Ответ не прошёл проверку оснований')
    used={i for c in result['claims'] for i in c['fact_ids']}
    result['sources']=[f for f in facts if f['id'] in used]
    return result

def _topic_input(conn,topic):
    profile_version=relevance.load_profile()['version']
    facts=fact_context(conn,topic,reader_only=True)
    ids=sorted({f['material_id'] for f in facts})
    admissions=conn.execute('''SELECT a.material_id,a.profile_version,a.text_version,a.status,
        a.details->>'topic',a.details->>'application',a.details->>'audience'
        FROM material_assessments a JOIN materials m ON m.id=a.material_id
        WHERE a.material_id=ANY(%s) AND a.scope_key='reader' AND a.profile_version=%s
        AND a.text_version=reader_text_version(m.title,m.text,m.text_scope,m.source_role)
        ORDER BY a.material_id''',(ids,profile_version)).fetchall() if ids else []
    signature=hashlib.sha256(json.dumps({'profile':profile_version,'facts':facts,'admissions':admissions},
        ensure_ascii=False,sort_keys=True,default=str).encode('utf-8')).hexdigest()
    return facts,signature,profile_version


def _finish_topic(conn,topic,signature,profile_version,result=None):
    # Queue row is locked only for publication, never around a model/network call.
    with conn.transaction():
        conn.execute('SELECT revision FROM topic_refresh WHERE topic=%s FOR UPDATE',(topic,)).fetchone()
        facts,current_signature,current_profile=_topic_input(conn,topic)
        if current_signature!=signature or current_profile!=profile_version:
            conn.execute("UPDATE topic_refresh SET state='pending',updated_at=now() WHERE topic=%s",(topic,))
            return {'topic':topic,'status':'stale'}
        conn.execute('UPDATE conclusions SET is_current=FALSE WHERE topic=%s AND is_current',(topic,))
        if facts:
            if result is None:
                prior=conn.execute('''SELECT id FROM conclusions WHERE topic=%s AND input_signature=%s
                    AND profile_version=%s ORDER BY version DESC LIMIT 1''',(topic,signature,profile_version)).fetchone()
                if not prior:
                    conn.execute("UPDATE topic_refresh SET success_signature=NULL,state='pending' WHERE topic=%s",(topic,))
                    conn.execute('UPDATE topic_snapshots SET successful=FALSE WHERE topic=%s AND input_signature=%s',
                                 (topic,signature))
                    return {'topic':topic,'status':'pending'}
                conn.execute('UPDATE conclusions SET is_current=TRUE WHERE id=%s',(prior[0],))
            else:
                version=conn.execute('SELECT coalesce(max(version),0)+1 FROM conclusions WHERE topic=%s',(topic,)).fetchone()[0]
                conn.execute('''INSERT INTO conclusions(topic,version,content,reason,input_signature,profile_version,is_current)
                    VALUES (%s,%s,%s,%s,%s,%s,TRUE)''',(topic,version,Jsonb(result),
                    'Проверенные факты и допуск актуальны; предыдущая версия сохранена',signature,profile_version))
        conn.execute("""UPDATE topic_refresh SET done_revision=revision,state='ready',success_signature=%s,
            attempt_signature=%s,attempt_revision=revision,attempts=0,next_retry_at=NULL,last_error=NULL,
            updated_at=now() WHERE topic=%s""",(signature,signature,topic))
        conn.execute('''UPDATE topic_snapshots SET successful=TRUE,attempts=0,next_retry_at=NULL,last_error=NULL
            WHERE topic=%s AND input_signature=%s''',(topic,signature))
    return {'topic':topic,'status':'ready' if result is not None else 'unchanged'}


def synthesize(conn,topic,*,now=None):
    """One deferred public topic update; independent of the answer request path."""
    now=now or datetime.now(timezone.utc)
    conn.execute('INSERT INTO topic_refresh(topic) VALUES (%s) ON CONFLICT DO NOTHING',(topic,))
    locked=conn.execute('SELECT pg_try_advisory_lock(4218,hashtext(%s))',(topic,)).fetchone()[0]
    if not locked:
        return {'topic':topic,'status':'locked'}
    try:
        row=conn.execute('''SELECT revision,success_signature
            FROM topic_refresh WHERE topic=%s''',(topic,)).fetchone()
        revision,success=row
        facts,signature,profile_version=_topic_input(conn,topic)
        conn.execute('''INSERT INTO topic_snapshots(topic,input_signature,profile_version)
            VALUES (%s,%s,%s) ON CONFLICT DO NOTHING''',(topic,signature,profile_version))
        attempts,next_retry,successful,last_error=conn.execute('''SELECT attempts,next_retry_at,successful,last_error
            FROM topic_snapshots WHERE topic=%s AND input_signature=%s''',(topic,signature)).fetchone()
        if success==signature or successful or not facts:
            return _finish_topic(conn,topic,signature,profile_version)
        if attempts>=3:
            conn.execute("""UPDATE topic_refresh SET state='failed',attempt_signature=%s,
                attempt_revision=revision,attempts=%s,next_retry_at=%s,last_error=%s,updated_at=now() WHERE topic=%s""",
                (signature,attempts,next_retry,Jsonb(last_error) if last_error else None,topic))
            return {'topic':topic,'status':'failed','attempts':attempts}
        if next_retry and next_retry>now:
            conn.execute("""UPDATE topic_refresh SET attempt_signature=%s,attempt_revision=revision,
                attempts=%s,next_retry_at=%s,last_error=%s,state='pending' WHERE topic=%s""",
                (signature,attempts,next_retry,Jsonb(last_error) if last_error else None,topic))
            return {'topic':topic,'status':'pending','attempts':attempts}
        issues=llm.configuration_issues()
        if issues:
            from net import SourceError
            budget_only=all(('MODEL_MONTHLY_USD' in issue or 'MODEL_CALL_RESERVE_USD' in issue)
                            and 'больше 0' in issue for issue in issues)
            if budget_only:
                from decimal import Decimal
                import os
                for name,default in (('MODEL_MONTHLY_USD','0'),('MODEL_CALL_RESERVE_USD','1')):
                    if any(name in issue for issue in issues):
                        value=Decimal(os.environ.get(name,default))
                        if not value.is_finite() or value>0:
                            budget_only=False
            info=safe_error(llm.BudgetExceeded() if budget_only else SourceError('configuration_error'),'synthesis')
            conn.execute("""UPDATE topic_refresh SET state='pending',attempt_signature=%s,
                attempt_revision=revision,attempts=%s,next_retry_at=NULL,last_error=%s WHERE topic=%s""",
                (signature,attempts,Jsonb(info),topic))
            return {'topic':topic,'status':'budget_blocked' if budget_only else 'configuration_blocked',
                    'attempts':attempts,'error':info}
        with conn.transaction():
            conn.execute('''UPDATE topic_snapshots SET attempts=%s,next_retry_at=%s
                WHERE topic=%s AND input_signature=%s''',
                (attempts+1,now+timedelta(minutes=15),topic,signature))
            conn.execute("""UPDATE topic_refresh SET state='running',attempt_signature=%s,attempt_revision=%s,
                attempts=%s,next_retry_at=%s,updated_at=now() WHERE topic=%s""",
                (signature,revision,attempts+1,now+timedelta(minutes=15),topic))
        activity={}
        try:
            result=grounded(conn,'synthesis',topic,facts,activity=activity)
        except Exception as error:
            budget=isinstance(error,llm.BudgetExceeded)
            # The first call may be denied before HTTP. A denied verification
            # after successful generation has already consumed a paid attempt.
            info=safe_error(error,'synthesis')
            preflight=budget or info['code']=='configuration_error'
            consumed=not preflight or bool(activity.get('generated'))
            used=attempts+(1 if consumed else 0)
            retry_at=now+timedelta(minutes=15) if consumed else None
            with conn.transaction():
                conn.execute('''UPDATE topic_snapshots SET attempts=%s,next_retry_at=%s,last_error=%s
                    WHERE topic=%s AND input_signature=%s''',(used,retry_at,Jsonb(info),topic,signature))
                conn.execute("""UPDATE topic_refresh SET state=%s,attempts=%s,next_retry_at=%s,last_error=%s,
                    updated_at=now() WHERE topic=%s""",
                    ('failed' if used>=3 else 'pending',used,retry_at,Jsonb(info),topic))
            return {'topic':topic,'status':'budget_blocked' if budget else 'failed',
                    'attempts':used,'error':info}
        return _finish_topic(conn,topic,signature,profile_version,result)
    finally:
        conn.execute('SELECT pg_advisory_unlock(4218,hashtext(%s))',(topic,))


def refresh_topics(conn,now=None,limit=30):
    now=now or datetime.now(timezone.utc)
    rows=conn.execute("""SELECT topic FROM topic_refresh WHERE state IN ('pending','running')
        AND (next_retry_at IS NULL OR next_retry_at<=%s OR revision>attempt_revision)
        ORDER BY updated_at,topic LIMIT %s""",(now,min(max(int(limit),1),30))).fetchall()
    reports=[]
    for (topic,) in rows:
        try:
            reports.append(synthesize(conn,topic,now=now))
        except Exception as error:
            reports.append({'topic':topic,'status':'failed','error':safe_error(error,'synthesis')})
    return reports

def answer(conn,question,owner_telegram_id=None):
    if not isinstance(question,str) or not question.strip() or len(question)>2000:
        raise ValueError('Вопрос должен содержать от 1 до 2000 символов')
    from collector.main import search
    from worker.main import process_material
    from collector.exa import question_scope
    question=question.strip()
    # Choose once, from the ORIGINAL question and verified caller identity.
    # All calls/retries inside this research reuse it, even across UTC month-end.
    # Conservative grouping of identical owner/questions within a month remains
    # intentional until a separately specified request-idempotency flow exists.
    scope_id=question_scope(question,owner_telegram_id)
    errors=[]
    try:
        facts=fact_context(conn,question)
    except Exception as error:
        facts=[]
        errors.append(safe_error(error,'processing'))
    status=[]
    collection={}
    processing=[]
    clarification_result=None
    phase='planning'
    lookup=question
    if len(facts)<4:
        try:
            plan=llm.call(conn,'search_plan',SEARCH_PROMPT,{'question':question})
            if not isinstance(plan,dict):
                raise ValueError('Некорректный план поиска')
            identified=plan.get('subject_identified')
            if identified is False or (identified is None and plan.get('clarification')):
                clarification=plan.get('clarification')
                if not isinstance(clarification,str) or not clarification.strip():
                    clarification='Укажите название продукта, процедуры или устройства либо пришлите ссылку.'
                # Keep clarification in private history too, without importing
                # a separate continuation workflow or inventing a ready answer.
                first=next((c.lower() for c in clarification if c.isalpha()),'')
                if not ('а'<=first<='я' or first=='ё'):
                    clarification='Укажите название продукта, процедуры или устройства либо пришлите ссылку.'
                clarification_result={'claims':[],'clarification':clarification.strip()[:2000],
                        'limitations':[],'sources':[],'scope_id':scope_id,'source_checks':[],'processing':[],
                        'status':'needs_clarification','message':ANSWER_MESSAGES['needs_clarification']}
                return _save_answer(conn,question,clarification_result,owner_telegram_id)
            if identified is not None and identified is not True:
                raise ValueError('Некорректный предмет поиска')
            query=plan.get('query')
            if not isinstance(query,str) or not query.strip() or not isinstance(plan.get('terms',[]),list):
                raise ValueError('Некорректный план поиска')
            query=query.strip()[:1000]
            terms=[t.strip() for t in plan.get('terms',[]) if isinstance(t,str) and t.strip()][:12]
            # fact_context bounds words: put identified subject before the broad question.
            lookup=' '.join(terms+[query,question])
            phase='search'
            collection=search(conn,query,scope_id=scope_id,owner_telegram_id=owner_telegram_id)
            if collection.get('failed_sources'):
                status.append('Часть источников недоступна: '+', '.join(collection['failed_sources']))
            patterns=['%'+t+'%' for t in terms] or ['%'+question+'%']
            if 'material_ids' in collection:
                ids=list(dict.fromkeys(i for i in collection['material_ids'] if type(i) is int and i>0))[:8]
                rows=conn.execute("""SELECT m.id,m.title,m.text,m.text_scope FROM materials m
                    LEFT JOIN findings f ON f.material_id=m.id WHERE m.id=ANY(%s)
                    AND m.attempts<3 AND (m.status IN ('new','failed') OR
                        (m.status='done' AND NOT coalesce(f.verified,FALSE)))
                    ORDER BY array_position(%s::integer[],m.id) LIMIT 8""",(ids,ids)).fetchall() if ids else []
            else:
                # Compatibility only for callers/tests predating collector material IDs.
                rows=conn.execute("SELECT m.id,m.title,m.text,m.text_scope FROM materials m LEFT JOIN findings f ON f.material_id=m.id WHERE (m.status='new' OR (m.status='done' AND NOT coalesce(f.verified,FALSE))) AND (m.title ILIKE ANY(%s) OR f.summary ILIKE ANY(%s)) ORDER BY m.id DESC LIMIT 8",(patterns,patterns)).fetchall()
            phase='processing'
            for row in rows[:8]:
                try:
                    conn.execute('UPDATE materials SET attempts=attempts+1 WHERE id=%s',(row[0],))
                    report=process_material(conn,row,force=True) or {}
                    processing.append(dict(report,material_id=row[0]))
                    if report.get('status')=='locked':
                        conn.execute('UPDATE materials SET attempts=attempts-1 WHERE id=%s',(row[0],))
                    if report.get('synthesis_errors'):
                        status.append('Проверенные факты сохранены; синтез по части тем не завершён.')
                except llm.BudgetExceeded:
                    conn.execute('UPDATE materials SET attempts=attempts-1 WHERE id=%s',(row[0],))
                    processing.append(dict(safe_error(llm.BudgetExceeded(),'processing'),material_id=row[0],status='failed'))
                    status.append('Анализ части материалов не завершён: лимит модели исчерпан.')
                    break
                except Exception as error:
                    info=safe_error(error,'processing')
                    reason=info['reason']
                    processing.append(dict(info,material_id=row[0],status='failed'))
                    status.append('Один из материалов не прошёл анализ или проверку оснований.')
                    # A post-commit failure must not hide a successfully completed material.
                    conn.execute("""UPDATE materials SET status='failed',last_error=%s WHERE id=%s
                        AND status<>'done' AND text IS NOT DISTINCT FROM %s AND text_scope=%s""",
                        (reason,row[0],row[2],row[3]))
            if 'material_ids' in collection and collection['material_ids']:
                pending=conn.execute("SELECT id,status FROM materials WHERE id=ANY(%s) AND status IN ('new','failed')",
                                     (collection['material_ids'],)).fetchall()
                if pending:
                    errors.append({'code':'processing_error','stage':'processing',
                                   'reason':'Не все найденные материалы обработаны в пределах проверки'})
        except Exception as error:
            info=safe_error(error,phase)
            errors.append(info)
            status.append(info['reason'])
        finally:
            # Includes successes from earlier documents and facts saved before
            # a later synthesis failure. Reread BOTH original and expanded query:
            # translated terms must not displace an existing Russian subject.
            # Never merge the stale initial snapshot: those facts may be retired.
            if clarification_result is None:
                current=[]
                seen=set()
                try:
                    for current_query in dict.fromkeys((question,lookup)):
                        for fact in fact_context(conn,current_query):
                            if fact['id'] not in seen:
                                current.append(fact)
                                seen.add(fact['id'])
                except Exception as error:
                    errors.append(safe_error(error,'processing'))
                    current=[]
                facts=current[:80]
    try:
        result=grounded(conn,'answer',question,facts)
    except Exception as error:
        errors.append(safe_error(error,'answer'))
        # A rejected model output is never published, even as a partial answer.
        result={'claims':[],'limitations':[],'contradictions':[],'missing_data':[],'sources':[]}
    result.setdefault('limitations',[]).extend(status)
    result['scope_id']=scope_id
    result['source_checks']=collection.get('checks',[])
    result['processing']=processing
    result['errors']=errors
    result['status']=_answer_status(result,collection,processing,errors,facts)
    result['message']=ANSWER_MESSAGES[result['status']]
    if errors:
        result['limitations'].extend(info['reason'] for info in errors if info['reason'] not in result['limitations'])
    return _save_answer(conn,question,result,owner_telegram_id)
