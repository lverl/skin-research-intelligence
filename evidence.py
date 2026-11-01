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
