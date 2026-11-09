"""Grounded cross-document synthesis and question answering."""
import db
import llm
import re
import hashlib
import json
from datetime import datetime,timedelta,timezone
import relevance
from evidence import split_claims, verified_checks
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
        return 'partial' if incomplete or result.get('grounding_partial') or any(c.get('planned',True) and c.get('status')=='no_text' for c in checks) else 'completed'
    if incomplete:
        return 'failed'
    checked=bool(facts) or any(c.get('planned',True) and c.get('complete',c.get('status') in {'success','empty','no_text'}) for c in checks)
    # Preserve the old collector response contract in isolated older callers.
    checked=checked or bool(collection and 'checks' not in collection and not collection.get('failed_sources'))
    return 'insufficient_data' if checked else 'failed'

STOPWORDS=set('что от и или как про для это этот эта эти чем помогает помогают работает работают работает ли насколько эффективность расскажи расскажите известно есть мне пожалуйста по на в с о об а до после без при такой такая какие какой кожа кожи кожей лицо лица лице лицу шея шеи уход ухода применение применения данные исследований исследование why what how does do the a an and or of for with about is are help helps efficacy effective effectiveness tell me know study evidence facial face neck skin care'.split())
CONCEPTS={
    'retinol': ('retinol', 'ретинол'),
    'retinal': ('retinal', 'ретиналь'),
    'pigmentation': ('pigment', 'пигмент', 'melasma', 'мелазм', 'dark spots', 'пятен', 'пятна', 'пятно'),
    'fotona': ('fotona', 'фотона', 'фотон'),
    'radiofrequency': ('radiofrequency', 'радиочастот', 'rf'),
    'niacinamide': ('niacinamide', 'ниацинамид'),
    'vitamin_c': ('vitamin c', 'витамин c', 'витамин с', 'ascorb', 'аскорбин'),
    'acne': ('acne', 'акне', 'угре'),
    'aging': ('photoaging', 'wrinkl', 'aging', 'морщин', 'фотостарен', 'старен'),
    'irritation': ('irritat', 'раздражен'),
    'moisturizing': ('moistur', 'hydration', 'увлажнен'),
    'sunscreen': ('sunscreen', 'spf', 'солнцезащит'),
    'rosacea': ('rosacea', 'розаце'),
    'pores': ('pores', 'поры', 'пор '),
    'vaginal': ('vaginal', 'влагалищ'),
    'dryness': ('dryness', 'сухост'),
}


def _has_term(text, term):
    return bool(re.search(r'(?<!\w)'+re.escape(term)+(r'(?!\w)' if term=='rf' else ''), text, re.I))


def _query_groups(query):
    text=relevance.normalized(query).lower()
    groups=[terms for terms in CONCEPTS.values() if any(_has_term(text,t) for t in terms)]
    covered={word for group in groups for term in group if _has_term(text,term)
             for word in re.findall(r'[\w-]+',term)}
    for word in dict.fromkeys(re.findall(r'[\w-]+',text)):
        if (word in STOPWORDS or word in covered or word.startswith('эффектив')
                or len(word)<3 or any(_has_term(word,t) for group in groups for t in group)):
            continue
        groups.append((word,))
    return groups[:12]


def _query_patterns(query):
    return ['%'+term.replace('%','').replace('_','')+'%' for group in _query_groups(query) for term in group]


def _subject_matches(fact, question):
    text=' '.join(str(fact.get(k,'')) for k in ('title','statement','quote')).lower()
    return [any(_has_term(text,term) for term in group) for group in _query_groups(question)]


def _facts_sufficient(facts, question):
    groups=_query_groups(question)
    if not groups or re.search(r'\b(?:этот|эта|это|this|that)\b',question,re.I):
        return False
    unique={fact['id']:fact for fact in facts if any(_subject_matches(fact,question))}
    return len(unique)>=4 and all(any(_subject_matches(f,question)[i] for f in unique.values()) for i in range(len(groups)))


def fact_context(conn,query,*,reader_only=False,owner_telegram_id=None,question=None):
    patterns=_query_patterns(query)
    if not patterns:
        return []
    version=relevance.load_profile()['version']
    gate=relevance.reader_condition('m')
    params=(patterns,patterns,patterns,version)
    if not reader_only and owner_telegram_id is not None and question:
        private=relevance.scope_key('question',owner_telegram_id,question)
        gate='('+gate+''' OR EXISTS(SELECT 1 FROM material_assessments pa WHERE pa.material_id=m.id
            AND pa.profile_version=%s AND pa.scope_key=%s AND pa.status='eligible'
            AND pa.text_version=reader_text_version(m.title,m.text,m.text_scope,m.source_role)))'''
        params+=(version,private)
    search_column='f.topics::text' if reader_only else 'x.quote'
    rows=conn.execute("SELECT x.id,x.statement,x.quote,x.payload,m.id,m.title,m.url,m.text_scope,f.kind,m.source_role FROM facts x JOIN materials m ON m.id=x.material_id JOIN findings f ON f.material_id=m.id WHERE x.active AND m.status='done' AND f.verified AND (reader_casefold(m.title) LIKE ANY(%s) OR reader_casefold(x.statement) LIKE ANY(%s) OR reader_casefold("+search_column+") LIKE ANY(%s)) AND "+gate+" ORDER BY x.id DESC LIMIT 80",params).fetchall()
    from worker.main import bounded_kind
    facts=[]
    for row in rows:
        fact=dict(zip(('id','statement','quote','payload','material_id','title','url','scope','kind','source_role'),row))
        fact.setdefault('source_role','unknown')
        # Old findings may predate deterministic role bounds. Keep the origin
        # restriction at read time as well as during new material processing.
        fact['kind']=bounded_kind(fact['kind'],fact['source_role'])
        if reader_only or any(_subject_matches(fact, query)):
            facts.append(fact)
    return facts

PROMPT='Составь справочный ответ по-русски только по переданным фактам. Верни {claims:[{text:str,fact_ids:[int]}],limitations:[str],contradictions:[str],missing_data:[str]}. Все содержательные утверждения включая ограничения и противоречия должны быть в claims с fact_ids. Остальные списки только для пробелов и ограничений процесса. Не сравнивай несопоставимые показатели, не считай универсальный процент эффективности. Отделяй отзывы, экспертное мнение и рекламу от научных результатов. Не переноси доказательства ингредиента на продукт. Отмечай аннотации. Неизвестное обозначай. Числа используй только с условиями и сроками. Не назначай лечение.'

SOURCE_RULES=('Сохраняй source_role и вид доступного текста каждого факта. Справка и объяснения пациентам '
    '(reference, professional_information) не являются отдельным исследованием или формальной рекомендацией. '
    'systematic_review_summary — публичное резюме обзора, не полный текст. official_safety — '
    'официальное сообщение о безопасности, не клиническое испытание. Оценку достоверности источника '
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
    raw=llm.call(conn,stage,PROMPT,{'question':question,'facts':facts})
    if activity is not None:
        activity['generated']=True
    claims,indices,checks=split_claims(raw,{f['id'] for f in facts})
    if not raw['claims'] and stage=='answer':
        # Abstention contains no rejected assertion. Never publish raw notes as
        # an alternative route for unverified advice or an absence-of-effect claim.
        return {'claims':[],'limitations':[], 'contradictions':[],
                'missing_data':['Предоставленных фактов недостаточно для обоснованного ответа.'],
                'sources':[],'claim_checks':[],'claim_sources':[]}
    allowed={f['id']:f for f in facts}
    claim_sources=[]
    for index,claim in enumerate(raw['claims']):
        ids=claim.get('fact_ids') if isinstance(claim,dict) else None
        valid_ids=list(dict.fromkeys(i for i in ids if type(i) is int and i in allowed)) if isinstance(ids,list) else []
        mids=sorted({allowed[i]['material_id'] for i in valid_ids if type(allowed[i].get('material_id')) is int})
        claim_sources.append({'index':index,'fact_ids':valid_ids,'material_ids':mids})
    result={'claims':claims}
    for key in ('limitations','contradictions','missing_data'):
        values=raw.get(key,[])
        if not isinstance(values,list) or any(not isinstance(value,str) for value in values):
            from net import SourceError
            raise SourceError('validation_error')
        result[key]=values
    if claims:
        check=llm.call(conn,stage+'_verify',
            'Проверь КАЖДОЕ утверждение только по указанным в нём fact_ids. Индексы с нуля. '
            'Верни {claims:[{index:int,valid:bool,code:str}],notes_valid:bool}, ровно один результат на индекс. '
            'code=supported только при valid=true; иначе unsupported, wrong_subject, wrong_population, '
            'wrong_protocol, unsupported_number или personal_advice. Проверяй предмет, популяцию, протокол, '
            'числа/сроки, назначения и перенос ингредиент→продукт. notes_valid=false, если любой текст '
            'в limitations/contradictions/missing_data содержит клиническое утверждение, неподтверждённый '
            'вывод или назначение. Эти списки допустимы только для пробелов и ограничений процесса. '+SOURCE_RULES,
            {'answer':result,'facts':facts})
        try:
            verdicts,notes_valid=verified_checks(check,len(claims))
        except ValueError as error:
            error.claim_checks=sorted(checks+[
                {'index':index,'valid':False,'code':'verification_format'} for index in indices],
                key=lambda c:c['index'])
            error.claim_sources=claim_sources
            raise
        result['claims']=[claim for i,claim in enumerate(claims) if verdicts[i]['valid']]
        checks.extend({'index':indices[i],'valid':verdicts[i]['valid'],'code':verdicts[i]['code']} for i in range(len(claims)))
    else:
        notes_valid=False
    if not notes_valid:
        for key in ('limitations','contradictions','missing_data'):
            result[key]=[]
        result['limitations']=['Непроверенные примечания исключены из ответа.']
    result['claim_checks']=sorted(checks,key=lambda c:c['index'])
    result['claim_sources']=claim_sources
    result['grounding_partial']=any(not c['valid'] for c in checks) or not notes_valid
    if not result['claims']:
        from net import SourceError
        error=SourceError('grounding_rejected')
        error.claim_checks=result['claim_checks']
        error.claim_sources=claim_sources
        raise error
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

def _answer_facts(conn, question, lookup, owner_telegram_id):
    current={}
    for query in dict.fromkeys((question,lookup)):
        for fact in fact_context(conn,query,owner_telegram_id=owner_telegram_id,question=question):
            current[fact['id']]=fact
    return list(current.values())[:80]


def _review_existing(conn, lookup, question, owner_telegram_id, reviewed):
    """Targeted readmission of already verified facts; never re-extract them."""
    remaining=min(int(relevance.load_profile()['legacy_per_question']),8)-len(reviewed)
    patterns=_query_patterns(lookup)
    if remaining<=0 or not patterns or re.search(r'\b(?:этот|эта|это|this|that)\b',question,re.I):
        return []
    purpose='question' if owner_telegram_id is not None else 'reader'
    scope=relevance.scope_key(purpose,owner_telegram_id,question)
    version=relevance.load_profile()['version']
    rows=conn.execute('''SELECT m.id,m.title,m.text,m.text_scope,m.source_role,m.url FROM materials m
        JOIN findings f ON f.material_id=m.id WHERE m.status='done' AND f.verified
        AND NOT(m.id=ANY(%s)) AND EXISTS(SELECT 1 FROM facts x WHERE x.material_id=m.id
            AND x.active AND (reader_casefold(m.title) LIKE ANY(%s) OR
                reader_casefold(x.statement) LIKE ANY(%s) OR reader_casefold(x.quote) LIKE ANY(%s)))
        AND NOT EXISTS(SELECT 1 FROM material_assessments a WHERE a.material_id=m.id
            AND a.profile_version=%s AND a.text_version=reader_text_version(m.title,m.text,m.text_scope,m.source_role)
            AND ((a.scope_key='reader' AND a.status='eligible') OR
                 (a.scope_key=%s AND a.status<>'needs_review')))
        ORDER BY m.id DESC LIMIT %s''',
        (list(reviewed),patterns,patterns,patterns,version,scope,remaining)).fetchall()
    reports=[]
    for row in rows:
        reviewed.add(row[0])
        assessment=relevance.ensure_assessment(conn,relevance.material_from_row(row),purpose=purpose,
            owner_telegram_id=owner_telegram_id,question=question)
        reports.append({'material_id':row[0],'status':'reviewed','assessment':assessment})
        # Do not spend remaining reviews if enough existing facts are now usable.
        if _facts_sufficient(_answer_facts(conn,question,lookup,owner_telegram_id),question):
            break
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
        facts=_answer_facts(conn,question,question,owner_telegram_id)
    except Exception as error:
        facts=[]
        errors.append(safe_error(error,'processing'))
    status=[]
    collection={}
    processing=[]
    clarification_result=None
    phase='planning'
    lookup=question
    reviewed=set()
    if not _facts_sufficient(facts,question):
        try:
            processing.extend(_review_existing(conn,question,question,owner_telegram_id,reviewed))
            facts=_answer_facts(conn,question,question,owner_telegram_id)
        except Exception as error:
            errors.append(safe_error(error,'processing'))
    if not _facts_sufficient(facts,question):
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
            phase='processing'
            processing.extend(_review_existing(conn,lookup,question,owner_telegram_id,reviewed))
            facts=_answer_facts(conn,question,lookup,owner_telegram_id)
            phase='search'
            collection=({} if _facts_sufficient(facts,question) else
                        search(conn,query,scope_id=scope_id,owner_telegram_id=owner_telegram_id))
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
                if _facts_sufficient(facts,question):
                    break
                try:
                    conn.execute('UPDATE materials SET attempts=attempts+1 WHERE id=%s',(row[0],))
                    report=process_material(conn,row,force=True,
                        purpose='question' if owner_telegram_id is not None else 'reader',
                        owner_telegram_id=owner_telegram_id,question=question) or {}
                    processing.append(dict(report,material_id=row[0]))
                    if report.get('status')=='locked':
                        conn.execute('UPDATE materials SET attempts=attempts-1 WHERE id=%s',(row[0],))
                    if report.get('synthesis_errors'):
                        status.append('Проверенные факты сохранены; синтез по части тем не завершён.')
                    facts=_answer_facts(conn,question,lookup,owner_telegram_id)
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
            if not _facts_sufficient(facts,question) and 'material_ids' in collection and collection['material_ids']:
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
                try:
                    facts=_answer_facts(conn,question,lookup,owner_telegram_id)
                except Exception as error:
                    errors.append(safe_error(error,'processing'))
                    facts=[]
    try:
        result=grounded(conn,'answer',question,facts)
    except Exception as error:
        info=safe_error(error,'answer')
        if getattr(error,'claim_checks',None) is not None:
            info['claim_checks']=error.claim_checks
            info['claim_sources']=error.claim_sources
        errors.append(info)
        # A rejected model output is never published, even as a partial answer.
        result={'claims':[],'limitations':[],'contradictions':[],'missing_data':[],'sources':[]}
    # Admission failures must remain technical failures, even when a later
    # source check legitimately finishes empty. Uncertainty alone is not error.
    for report in processing:
        assessment=report.get('assessment') or {}
        for info in list(report.get('errors',[]))+list(assessment.get('errors',[])):
            if isinstance(info,dict) and isinstance(info.get('code'),str) and info['code'] in ERROR_REASONS:
                error={'code':info['code'],'reason':ERROR_REASONS[info['code']],'stage':'processing'}
                if type(report.get('material_id')) is int:
                    error['material_id']=report['material_id']
                if error not in errors:
                    errors.append(error)
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
