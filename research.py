"""Grounded cross-document synthesis and question answering."""
import db
import llm
import re
from evidence import validate_claims
from db import jsonb as Jsonb

def fact_context(conn,query):
    words=[w for w in re.findall(r'[\w-]+',query) if len(w)>1][:12]
    patterns=['%'+w+'%' for w in words] or ['%'+query+'%']
    rows=conn.execute("SELECT x.id,x.statement,x.quote,x.payload,m.id,m.title,m.url,m.text_scope,f.kind,m.source_role FROM facts x JOIN materials m ON m.id=x.material_id JOIN findings f ON f.material_id=m.id WHERE x.active AND m.status='done' AND f.verified AND (m.title ILIKE ANY(%s) OR x.statement ILIKE ANY(%s) OR f.topics::text ILIKE ANY(%s)) ORDER BY x.id DESC LIMIT 80",(patterns,patterns,patterns)).fetchall()
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

def grounded(conn,stage,question,facts):
    if not facts:
        return {'claims':[],'limitations':['В базе нет проверенных фактов для ответа.'],'contradictions':[],'missing_data':['Недостаточно данных'],'sources':[]}
    result=llm.call(conn,stage,PROMPT,{'question':question,'facts':facts})
    validate_claims(result,{f['id'] for f in facts})
    check=llm.call(conn,stage+'_verify','Проверь каждое утверждение ответа по фактам. Верни {valid:bool}. Отклоняй неподтверждённые числа, ложные прямые сравнения, персональные назначения, выводы из отсутствия данных и необоснованный перенос ингредиент→продукт. '+SOURCE_RULES,{'answer':result,'facts':facts})
    if check.get('valid') is not True:
        raise ValueError('Ответ не прошёл проверку оснований')
    used={i for c in result['claims'] for i in c['fact_ids']}
    result['sources']=[f for f in facts if f['id'] in used]
    return result

def synthesize(conn,topic):
    facts=fact_context(conn,topic)
    result=grounded(conn,'synthesis',topic,facts)
    with conn.transaction():
        conn.execute('SELECT pg_advisory_xact_lock(hashtext(%s))',('topic:'+topic,))
        old=conn.execute('SELECT version,content FROM conclusions WHERE topic=%s ORDER BY version DESC LIMIT 1',(topic,)).fetchone()
        if old and old[1]==result:
            return
        conn.execute('INSERT INTO conclusions(topic,version,content,reason) VALUES (%s,%s,%s,%s)',(topic,old[0]+1 if old else 1,Jsonb(result),'Новые или обновлённые проверенные факты; предыдущая версия сохранена'))

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
    facts=fact_context(conn,question)
    status=[]
    collection={}
    processing=[]
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
                return {'claims':[],'clarification':clarification.strip()[:2000],
                        'limitations':[],'sources':[],'scope_id':scope_id}
            if identified is not None and identified is not True:
                raise ValueError('Некорректный предмет поиска')
            query=plan.get('query')
            if not isinstance(query,str) or not query.strip() or not isinstance(plan.get('terms',[]),list):
                raise ValueError('Некорректный план поиска')
            query=query.strip()[:1000]
            terms=[t.strip() for t in plan.get('terms',[]) if isinstance(t,str) and t.strip()][:12]
            # fact_context bounds words: put identified subject before the broad question.
            lookup=' '.join(terms+[query,question])
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
                    processing.append({'material_id':row[0],'status':'failed','stage':'processing','reason':'Лимит модели исчерпан'})
                    status.append('Анализ части материалов не завершён: лимит модели исчерпан.')
                    break
                except Exception as error:
                    reason=llm.safe_error(error)
                    processing.append({'material_id':row[0],'status':'failed','stage':'processing','reason':reason})
                    status.append('Один из материалов не прошёл анализ или проверку оснований.')
                    # A post-commit failure must not hide a successfully completed material.
                    conn.execute("""UPDATE materials SET status='failed',last_error=%s WHERE id=%s
                        AND status<>'done' AND text IS NOT DISTINCT FROM %s AND text_scope=%s""",
                        (reason,row[0],row[2],row[3]))
        except Exception as error:
            status.append('Дополнительный поиск или анализ не завершён: '+llm.safe_error(error))
        finally:
            # Includes successes from earlier documents and facts saved before
            # a later synthesis failure. Reread BOTH original and expanded query:
            # translated terms must not displace an existing Russian subject.
            # Never merge the stale initial snapshot: those facts may be retired.
            current=[]
            seen=set()
            for current_query in dict.fromkeys((question,lookup)):
                for fact in fact_context(conn,current_query):
                    if fact['id'] not in seen:
                        current.append(fact)
                        seen.add(fact['id'])
            facts=current[:80]
    result=grounded(conn,'answer',question,facts)
    result.setdefault('limitations',[]).extend(status)
    result['scope_id']=scope_id
    result['source_checks']=collection.get('checks',[])
    result['processing']=processing
    conn.execute('INSERT INTO answers(question,content,owner_telegram_id) VALUES (%s,%s,%s)',
                 (question,Jsonb(result),owner_telegram_id))
    return result
