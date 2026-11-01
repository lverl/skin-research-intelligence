import base64
import unittest
import http.client
import threading
from http.server import ThreadingHTTPServer
from unittest.mock import patch

from web.main import Handler, authorized, clean_config, list_materials


class WebTests(unittest.TestCase):
    def test_auth_rejects_missing_and_malformed_credentials(self):
        for header in (None, '', 'Basic !!!', 'Basic '+base64.b64encode(b'no-colon').decode(), 'Bearer wrong'):
            self.assertFalse(authorized(header, 'private-secret'))
        self.assertFalse(authorized('Bearer ', ''))

    def test_basic_and_bearer_auth(self):
        basic='Basic '+base64.b64encode(b'reader:private-secret').decode()
        self.assertTrue(authorized(basic, 'private-secret'))
        self.assertTrue(authorized('Bearer private-secret', 'private-secret'))

    def test_source_configuration_does_not_expose_credentials(self):
        self.assertEqual(clean_config({'query':'skin','api_key':'private','token':'private','password':'private'}), {'query':'skin'})

    def test_query_is_parameterized(self):
        class Conn:
            def execute(self, sql, params):
                self.sql, self.params = sql, params
                class Cursor:
                    description=[]
                    def fetchall(self): return []
                return Cursor()
        conn=Conn()
        attack="'; DROP TABLE materials;--"
        self.assertEqual(list_materials(conn, {'q':[attack],'topic':[attack]}), [])
        self.assertNotIn(attack, conn.sql)
        self.assertIn(attack, conn.params)

    def test_http_rejects_anonymous_and_cross_origin_writes(self):
        with patch.dict('os.environ', {'APP_TOKEN':'private-test-token-123'}), patch('web.main.db.log', create=True):
            server=ThreadingHTTPServer(('127.0.0.1', 0), Handler)
            thread=threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                client=http.client.HTTPConnection('127.0.0.1', server.server_port)
                client.request('GET', '/api/materials')
                response=client.getresponse()
                self.assertEqual(response.status, 401)
                response.read()
                client.request('POST', '/api/correct', '{}', {
                    'Authorization':'Bearer private-test-token-123',
                    'Content-Type':'application/json', 'Origin':'https://evil.example'})
                response=client.getresponse()
                self.assertEqual(response.status, 403)
                response.read()
                client.close()
            finally:
                server.shutdown()
                server.server_close()
                thread.join()


if __name__ == '__main__':
    unittest.main()
