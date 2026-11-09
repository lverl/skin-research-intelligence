"""Versioned reader admission. Pure policy, then separately persisted decisions.

Classification is not evidence verification. A model boolean cannot publish a
material; explicit application, audience and a source quote are required too.
"""
import copy
import hashlib
import json
import re
import unicodedata
from functools import lru_cache
from pathlib import Path
import db

PROFILE_PATH = Path(__file__).with_name('reader_profile.json')
REASONS = {
    'eligible': 'Предмет, применение и аудитория соответствуют выбранной задаче',
    'off_topic': 'Материал относится к другой задаче или области применения',
    'laboratory_only': 'Только лабораторная или животная модель без подходящих результатов на людях',
    'paediatric_only': 'Данные только о детях; применимость к взрослым не установлена',
    'no_results': 'Результаты применения ещё не представлены',
    'commercial': 'Рекламный материал без независимых данных',
    'no_text': 'Содержательный текст для оценки не получен',
    'review_required': 'Нужно проверить предмет, применение и аудиторию',
    'incomplete_review': 'Проверка применимости не дала полного обоснования',
    'quote_mismatch': 'Обоснование применимости не найдено в исходном тексте',
    'wrong_application': 'Применение не относится к выбранным задачам ухода за лицом и шеей',
    'wrong_audience': 'Применимость к взрослым читательницам не установлена',
    'unsupported_topic': 'Выбранное направление не подтверждено содержанием',
    'human_results_required': 'Для этого научного материала нет подтверждённых результатов на людях',
}


@lru_cache(maxsize=1)
def _profile():
    return json.loads(PROFILE_PATH.read_text(encoding='utf-8'))


def load_profile():
    return copy.deepcopy(_profile())


def normalized(text):
    return re.sub(r'\s+', ' ', unicodedata.normalize('NFKC', str(text or ''))).strip()


def scope_key(purpose='reader', owner_telegram_id=None, question=None):
    if purpose == 'reader':
        return 'reader'
    if purpose != 'question':
        raise ValueError('Неизвестная область оценки материала')
    if type(owner_telegram_id) is not int or owner_telegram_id <= 0:
        raise ValueError('Для личной оценки требуется подтверждённый владелец')
    if not isinstance(question, str) or not question.strip():
        raise ValueError('Для личной оценки требуется вопрос')
    subject = normalized(question).casefold()
    digest = hashlib.sha256(subject.encode('utf-8')).hexdigest()
    return 'question:' + str(owner_telegram_id) + ':' + digest


def text_version(material):
    # Length framing matches reader_text_version() in PostgreSQL and cannot be
    # confused by delimiter characters inside untrusted source text. This MD5
    # is a cache checksum, never an authentication or integrity credential.
    values = [material.get('title') or '', material.get('text') or '',
              material.get('text_scope') or 'excerpt', material.get('source_role') or 'unknown']
    framed = ''.join(str(len(value)) + ':' + value for value in values)
    return hashlib.md5(framed.encode('utf-8'), usedforsecurity=False).hexdigest()


DOMAIN_RULES = (
    r'vagin|vulvovagin|gynaecolog|gynecolog|влагалищ|гинеколог',
    r'\bretin(?:a|al)\b|ophthalm|corneal|сетчатк|офтальмолог',
    r'periodont|periodontal|dental|gingivit|пародонт|стоматолог|зубн',
    r'parenteral nutrition|парентеральн|neonatal pig|новорожд[её]нн.{0,20}поросят',
    r'chronic wounds?|pressure ulcers?|хроническ.{0,12}ран|пролежн',
)
LAB_RULE = r'in[ -]vitro|cell cultures?|cellular cultures?|fibroblast.{0,15}cult|cultured cells|культур.{0,15}клет|лабораторн|\b(?:mice|rats|piglets|pigs|porcine)\b|на мыш|на крыс|поросят'
CHILD_RULE = r'paediatric|pediatric|children|infants|newborn|неонатальн|младенц|детей|детск'
FACE_RULE = r'facial|face(?: and neck)?|skin[ -]?care|dermatolog|кож.{0,10}лиц|уход.{0,15}кож|омоложени.{0,10}лиц'
TOPIC_SIGNALS = {
    'home_skincare': r'topical|skincare|skin care|pigment|sunscreen|retino|retinol|moistur|niacinamide|azelaic|пигмент|ретин|увлажн|уход|крем|сыворот|солнц',
    'aesthetic_procedures': r'radiofrequency|laser|\bRF\b|\bBBL\b|\bIPL\b|inject|rejuvenat|лазер|инъекц|процедур|омоложен',
    'home_devices_massage': r'home|device|\bLED\b|massage|microcurrent|домашн|устройств|массаж|микроток',
    'finished_cosmetics': r'finished|product|moistur|cream|serum|sunscreen|cosmetic|готов.{0,10}продукт|косметик|крем|сыворот',
}


def _adult_context(text, *, results=False):
    # Background laboratory work or a paediatric comparison does not erase an
    # explicit adult/human arm. Negated human evidence is not a positive arm.
    population = (r'\b(?:adults?|women|human|patients)\b|взросл|женщин|на людях'
                  if results else r'\b(?:adults?|women)\b|взросл|женщин')
    for sentence in re.split(r'[.!?\n]+', text):
        if not re.search(population, sentence, re.I):
            continue
        if re.search(r'\b(?:without|no|not|absence|lacks?)\b.{0,90}\b(?:human|adult|clinical)\b|без.{0,60}(?:люд|человек|взросл)', sentence, re.I):
            continue
        clinical_arm = re.search(
            r'\bclinical (?:study|trial|results|arm)\b|'
            r'\bcontrolled (?:human |clinical )?(?:study|trial)\b|'
            r'\bhuman (?:clinical )?(?:study|trial|results)\b|'
            r'\brandomi[sz]ed (?:controlled )?(?:study|trial)\b|'
            r'клиническ.{0,20}(?:исследован|испытан|результат)|'
            r'исследовани.{0,20}(?:участниц|пациент|на людях)', sentence, re.I)
        if not results or clinical_arm:
            return True
    return False


def assess(material, *, profile=None, purpose='reader', owner_telegram_id=None,
           question=None, review=None):
    profile = profile or load_profile()
    scope = scope_key(purpose, owner_telegram_id, question)
    base = {'status': 'needs_review', 'code': 'review_required',
            'profile_version': str(profile['version']), 'text_version': text_version(material),
            'scope_key': scope, 'topic': None, 'subject': '', 'application': '',
            'audience': '', 'evidence_quote': '', 'review_attempted': review is not None}

    def result(status, code, **details):
        return dict(base, status=status, code=code, reason=REASONS[code], **details)

    text = material.get('text')
    if material.get('text_scope') == 'metadata' or not isinstance(text, str) or not text.strip():
        return result('needs_review', 'no_text')
    title = normalized(material.get('title'))
    head = title + '\n' + text[:6000]
    if purpose == 'reader':
        for rule in DOMAIN_RULES:
            if re.search(rule, title, re.I) or (re.search(rule, head, re.I) and not re.search(FACE_RULE, head, re.I)):
                return result('excluded', 'off_topic')
        if re.search(LAB_RULE, head, re.I) and not _adult_context(head, results=True):
            return result('excluded', 'laboratory_only')
        if re.search(CHILD_RULE, head, re.I) and not _adult_context(head):
            return result('excluded', 'paediatric_only')
        if re.search(r'study protocol|trial protocol|no results yet|results are expected|протокол исследования|результаты ожидаются', head, re.I):
            return result('excluded', 'no_results')
        if material.get('kind') == 'commercial':
            return result('excluded', 'commercial')
    if not isinstance(review, dict):
        return result('needs_review', 'review_required')
    if review.get('relevant') is False:
        return result('excluded', 'off_topic')
    required = ('topic', 'subject', 'application', 'audience', 'reason', 'evidence_quote')
    if review.get('relevant') is not True or any(not isinstance(review.get(k), str) or not review[k].strip() for k in required):
        return result('needs_review', 'incomplete_review')
    quote = normalized(review['evidence_quote'])
    if len(quote) < 12 or quote not in normalized(text):
        return result('needs_review', 'quote_mismatch')
    if purpose == 'reader':
        if review['application'] not in profile['applications']:
            return result('excluded', 'wrong_application')
        if review['audience'] not in profile['audiences']:
            return result('excluded', 'wrong_audience')
        topic = review['topic']
        if topic not in profile['topics'] or not re.search(TOPIC_SIGNALS.get(topic, r'(?!)'), head, re.I):
            return result('needs_review', 'unsupported_topic')
        informational = material.get('source_role') in {'reference', 'professional_information'}
        regulatory = material.get('kind') == 'regulatory'
        if not informational and not regulatory and review.get('human_results') is not True:
            return result('needs_review', 'human_results_required')
    return result('eligible', 'eligible', topic=review['topic'],
                  subject=review['subject'][:300], application=review['application'],
                  audience=review['audience'], evidence_quote=quote)


def material_from_row(row):
    return dict(zip(('id', 'title', 'text', 'text_scope', 'source_role'), row))


def get_material(conn, material_id, *, lock=False):
    row = conn.execute('SELECT id,title,text,text_scope,source_role FROM materials WHERE id=%s' +
                       (' FOR UPDATE' if lock else ''), (material_id,)).fetchone()
    return material_from_row(row) if row else None


def store_assessment(conn, material, result):
    """Never publish a classification made against a concurrently replaced text."""
    if result.get('status') not in {'eligible', 'excluded', 'needs_review'} or result.get('code') not in REASONS:
        raise ValueError('Некорректная оценка применимости')
    with conn.transaction():
        current = get_material(conn, material['id'], lock=True)
        if current is None or text_version(current) != result['text_version']:
            return False
        conn.execute('''INSERT INTO material_assessments
            (material_id,profile_version,text_version,scope_key,status,code,details)
            VALUES (%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT(material_id,profile_version,text_version,scope_key) DO UPDATE SET
            status=EXCLUDED.status,code=EXCLUDED.code,details=EXCLUDED.details,reviewed_at=now()''',
            (material['id'], result['profile_version'], result['text_version'], result['scope_key'],
             result['status'], result['code'], db.jsonb(result)))
    return True


def get_current_assessment(conn, material, *, profile=None, purpose='reader',
                           owner_telegram_id=None, question=None):
    version = str((profile or load_profile())['version'])
    scope = scope_key(purpose, owner_telegram_id, question)
    row = conn.execute('''SELECT a.details FROM material_assessments a JOIN materials m ON m.id=a.material_id
        WHERE a.material_id=%s AND a.profile_version=%s AND a.text_version=%s AND a.scope_key=%s
        AND a.text_version=reader_text_version(m.title,m.text,m.text_scope,m.source_role)''',
        (material['id'], version, text_version(material), scope)).fetchone()
    return row[0] if row else None


def reader_condition(alias='m'):
    """One shared SQL predicate for current public admission; caller binds version."""
    if not re.fullmatch(r'[a-z][a-z0-9_]*', alias):
        raise ValueError('Некорректное имя таблицы')
    return ("EXISTS (SELECT 1 FROM material_assessments ra WHERE ra.material_id=" + alias +
            ".id AND ra.profile_version=%s AND ra.scope_key='reader' AND ra.status='eligible' "
            "AND ra.text_version=reader_text_version(" + alias + '.title,' + alias + '.text,' +
            alias + '.text_scope,' + alias + '.source_role))')


def record_candidate(conn, material_id):
    material = get_material(conn, material_id)
    if material is None:
        return
    existing = get_current_assessment(conn, material)
    if existing is None:
        store_assessment(conn, material, assess(material))
