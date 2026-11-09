"""One daily edition; independent of whether scientific conclusions changed."""
import json
import os
import hashlib
import re
from urllib.parse import urlparse
from datetime import datetime,timedelta,timezone
from zoneinfo import ZoneInfo
import llm
import db
import relevance
from evidence import verified_checks
from net import SourceError,safe_error
from db import jsonb as Jsonb

EDITORIAL_VERSION = 5
READER_RULES = (
    'Не включай каталоги и карточки товаров, запуски средств, пресс-релизы и рекламные анонсы. '
    'Конкретное средство включай только при наличии данных исследования готового продукта, независимого сравнения '
    'или официального сообщения о безопасности. Список ингредиентов и назначение продукта не доказывают его '
    'эффективность, переносимость или пригодность для определённого типа кожи. Не достраивай пользу из состава. '
    'Не включай отдельные сообщения об инцидентах MAUDE/openFDA. Для безопасности нужны официальное предупреждение, '
    'отзыв продукта или изменение рекомендаций, относящиеся к косметологии и уходу. '
    'Описывай спокойно: что изменилось, кого касается и какое действие рекомендует регулятор; '
    'не добавляй собственных предостережений. Основу выпуска составляют исследования и разборы вопросов ухода. '
)
COMPACT_RULES = 'summary: 1-2 short Russian sentences, maximum 400 characters. limitations: one short Russian sentence only when essential. '
SOURCE_RULES = ('Для научных результатов используй только переданные проверенные facts с цитатами, '
    'а не непроверенный summary первичного отбора. Сохраняй source_role и вид доступного текста: '
    'справка и объяснения пациентам не являются отдельным исследованием или формальной рекомендацией; '
    'публичное резюме обзора не является полным текстом. Не превращай новость или экспертный пересказ '
    'в новое доказательство эффективности. Указывай исходный год старой публикации, не называй её '
    'новым исследованием только из-за даты загрузки. Чужие оценки достоверности передавай с авторством, '
    'не создавай собственных баллов.')


def _finding_gate():
    return ("rf.status='eligible' AND rf.editorial_version=%s AND rf.profile_version=%s "
            "AND rf.text_version=reader_text_version(m.title,m.text,m.text_scope,m.source_role) "
            "AND rf.input_signature=reader_fact_version(m.id) AND m.status='done' AND f.verified "
            "AND cardinality(rf.fact_ids)>0 AND rf.content->'fact_ids'=to_jsonb(rf.fact_ids) "
            "AND rf.content->'material_ids'=jsonb_build_array(m.id) "
            "AND NOT EXISTS(SELECT 1 FROM unnest(rf.fact_ids) cited(id) WHERE NOT EXISTS "
            "(SELECT 1 FROM facts proof WHERE proof.id=cited.id AND proof.material_id=m.id AND proof.active)) "
            "AND EXISTS(SELECT 1 FROM facts x WHERE x.material_id=m.id AND x.active) AND "
            +relevance.reader_condition('m'))


def current_findings(conn,material_ids=None,*,limit=10,offset=0):
    """Reader GETs only read a previously checked, still current finding."""
    profile=relevance.load_profile()['version']
    params=(EDITORIAL_VERSION,profile,profile)
    gate=_finding_gate()
    if material_ids is not None:
        ids=[i for i in material_ids if type(i) is int and i>0]
        if not ids:
            return []
        gate+=' AND m.id=ANY(%s)'
        params+=(ids,)
    params+=(min(max(int(limit),1),100),max(0,int(offset)))
    rows=conn.execute('''SELECT rf.id,rf.material_id,rf.content,rf.profile_version,rf.text_version,
        rf.input_signature,rf.editorial_version FROM reader_findings rf
        JOIN materials m ON m.id=rf.material_id JOIN findings f ON f.material_id=m.id
        WHERE '''+gate+' ORDER BY rf.created_at DESC,rf.id DESC LIMIT %s OFFSET %s',params).fetchall()
    result=[]
    for fid,mid,content,profile,text,signature,editorial in rows:
        item=dict(content,id=mid,finding_id=fid,profile_version=profile,text_version=text,
                  input_signature=signature,editorial_version=editorial)
        source=content['sources'][0]
        item.update(url=source['url'],source=source['name'],kind=source['kind'],
                    source_role=source['source_role'],text_scope=source['scope'],published_at=source['published_at'])
        result.append(item)
    return result


def _split_reader_items(raw,candidates):
    if not isinstance(raw,dict) or not isinstance(raw.get('items'),list) or len(raw['items'])>10:
        raise SourceError('validation_error')
    allowed={c['id']:c for c in candidates}
    accepted,indices,checks=[],[],[]
    used=set()
    for index,item in enumerate(raw['items']):
        ids=item.get('material_ids') if isinstance(item,dict) else None
        fact_ids=item.get('fact_ids') if isinstance(item,dict) else None
        mid=ids[0] if isinstance(ids,list) and len(ids)==1 and type(ids[0]) is int else None
        candidate=allowed.get(mid)
        valid=bool(candidate and mid not in used and isinstance(fact_ids,list) and fact_ids and
            all(type(i) is int and i in {f['id'] for f in candidate['facts']} for i in fact_ids))
        if valid:
            for key,maximum,required in (('title',160,True),('summary',400,True),('limitations',400,False)):
                value=item.get(key,'' if not required else None)
                if (not isinstance(value,str) or len(value)>maximum or (required and not value.strip())
                        or (value.strip() and not re.search('[А-Яа-яЁё]',value))):
                    valid=False
        if not valid:
            checks.append({'index':index,'valid':False,'code':'invalid_claim'})
            continue
        used.add(mid)
        accepted.append({'material_ids':[mid],'fact_ids':list(dict.fromkeys(fact_ids)),
            'title':item['title'].strip(),'summary':item['summary'].strip(),
            'limitations':item.get('limitations','').strip()})
        indices.append(index)
    return accepted,indices,checks


def _verified_reader_items(check,count):
    if not isinstance(check,dict):
        raise SourceError('verification_format')
    verdicts,_=verified_checks({'claims':check.get('items'),'notes_valid':True},count,
                              allowed_rejections=('not_useful','duplicate'))
    return verdicts


def _store_finding(conn,candidate,status,content=None,error=None):
    with conn.transaction():
        current=conn.execute('''SELECT m.id FROM materials m JOIN findings f ON f.material_id=m.id
            WHERE m.id=%s AND m.status='done' AND f.verified
            AND reader_text_version(m.title,m.text,m.text_scope,m.source_role)=%s
            AND reader_fact_version(m.id)=%s AND '''+relevance.reader_condition('m')+' FOR UPDATE OF m',
            (candidate['id'],candidate['text_version'],candidate['input_signature'],candidate['profile_version'])).fetchone()
        if not current or relevance.load_profile()['version']!=candidate['profile_version']:
            return False
        conn.execute('''INSERT INTO reader_findings(material_id,profile_version,text_version,input_signature,
            editorial_version,status,content,fact_ids,error) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT(material_id,profile_version,text_version,input_signature,editorial_version) DO NOTHING''',
            (candidate['id'],candidate['profile_version'],candidate['text_version'],candidate['input_signature'],
             EDITORIAL_VERSION,status,Jsonb(content or {}),(content or {}).get('fact_ids',[]),Jsonb(error) if error else None))
    return True


def prepare_findings(conn):
    """One bounded worker batch; no generation or retry is hidden inside a GET."""
    locked=conn.execute('SELECT pg_try_advisory_lock(4219,0)').fetchone()[0]
    if not locked:
        return {'prepared':0,'errors':[]}
    try:
        return _prepare_findings(conn)
    finally:
        conn.execute('SELECT pg_advisory_unlock(4219,0)')


def _prepare_findings(conn):
    profile=relevance.load_profile()['version']
    rows=conn.execute('''SELECT m.id,m.title,m.url,f.kind,m.source_role,m.published_at,m.text_scope,
        coalesce(s.name,''),reader_text_version(m.title,m.text,m.text_scope,m.source_role),reader_fact_version(m.id),
        (SELECT jsonb_agg(jsonb_build_object('id',x.id,'statement',x.statement,'quote',x.quote,'payload',x.payload) ORDER BY x.id)
         FROM facts x WHERE x.material_id=m.id AND x.active)
        FROM findings f JOIN materials m ON m.id=f.material_id LEFT JOIN sources s ON s.id=m.source_id
        WHERE m.status='done' AND f.verified AND f.kind<>'commercial'
        AND EXISTS(SELECT 1 FROM facts x WHERE x.material_id=m.id AND x.active)
        AND '''+relevance.reader_condition('m')+''' AND NOT EXISTS(SELECT 1 FROM reader_findings rf
            WHERE rf.material_id=m.id AND rf.profile_version=%s AND rf.editorial_version=%s
            AND rf.text_version=reader_text_version(m.title,m.text,m.text_scope,m.source_role)
            AND rf.input_signature=reader_fact_version(m.id))
        ORDER BY f.created_at DESC,m.id DESC LIMIT 10''',(profile,profile,EDITORIAL_VERSION)).fetchall()
    candidates=[]
    for row in rows:
        item=dict(zip(('id','title','url','kind','source_role','published_at','scope','name',
                       'text_version','input_signature','facts'),row),profile_version=profile)
        if not digest_eligible(item):
            _store_finding(conn,item,'excluded',error=safe_error(SourceError('out_of_scope'),'processing'))
            continue
        from worker.main import bounded_kind
        item['kind']=bounded_kind(item['kind'],item['source_role'])
        if item['published_at']:
            item['published_at']=item['published_at'].isoformat()
        candidates.append(item)
    if not candidates:
        return {'prepared':0,'errors':[]}
    if llm.configuration_issues():
        from decimal import Decimal,InvalidOperation
        monetary=False
        try:
            values=[Decimal(os.environ.get(name,default)) for name,default in
                    (('MODEL_MONTHLY_USD','0'),('MODEL_CALL_RESERVE_USD','1'))]
            monetary=all(v.is_finite() for v in values) and any(v<=0 for v in values)
        except (InvalidOperation,ValueError):
            pass
        configured=all(os.environ.get(name,'').strip() for name in ('MODEL','OPENROUTER_API_KEY'))
        info=safe_error(llm.BudgetExceeded() if monetary and configured else SourceError('configuration_error'),'processing')
        return {'prepared':0,'errors':[dict(info,material_id=c['id']) for c in candidates]}
    generated=False
    try:
        raw=llm.call(conn,'digest',READER_RULES+SOURCE_RULES+COMPACT_RULES+
            'Для каждого пригодного кандидата сформулируй находку по-русски. Читательницы: женщины около 40 лет; '
            'темы строго из профиля. Заголовок сообщает конкретный результат, а не тему или факт выхода новости. '
            'Одна мысль, простые слова, без рекламных оценок. summary раскрывает результат и его значение '
            'для указанной задачи ухода только в пределах источника. Не придумывай практический совет. '
            'title до160 знаков, summary до400, limitations коротко при существенном ограничении. '
            'Все три текста подтверждены указанными fact_ids; сохраняй популяцию, протокол, сроки и числа. '
            'Справка не становится новым исследованием; старый год не становится текущим результатом. '
            'Не включай ожидания результатов к уже прошедшему сроку без актуальных результатов. '
            'Не заполняй список ради количества. Верни {items:[{material_ids:[int],fact_ids:[int],title:str,summary:str,limitations:str}]}; '
            'одна находка на один материал. Нельзя выдавать ингредиент за исследование готового продукта.',candidates)
        generated=True
        items,indices,checks=_split_reader_items(raw,candidates)
        verdicts={}
        if items:
            check=llm.call(conn,'digest_verify',READER_RULES+SOURCE_RULES+
                'Проверь КАЖДУЮ находку независимо по указанным fact_ids. title,summary,limitations целиком '
                'должны следовать из этих фактов и не менять предмет, аудиторию, протокол, число или срок. '
                'Она должна давать понятный конкретный вывод для одной из четырёх задач профиля, а не '
                'выжимку новости или выдуманную пользу. Исключи лабораторное/детское и постороннее применение, '
                'рекламу, персональные назначения, ингредиент→продукт и просроченные ожидания результатов. '
                'Верни {items:[{index:int,valid:bool,code:str}]}: ровно один результат на индекс с нуля. '
                'true только supported; false: unsupported,wrong_subject,wrong_population,wrong_protocol,'
                'unsupported_number,personal_advice,not_useful,duplicate. Заголовки и пояснения по-русски.',
                {'items':items,'candidates':candidates,'profile':relevance.load_profile()})
            verdicts=_verified_reader_items(check,len(items))
    except Exception as error:
        info=safe_error(error,'verification' if generated else 'processing')
        # A pre-call monetary/configuration block may recover without payment.
        # Other failures remain held for targeted review, never an automatic paid loop.
        cache=generated or (not isinstance(error,llm.BudgetExceeded) and info['code']!='configuration_error')
        if cache:
            for candidate in candidates:
                _store_finding(conn,candidate,'needs_review',error=info)
        return {'prepared':0,'errors':[dict(info,material_id=c['id']) for c in candidates]}
    accepted={}
    for index,item in enumerate(items):
        mid=item['material_ids'][0]
        verdict=verdicts[index]
        accepted[mid]=(item,verdict)
    prepared=0
    errors=[]
    for candidate in candidates:
        item,verdict=accepted.get(candidate['id'],(None,None))
        if verdict and verdict['valid']:
            item['sources']=[{key:candidate.get(key) for key in ('url','kind','name','source_role','scope','published_at')}]
            if _store_finding(conn,candidate,'eligible',item):
                prepared+=1
        else:
            code=verdict['code'] if verdict else 'invalid_claim'
            info=dict(safe_error(SourceError('grounding_rejected'),'verification'),item_code=code)
            status='excluded' if code in {'wrong_subject','wrong_population','not_useful','duplicate'} else 'needs_review'
            _store_finding(conn,candidate,status,error=info)
            if status=='needs_review':
                errors.append(dict(info,material_id=candidate['id']))
    return {'prepared':prepared,'errors':errors}


def source_coverage(conn,since,until):
    """Coverage of scheduled reader sources, never every source on the internet.

    Questions do not satisfy monitoring. A successful check that is not yet due
    again is outside this period's plan. Missing due checks and unfinished daily
    claims are incomplete, even if the material queue happens to be empty.
    """
    from decimal import Decimal,InvalidOperation
    from collector.exa import allowed_scientific_url
    start=since.isoformat()
    end=until.isoformat()
    checks=[]
    expected=checked=failed=0
    material_ids=set()
    missing=[]
    rows=conn.execute('''SELECT s.id,s.name,s.kind,s.url,s.interval_hours,s.deferred_at,
        s.deferred_code,s.deferred_reason,c.status,c.complete,c.code,c.reason,c.details,c.finished_at
        FROM sources s LEFT JOIN LATERAL (
          SELECT status,complete,code,reason,details,finished_at FROM source_checks
          WHERE source_id=s.id AND purpose='monitoring' AND planned AND finished_at<=%s
          ORDER BY finished_at DESC,id DESC LIMIT 1) c ON TRUE
        WHERE s.active AND s.kind NOT IN ('manual','exa','europe_pmc','pubmed','beauty_facts') ORDER BY s.id''',(until,)).fetchall()
    for row in rows:
        sid,name,kind,url,interval,deferred_at,deferred_code,deferred_reason,status,complete,code,reason,details,finished=row
        parsed=urlparse(url)
        if not allowed_scientific_url(url) or (kind=='openfda' and parsed.path.startswith('/device/event')):
            continue
        if deferred_at and deferred_at<since:
            checks.append({'source_id':sid,'name':name,'status':'deferred','planned':False,'complete':False})
            continue
        if deferred_at and deferred_at<=until:
            expected+=1
            failed+=1
            checks.append({'source_id':sid,'name':name,'status':'failed','planned':True,'complete':False,
                           'code':deferred_code,'reason':deferred_reason})
            continue
        if finished and finished<since and (until-finished).total_seconds()<interval*3600:
            continue
        expected+=1
        if finished and finished>=since:
            checked+=1
            check={'source_id':sid,'name':name,'status':status,'planned':True,'complete':bool(complete),'code':code,'reason':reason}
            if not complete: failed+=1
            if isinstance(details,dict):
                material_ids.update(i for i in details.get('material_ids',[]) if type(i) is int)
        else:
            missing.append(sid)
            check={'source_id':sid,'name':name,'status':'missing','planned':True,'complete':False}
        checks.append(check)
    zone=configured_zone()
    day=until.astimezone(zone).date()
    daily=conn.execute('SELECT status,code,reason,run_id FROM daily_search_runs WHERE day=%s',(day,)).fetchone()
    try:
        value=Decimal(os.environ.get('EXA_MONTHLY_USD','0'))
        daily_planned=not (value.is_finite() and value==0)
    except (InvalidOperation,ValueError):
        daily_planned=True
    daily_status=daily[0] if daily else 'missing' if daily_planned else 'disabled'
    if daily or daily_planned:
        expected+=1
        daily_checks=conn.execute('''SELECT status,complete,code,reason,details FROM source_checks
            WHERE purpose='daily' AND run_id=%s AND scope_id=%s AND planned ORDER BY id''',
            (daily[3],'daily:'+day.isoformat())).fetchall() if daily and daily[3] else []
        good=bool(daily and daily[0]=='completed' and daily_checks and all(row[1] for row in daily_checks))
        if daily_checks: checked+=1
        if not good:
            failed+=1
            if not daily_checks: missing.append('daily')
        checks.append({'name':'Exa','status':'success' if good else daily_status,'planned':True,'complete':good,
                       'code':daily[1] if daily else None,'reason':daily[2] if daily else None})
        for _,_,_,_,details in daily_checks:
            if isinstance(details,dict):
                material_ids.update(i for i in details.get('material_ids',[]) if type(i) is int)
    pending=0
    if material_ids:
        pending=conn.execute("""SELECT count(*) FROM materials WHERE id=ANY(%s)
            AND (status IN ('new','failed') OR (status='done' AND last_error IS NOT NULL))""",(sorted(material_ids),)).fetchone()[0]
    complete=bool(expected and checked==expected and not failed and not missing and not pending)
    stable={'expected':expected,'checked':checked,'failed':failed,'pending':pending,'checks':checks,
            'missing_source_ids':missing,'daily_status':daily_status,'complete':complete}
    fingerprint=hashlib.sha256(json.dumps(stable,sort_keys=True,ensure_ascii=False).encode()).hexdigest()
    return dict(stable,period_start=start,period_end=end,fingerprint=fingerprint)


def digest_eligible(candidate):
    """Catalogues and individual adverse events stay searchable, not digestible."""
    parsed = urlparse(candidate['url'])
    host = (parsed.hostname or '').lower()
    return not (
        host == 'openbeautyfacts.org' or host.endswith('.openbeautyfacts.org')
        or '/cfmaude/' in parsed.path.lower()
        or (host == 'api.fda.gov' and parsed.path.lower().startswith('/device/event'))
        or candidate.get('kind') == 'commercial'
    )


def configured_zone():
    name=os.environ.get('DIGEST_TIMEZONE','Europe/Moscow')
    return timezone.utc if name=='UTC' else ZoneInfo(name)

def generate(conn,now=None):
    try:
        # The existing worker invokes this every cycle. Findings are useful to
        # the feed before the edition hour; GET routes never invoke preparation.
        preparation=prepare_findings(conn)
        return _generate(conn,now,preparation)
    except llm.BudgetExceeded:
        # A status edition is still produced without paid calls; never claim no news.
        zone=configured_zone()
        moment=(now or datetime.now(zone)).astimezone(zone)
        content={'items':[],'message':'Ежедневный разбор не завершён: исчерпан установленный лимит модели. Материалы остаются в базе для последующей обработки.'}
        return conn.execute("INSERT INTO digests(day,status,content) VALUES (%s,'limited',%s) ON CONFLICT(day) DO UPDATE SET day=EXCLUDED.day RETURNING id",(moment.date(),Jsonb(content))).fetchone()[0]

def _finding_errors(conn):
    profile=relevance.load_profile()['version']
    rows=conn.execute("""SELECT m.id,m.url,f.kind,rf.error FROM materials m
        JOIN findings f ON f.material_id=m.id LEFT JOIN reader_findings rf ON rf.material_id=m.id
            AND rf.profile_version=%s AND rf.editorial_version=%s
            AND rf.text_version=reader_text_version(m.title,m.text,m.text_scope,m.source_role)
            AND rf.input_signature=reader_fact_version(m.id)
        WHERE m.status='done' AND f.verified
        AND EXISTS(SELECT 1 FROM facts x WHERE x.material_id=m.id AND x.active)
        AND """+relevance.reader_condition('m')+""" AND (rf.id IS NULL OR rf.status='needs_review')
        ORDER BY m.id LIMIT 100""",(profile,EDITORIAL_VERSION,profile)).fetchall()
    errors=[]
    for mid,url,kind,error in rows:
        if digest_eligible({'url':url,'kind':kind}):
            code=(error or {}).get('code','processing_error')
            errors.append(dict(safe_error(SourceError(code),'verification'),material_id=mid))
    return errors


def _generate(conn,now=None,preparation=None):
    zone=configured_zone()
    now=(now or datetime.now(zone)).astimezone(zone)
    if now.hour<int(os.environ.get('DIGEST_HOUR','19')):
        return None
    profile=relevance.load_profile()['version']
    with conn.transaction():
        conn.execute('SELECT pg_advisory_xact_lock(4210)')
        existing=conn.execute('SELECT id,content,created_at FROM digests WHERE day=%s',(now.date(),)).fetchone()
        legacy=bool(existing and (existing[1].get('editorial_version',1)!=EDITORIAL_VERSION
                                  or existing[1].get('profile_version')!=profile))
        prior=[]
        if existing and not legacy:
            mids=[mid for item in existing[1].get('items',[]) for mid in item.get('material_ids',[])]
            current={item['id']:item for item in current_findings(conn,mids,limit=100)}
            for item in existing[1].get('items',[]):
                ids=item.get('material_ids',[])
                match=current.get(ids[0]) if len(ids)==1 else None
                if match and match['finding_id']==item.get('finding_id'):
                    prior.append(match)
        editorial_errors=_finding_errors(conn)
        fresh_errors={e.get('material_id'):e for e in (preparation or {}).get('errors',[])}
        editorial_errors=[fresh_errors.get(e.get('material_id'),e) for e in editorial_errors]
        since=(existing[2] if existing and not legacy else
               conn.execute('SELECT max(created_at) FROM digests WHERE id<>%s AND day<%s',
                            (0,now.date())).fetchone()[0] or now-timedelta(days=1))
        coverage=source_coverage(conn,now-timedelta(days=1),now)
        coverage['editorial_pending']=len(editorial_errors)
        coverage['fingerprint']=hashlib.sha256(json.dumps(
            [coverage['fingerprint'],editorial_errors],sort_keys=True,ensure_ascii=False).encode()).hexdigest()
        ids=[row[0] for row in conn.execute("""SELECT m.id FROM reader_findings rf
            JOIN materials m ON m.id=rf.material_id JOIN findings f ON f.material_id=m.id
            WHERE """+_finding_gate()+""" AND greatest(rf.created_at,f.created_at)>%s
            AND NOT EXISTS(SELECT 1 FROM digest_items di JOIN digests d ON d.id=di.digest_id
                WHERE di.material_id=m.id AND d.id<>%s AND d.created_at>=rf.created_at)
            ORDER BY rf.created_at DESC,rf.id DESC LIMIT 100""",
            (EDITORIAL_VERSION,profile,profile,since,existing[0] if existing else 0)).fetchall()]
        fresh=current_findings(conn,ids,limit=100)
        combined={item['id']:item for item in fresh}
        for item in prior:
            combined.setdefault(item['id'],item)
        items=list(combined.values())[:10]
        updates=conn.execute("""SELECT topic,version,content,reason FROM conclusions
            WHERE is_current AND created_at>%s ORDER BY created_at DESC LIMIT 30""",(since,)).fetchall()
        changes=[dict(zip(('topic','version','content','reason'),row)) for row in updates]
        if (existing and not legacy and items==existing[1].get('items',[]) and not changes
                and existing[1].get('coverage',{}).get('fingerprint')==coverage['fingerprint']
                and existing[1].get('editorial_errors',[])==editorial_errors):
            return existing[0]
        complete=coverage['complete'] and not editorial_errors
        coverage_status='complete' if complete else 'partial' if items else 'incomplete'
        if any(error['code']=='budget_exceeded' for error in editorial_errors):
            message='Проверка находок не завершена: исчерпан установленный лимит модели.'
        elif editorial_errors:
            message='Проверка находок не завершена.'
        elif not coverage['complete']:
            message='Проверка источников не завершена.'
        else:
            message='' if items else 'Подходящих обновлений нет в проверенном объёме.'
        content={'items':items,'conclusion_updates':changes,'revision':int(existing[1].get('revision',1))+1 if existing else 1,
                 'editorial_version':EDITORIAL_VERSION,'profile_version':profile,'coverage':coverage,
                 'coverage_status':coverage_status,'editorial_errors':editorial_errors,'message':message}
        status='ready' if complete else 'partial' if items else 'limited' if any(
            error['code']=='budget_exceeded' for error in editorial_errors) else 'incomplete'
        if existing:
            did=existing[0]
            conn.execute('UPDATE digests SET content=%s,status=%s,created_at=now() WHERE id=%s',(Jsonb(content),status,did))
        else:
            did=conn.execute('INSERT INTO digests(day,status,content) VALUES (%s,%s,%s) RETURNING id',
                             (now.date(),status,Jsonb(content))).fetchone()[0]
        for mid in {mid for item in items for mid in item['material_ids']}:
            conn.execute('INSERT INTO digest_items VALUES (%s,%s) ON CONFLICT DO NOTHING',(did,mid))
        return did
