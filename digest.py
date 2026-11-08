"""One daily edition; independent of whether scientific conclusions changed."""
import json
import os
import hashlib
from urllib.parse import urlparse
from datetime import datetime,timedelta,timezone
from zoneinfo import ZoneInfo
import llm
import db
from db import jsonb as Jsonb

EDITORIAL_VERSION = 4
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


def _candidate(row):
    from worker.main import bounded_kind
    candidate=dict(zip(('id','title','url','summary','kind','details','verified','source_role','published_at','scope','facts'),row))
    candidate['kind']=bounded_kind(candidate['kind'],candidate.get('source_role','unknown'))
    # Actual SQL always includes the proof fields. Six-column legacy fixture
    # callers retain their prior boundary without inventing facts for them.
    if candidate['kind'] in {'research','review','guideline'} and 'verified' in candidate:
        if not candidate['verified'] or not candidate.get('facts'):
            return None
    if not digest_eligible(candidate): return None
    if candidate.get('published_at'):
        candidate['published_at']=candidate['published_at'].isoformat() if hasattr(candidate['published_at'],'isoformat') else str(candidate['published_at'])
    return candidate


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
        legacy = bool(existing and existing[1].get('editorial_version', 1) < EDITORIAL_VERSION)
        if existing and not legacy and existing[1].get('items') and existing[1].get('coverage_status','complete')=='complete':

            return existing[0]
        # Empty status editions can be replaced when processing catches up later
        # the same day. Their own timestamp is the refresh watermark, otherwise
        # every worker cycle would repeat the same paid digest generation.
        since=(existing[2] if existing and not legacy else
               conn.execute('SELECT max(created_at) FROM digests WHERE id<>%s AND day<%s',
                            (0,now.date())).fetchone()[0] or now-timedelta(days=1))
        coverage=source_coverage(conn,now-timedelta(days=1),now)
        fields="""m.id,m.title,m.url,f.summary,f.kind,f.details,f.verified,m.source_role,m.published_at,m.text_scope,
            COALESCE((SELECT jsonb_agg(jsonb_build_object('id',x.id,'statement',x.statement,'quote',x.quote,'payload',x.payload))
                      FROM facts x WHERE x.material_id=m.id AND x.active),'[]'::jsonb)"""
        rows=conn.execute("SELECT "+fields+" FROM findings f JOIN materials m ON m.id=f.material_id WHERE f.created_at>%s AND m.status='done' AND f.kind<>'commercial' AND NOT EXISTS(SELECT 1 FROM sources s WHERE s.id=m.source_id AND (s.kind='beauty_facts' OR (s.kind='openfda' AND s.url LIKE '%%/device/event%%'))) AND NOT EXISTS(SELECT 1 FROM digest_items di JOIN digests d ON d.id=di.digest_id WHERE di.material_id=m.id AND d.id<>%s AND d.created_at>=f.created_at) ORDER BY f.created_at DESC LIMIT 100",(since,existing[0] if existing else 0)).fetchall()
        candidates=[candidate for row in rows if (candidate:=_candidate(row)) is not None]
        fresh=bool(candidates)
        if fresh and existing and not legacy and existing[1].get('coverage_status')=='partial':
            prior_ids=list({mid for item in existing[1].get('items',[]) for mid in item.get('material_ids',[])})
            old_rows=conn.execute("SELECT "+fields+" FROM findings f JOIN materials m ON m.id=f.material_id WHERE m.id=ANY(%s) AND m.status='done'",(prior_ids,)).fetchall() if prior_ids else []
            merged={candidate['id']:candidate for candidate in candidates}
            for row in old_rows:
                candidate=_candidate(row)
                if candidate is not None: merged.setdefault(candidate['id'],candidate)
            candidates=list(merged.values())[:100]
        source_names=dict(conn.execute('SELECT m.id,s.name FROM materials m JOIN sources s ON s.id=m.source_id WHERE m.id=ANY(%s)',([r['id'] for r in candidates],)).fetchall()) if candidates else {}
        updates=conn.execute('SELECT topic,version,content,reason FROM conclusions WHERE created_at>%s ORDER BY created_at DESC LIMIT 30',(since,)).fetchall()
        changes=[dict(zip(('topic','version','content','reason'),r)) for r in updates]
        coverage_changed=bool(existing and existing[1].get('coverage') and
                              existing[1]['coverage'].get('fingerprint')!=coverage['fingerprint'])
        if existing and not legacy and not candidates and not changes and not coverage_changed:
            return existing[0]
        if candidates:
            result=llm.call(conn,'digest',READER_RULES+SOURCE_RULES+COMPACT_RULES+'Собери по-русски дайджест до 10 пунктов для обычных читательниц об уходе за кожей и косметологии. Приоритет: исследования на людях, безопасность, ингредиенты, готовые средства, домашний уход, процедуры, устройства, полезные новинки и экспертные материалы. Пропускай лабораторные протоколы, культуру клеток, опухолевую микросреду и опыты на животных без явной связи с уходом, указанной в источнике. Не переноси результаты животных или клеток на человека. Не заполняй выпуск ради количества: если подходящих материалов нет, верни items: []. title — короткий русский заголовок; summary — что узнали; significance — что это значит для ухода без выдуманных рекомендаций; limitations — ограничения, включая изучение только аннотации и доклиническую модель. Изменение научного вывода необязательно. Убирай повторы и перепечатки. Верни {items:[{material_ids:[int],title:str,summary:str,significance:str,limitations:str}]}. Используй только кандидатов, не добавляй фактов. Для мнений и рекламы указывай тип.',candidates)
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
                item['sources']=[{'url':r['url'],'kind':r['kind'],'name':source_names.get(r['id'],''),
                    'source_role':r.get('source_role','unknown'),'scope':r.get('scope'),
                    'published_at':r.get('published_at')} for r in candidates if r['id'] in ids]
            check=llm.call(conn,'digest_verify',READER_RULES+SOURCE_RULES+'Проверь, что дайджест не добавляет неподтверждённых утверждений, не превращает отзывы/рекламу в доказательства, не содержит дублей и персональных назначений. Проверь полезность каждого пункта для обычных читательниц об уходе: исключены лабораторные протоколы без явно обоснованной связи с уходом; нет переноса результатов клеток или животных на человека; заголовки на русском; практический смысл не выдуман. Верни {valid:bool}.',{'items':items,'candidates':candidates})
            if check.get('valid') is not True:
                raise ValueError('Дайджест не прошёл проверку')
        else:
            items=existing[1].get('items',[]) if existing and not legacy and existing[1].get('coverage_status')=='partial' else []
            used={mid for item in items for mid in item.get('material_ids',[])}
        revision=int(existing[1].get('revision',1))+1 if existing else 1
        coverage_status='complete' if coverage['complete'] else 'partial' if items else 'incomplete'
        message='' if items and coverage['complete'] else 'Подходящих обновлений нет в проверенном объёме.' if coverage['complete'] else 'Проверка источников не завершена.'
        content={'items':items,'conclusion_updates':changes,'revision':revision,'editorial_version':EDITORIAL_VERSION,
                 'coverage':coverage,'coverage_status':coverage_status,'message':message}
        status='ready' if coverage['complete'] else 'partial' if items else 'incomplete'
        if existing:
            did=existing[0]
            conn.execute("UPDATE digests SET content=%s,status=%s,created_at=now() WHERE id=%s",(Jsonb(content),status,did))
            db.log('digest',f'refreshed edition {did}; revision {revision}; items {len(items)}')
        else:
            did=conn.execute('INSERT INTO digests(day,status,content) VALUES (%s,%s,%s) RETURNING id',(now.date(),status,Jsonb(content))).fetchone()[0]
            db.log('digest',f'created edition {did}; revision {revision}; items {len(items)}')
        for mid in used:
            conn.execute('INSERT INTO digest_items VALUES (%s,%s) ON CONFLICT DO NOTHING',(did,mid))
        return did
