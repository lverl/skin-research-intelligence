"""Deterministic citation validation in addition to a model verification pass."""
import re

def normalized(text):
    return re.sub(r'\s+',' ',str(text)).strip()

def validate_facts(facts,text):
    if not isinstance(facts,list):
        raise ValueError('facts must be a list')
    accepted=[]
    for fact in facts:
        if not isinstance(fact,dict):
            raise ValueError('invalid fact')
        quote=normalized(fact.get('quote',''))
        statement=normalized(fact.get('statement',''))
        if len(quote)<12 or not statement or quote not in normalized(text):
            raise ValueError('Цитата отсутствует в исходном тексте')
        accepted.append({**fact,'quote':quote,'statement':statement})
    return accepted

def validate_claims(content,allowed):
    claims=content.get('claims')
    if not isinstance(claims,list):
        raise ValueError('Ожидается список утверждений')
    for claim in claims:
        ids=claim.get('fact_ids',[])
        if not claim.get('text') or not ids or any(type(i) is not int or i not in allowed for i in ids):
            raise ValueError('Утверждение не имеет проверенных оснований')
    return content


def split_claims(content, allowed):
    """Reject bad references individually without dropping independent claims."""
    from net import SourceError
    if not isinstance(content, dict) or not isinstance(content.get('claims'), list):
        raise SourceError('validation_error')
    accepted, indices, checks = [], [], []
    for index, claim in enumerate(content['claims']):
        ids = claim.get('fact_ids') if isinstance(claim, dict) else None
        valid = (isinstance(claim, dict) and isinstance(claim.get('text'), str)
                 and bool(claim['text'].strip()) and isinstance(ids, list) and bool(ids)
                 and all(type(i) is int and i in allowed for i in ids))
        if valid:
            accepted.append({'text': claim['text'].strip(), 'fact_ids': list(dict.fromkeys(ids))})
            indices.append(index)
        else:
            checks.append({'index': index, 'valid': False, 'code': 'invalid_claim'})
    return accepted, indices, checks


def verified_checks(check, count, *, allowed_rejections=()):
    """A bool summary or incomplete/ambiguous verdict cannot approve prose."""
    from net import SourceError
    codes = {'supported', 'unsupported', 'wrong_subject', 'wrong_population',
             'wrong_protocol', 'unsupported_number', 'personal_advice'}
    codes.update(allowed_rejections)
    if (not isinstance(check, dict) or type(check.get('notes_valid')) is not bool
            or not isinstance(check.get('claims'), list) or len(check['claims']) != count):
        raise SourceError('verification_format')
    verdicts = {}
    for item in check['claims']:
        if (not isinstance(item, dict) or type(item.get('index')) is not int
                or item['index'] not in range(count) or item['index'] in verdicts
                or type(item.get('valid')) is not bool or not isinstance(item.get('code'), str)
                or item['code'] not in codes
                or (item['valid'] != (item['code'] == 'supported'))):
            raise SourceError('verification_format')
        verdicts[item['index']] = item
    return verdicts, check['notes_valid']
