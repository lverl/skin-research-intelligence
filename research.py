"""Grounded cross-document synthesis and question answering."""
import db
import llm
from evidence import validate_claims
from db import jsonb as Jsonb

def fact_context(conn,query):
    words=[w for w in query.split() if len(w)>1][:12]
    patterns=['%'+w+'%' for w in words] or ['%'+query+'%']
    rows=conn.execute("SELECT x.id,x.statement,x.quote,x.payload,m.id,m.title,m.url,m.text_scope,f.kind FROM facts x JOIN materials m ON m.id=x.material_id JOIN findings f ON f.material_id=m.id WHERE x.active AND m.status='done' AND f.verified AND (m.title ILIKE ANY(%s) OR x.statement ILIKE ANY(%s) OR f.topics::text ILIKE ANY(%s)) ORDER BY x.id DESC LIMIT 80",(patterns,patterns,patterns)).fetchall()
    return [dict(zip(('id','statement','quote','payload','material_id','title','url','scope','kind'),r)) for r in rows]

PROMPT='Составь справочный ответ по-русски только по переданным фактам. Верни {claims:[{text:str,fact_ids:[int]}],limitations:[str],contradictions:[str],missing_data:[str]}. Все содержательные утверждения включая ограничения и противоречия должны быть в claims с fact_ids. Остальные списки только для пробелов и ограничений процесса. Не сравнивай несопоставимые показатели, не считай универсальный процент эффективности. Отделяй отзывы, экспертное мнение и рекламу от научных результатов. Не переноси доказательства ингредиента на продукт. Отмечай аннотации. Неизвестное обозначай. Числа используй только с условиями и сроками. Не назначай лечение.'

def grounded(conn,stage,question,facts):
    if not facts:
        return {'claims':[],'limitations':['В базе нет проверенных фактов для ответа.'],'contradictions':[],'missing_data':['Недостаточно данных'],'sources':[]}
    result=llm.call(conn,stage,PROMPT,{'question':question,'facts':facts})
    validate_claims(result,{f['id'] for f in facts})
    check=llm.call(conn,stage+'_verify','Проверь каждое утверждение ответа по фактам. Верни {valid:bool}. Отклоняй неподтверждённые числа, ложные прямые сравнения, персональные назначения, выводы из отсутствия данных и необоснованный перенос ингредиент→продукт.',{'answer':result,'facts':facts})
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

def answer(conn,question):
    if not isinstance(question,str) or not question.strip() or len(question)>2000:
        raise ValueError('Вопрос должен содержать от 1 до 2000 символов')
    from collector.main import search
    from worker.main import process_material
    facts=fact_context(conn,question)
    status=[]
    if len(facts)<4:
        try:
            plan=llm.call(conn,'search_plan','Преобразуй вопрос в поисковый запрос на английском и русские/английские ключевые слова с синонимами. Для крема сохрани точное название и бренд. Если вопрос неоднозначен, верни clarification с вопросом, иначе пустую строку. Формат {query:str,terms:[str],clarification:str}.',{'question':question})
            if plan.get('clarification'):
                return {'claims':[],'clarification':str(plan['clarification']),'limitations':[],'sources':[]}
            query=str(plan.get('query',''))[:1000]
            terms=[t for t in plan.get('terms',[]) if isinstance(t,str) and t.strip()][:12]
            result=search(conn,query)
            if result.get('failed_sources'):
                status.append('Часть источников недоступна: '+', '.join(result['failed_sources']))
            patterns=['%'+t+'%' for t in terms] or ['%'+question+'%']
            for row in conn.execute("SELECT m.id,m.title,m.text,m.text_scope FROM materials m LEFT JOIN findings f ON f.material_id=m.id WHERE (m.status='new' OR (m.status='done' AND NOT coalesce(f.verified,FALSE))) AND (m.title ILIKE ANY(%s) OR f.summary ILIKE ANY(%s)) ORDER BY m.id DESC LIMIT 8",(patterns,patterns)).fetchall():
                process_material(conn,row,force=True)
            facts=fact_context(conn,' '.join([question]+terms))
        except Exception as error:
            status.append('Дополнительный поиск или анализ не завершён: '+type(error).__name__)
    result=grounded(conn,'answer',question,facts)
    result.setdefault('limitations',[]).extend(status)
    conn.execute('INSERT INTO answers(question,content) VALUES (%s,%s)',(question,Jsonb(result)))
    return result
