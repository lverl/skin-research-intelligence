"""Bounded public HTTP reads; redirects revalidated before connecting."""
import ipaddress
import socket
import urllib.parse
import urllib.request
import http.client
import ssl

def validate_url(url):
    parts = urllib.parse.urlsplit(url)
    if parts.scheme not in ('http','https') or not parts.hostname or parts.username or parts.password:
        raise ValueError('Требуется публичный HTTP(S) URL')
    if parts.port not in (None,80,443):
        raise ValueError('Порт запрещён')
    addresses = socket.getaddrinfo(parts.hostname, parts.port or (443 if parts.scheme=='https' else 80))
    if not addresses or any(not ipaddress.ip_address(row[4][0]).is_global for row in addresses):
        raise ValueError('Локальные и служебные адреса запрещены')
    return url

class Redirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        validate_url(newurl)
        return super().redirect_request(req,fp,code,msg,headers,newurl)

def fetch(url, limit=8_000_000):
    for _ in range(6):
        validate_url(url)
        parts=urllib.parse.urlsplit(url)
        port=parts.port or (443 if parts.scheme=='https' else 80)
        addresses=socket.getaddrinfo(parts.hostname,port,type=socket.SOCK_STREAM)
        if any(not ipaddress.ip_address(a[4][0]).is_global for a in addresses):
            raise ValueError('Непубличный адрес')
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
                if not location: raise ValueError('Redirect without location')
                url=urllib.parse.urljoin(url,location)
                continue
            if not 200<=response.status<300:
                raise ValueError('HTTP '+str(response.status))
            data=response.read(limit+1)
            if len(data)>limit: raise ValueError('Документ превышает лимит размера')
            return data
        finally:
            conn.close()
    raise ValueError('Слишком много перенаправлений')
