"""Bounded public HTTP reads; redirects revalidated before connecting."""
import ipaddress
import socket
import urllib.parse
import urllib.request
import http.client
import ssl
import json
import urllib.error

ERROR_REASONS = {
 'http_error':'Источник вернул ошибку HTTP', 'dns_error':'Не удалось определить сетевой адрес источника',
 'timeout':'Источник не ответил вовремя', 'tls_error':'Не удалось установить защищённое соединение',
 'network_error':'Не удалось подключиться к источнику', 'unsafe_url':'Адрес запрещён защитой сетевого доступа',
 'redirect_blocked':'Перенаправление запрещено правилами источника', 'size_limit':'Ответ превышает допустимый размер',
 'parse_error':'Не удалось прочитать ответ источника', 'validation_error':'Материал не прошёл проверку данных',
 'quote_mismatch':'Цитата не найдена в исходном тексте', 'grounding_rejected':'Ответ не прошёл проверку оснований',
 'budget_exceeded':'Лимит расходов исчерпан', 'configuration_error':'Канал не настроен для выполнения проверки',
 'source_unavailable':'Источник временно недоступен', 'processing_error':'Обработка материала не завершена',
 'empty_result':'Подходящих результатов в проверенном объёме нет', 'disabled':'Канал планово выключен',
 'out_of_scope':'В ограниченном каталоге нет страниц для этой темы', 'deferred':'Подключение отложено до повторной проверки',
 'no_text':'Доступный содержательный текст не получен',
}


class SourceError(ValueError):
    """Controlled public failure. Never retain an upstream body, URL or reason."""
    def __init__(self, code, *, http_status=None):
        self.safe_code=code if code in ERROR_REASONS else 'source_unavailable'
        self.http_status=http_status if type(http_status) is int and 100<=http_status<=599 else None
        message=ERROR_REASONS[self.safe_code]
        if self.safe_code=='http_error' and self.http_status:
            message+=' ('+str(self.http_status)+')'
        super().__init__(message)


def safe_error(error, stage='access'):
    """Only known codes and fixed Russian text cross the diagnostics boundary."""
    code=getattr(error,'safe_code',None)
    http_status=getattr(error,'http_status',None)
    if not isinstance(code,str): code=None
    if isinstance(error,urllib.error.HTTPError):
        code,http_status='http_error',error.code
    elif code not in ERROR_REASONS:
        if isinstance(error,socket.gaierror): code='dns_error'
        elif isinstance(error,(TimeoutError,socket.timeout)): code='timeout'
        elif isinstance(error,ssl.SSLError): code='tls_error'
        elif isinstance(error,json.JSONDecodeError): code='parse_error'
        elif isinstance(error,urllib.error.URLError):
            if isinstance(error.reason,BaseException):
                return safe_error(error.reason,stage)
            code='network_error'
        elif isinstance(error,OSError): code='network_error'
        else:
            # These messages are generated locally, not arbitrary exception text.
            known={'Цитата отсутствует в исходном тексте':'quote_mismatch',
                   'Ответ не прошёл проверку оснований':'grounding_rejected',
                   'Утверждение не имеет проверенных оснований':'grounding_rejected',
                   'Локальные и служебные адреса запрещены':'unsafe_url','Непубличный адрес':'unsafe_url',
                   'URL запрещён правилами источника':'redirect_blocked',
                   'Требуется публичный HTTP(S) URL':'unsafe_url','Порт запрещён':'unsafe_url',
                   'Документ превышает лимит размера':'size_limit',
                   'Задайте MODEL и OPENROUTER_API_KEY':'configuration_error'}
            code=known.get(str(error))
            if code is None:
                import llm
                code='budget_exceeded' if isinstance(error,llm.BudgetExceeded) else 'validation_error' if isinstance(error,ValueError) else 'processing_error'
    result={'code':code,'reason':ERROR_REASONS[code],
            'stage':stage if stage in {'access','collection','read','search','contents','planning','processing','synthesis','answer','verification'} else 'processing'}
    if code=='http_error' and type(http_status) is int and 100<=http_status<=599:
        result['http_status']=http_status
        result['reason']+=' ('+str(http_status)+')'
    return result

def validate_url(url):
    parts = urllib.parse.urlsplit(url)
    if parts.scheme not in ('http','https') or not parts.hostname or parts.username or parts.password:
        raise SourceError('unsafe_url')
    if parts.port not in (None,80,443):
        raise SourceError('unsafe_url')
    addresses = socket.getaddrinfo(parts.hostname, parts.port or (443 if parts.scheme=='https' else 80))
    if not addresses or any(not ipaddress.ip_address(row[4][0]).is_global for row in addresses):
        raise SourceError('unsafe_url')
    return url

class Redirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        validate_url(newurl)
        return super().redirect_request(req,fp,code,msg,headers,newurl)

def fetch(url, limit=8_000_000, *, url_policy=None):
    for _ in range(6):
        # A channel may narrow public URLs further. Recheck each redirect before
        # DNS/HTTP; the existing public-address guard always remains in force.
        if url_policy is not None and not url_policy(url):
            raise SourceError('redirect_blocked')
        validate_url(url)
        parts=urllib.parse.urlsplit(url)
        port=parts.port or (443 if parts.scheme=='https' else 80)
        addresses=socket.getaddrinfo(parts.hostname,port,type=socket.SOCK_STREAM)
        if any(not ipaddress.ip_address(a[4][0]).is_global for a in addresses):
            raise SourceError('unsafe_url')
        pinned=addresses[0][4][0]
        cls=http.client.HTTPSConnection if parts.scheme=='https' else http.client.HTTPConnection
        conn=cls(parts.hostname,port,timeout=30)
        # Preserve original Host and TLS SNI, connect to the validated address only.
        conn._create_connection=lambda address,timeout,source_address=None: socket.create_connection((pinned,port),timeout,source_address)
        try:
            conn.request('GET',urllib.parse.urlunsplit(('', '',parts.path or '/',parts.query,'')),headers={'User-Agent':'SkinResearch/1.0','Accept-Encoding':'identity'})
            response=conn.getresponse()
            if response.status in (301,302,303,307,308):
                location=response.getheader('Location')
                if not location: raise SourceError('redirect_blocked')
                url=urllib.parse.urljoin(url,location)
                continue
            if not 200<=response.status<300:
                # openFDA documents no-match 404 separately from access failure.
                # Keep only this exact machine signal, never the server body.
                no_match=False
                if response.status==404 and parts.hostname=='api.fda.gov':
                    try:
                        payload=json.loads(response.read(4097))
                        detail=payload.get('error',{})
                        no_match=(detail.get('code')=='NOT_FOUND' and detail.get('message')=='No matches found!')
                    except Exception:
                        pass
                raise SourceError('empty_result' if no_match else 'http_error',http_status=response.status)
            data=response.read(limit+1)
            if len(data)>limit: raise SourceError('size_limit')
            return data
        finally:
            conn.close()
    raise SourceError('redirect_blocked')
