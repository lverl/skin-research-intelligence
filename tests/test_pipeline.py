"""Offline service tests: no database driver, paid calls or external sends."""
import os
import unittest
from contextlib import nullcontext
from datetime import datetime,timedelta,timezone
from unittest.mock import patch
import digest
import research
import llm
from bot.main import chunks,format_digest

class Result:
    def __init__(self,rows): self.rows=rows
    def fetchone(self): return self.rows[0] if self.rows else None
    def fetchall(self): return self.rows

class DigestDB:
    def __init__(self,candidates=(),existing=None,pending=0,existing_content=None,changes=()):
        self.candidates=candidates; self.existing=existing; self.pending=pending; self.saved=None
        self.existing_content=existing_content if existing_content is not None else {'items':[{'title':'Already sent'}]}
        self.existing_at=datetime(2026,11,1,19,tzinfo=timezone.utc)
        self.changes=changes
        self.refreshed=False
        self.calls=[]
    def transaction(self): return nullcontext()
    def execute(self,sql,args=None):
        self.calls.append((sql,args))
        if 'SELECT id,content,created_at FROM digests WHERE day' in sql: return Result([(self.existing,self.existing_content,self.existing_at)] if self.existing else [])
        if 'max(created_at)' in sql: return Result([(None,)])
        if 'FROM findings f JOIN materials' in sql: return Result(self.candidates)
        if 'FROM conclusions' in sql: return Result(self.changes)
        if 'count(*) FROM materials' in sql: return Result([(self.pending,)])
        if 'INSERT INTO digests' in sql:
            self.saved=args[-1]; return Result([(7,)])
        if 'UPDATE digests SET content=' in sql:
            self.saved=args[0]; self.existing_content=args[0]
            self.refreshed=True
            self.existing_at=datetime(2026,11,1,20,tzinfo=timezone.utc)
        return Result([])

class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.env=patch.dict(os.environ,{'DIGEST_TIMEZONE':'UTC','DIGEST_HOUR':'19'})
        self.env.start()
        self.now=datetime(2026,11,1,20,tzinfo=timezone.utc)
        self.json=patch.object(digest,'Jsonb',side_effect=lambda x:x)
        self.json.start()
    def tearDown(self): self.json.stop(); self.env.stop()

    def test_daily_edition_not_before_configured_hour(self):
        self.assertIsNone(digest.generate(DigestDB(),self.now.replace(hour=18)))

    def test_daily_edition_idempotent(self):
        conn=DigestDB([(4,'Later news','https://example.org','New','news',{})],existing=9)
        with patch.object(llm,'call') as model:
            self.assertEqual(digest.generate(conn,self.now),9)
            model.assert_not_called()

    def test_empty_edition_without_new_data_is_not_regenerated(self):
        conn=DigestDB(existing=9,existing_content={'items':[],'message':'Анализ не завершён'})
        with patch.object(llm,'call') as model:
            self.assertEqual(digest.generate(conn,self.now),9)
        model.assert_not_called()
        self.assertFalse(conn.refreshed)
        self.assertIsNone(conn.saved)
        candidate_call=next(args for sql,args in conn.calls if 'FROM findings f JOIN materials' in sql)
        self.assertEqual(candidate_call,(conn.existing_at,))

    def test_empty_edition_refreshes_with_new_findings_same_id(self):
        conn=DigestDB([(4,'New product','https://example.org','Announcement','news',{})],existing=9,
                      existing_content={'items':[],'revision':2,'message':'Анализ не завершён'})
        result={'items':[{'material_ids':[4],'title':'Новинка','summary':'Анонс','significance':'Новое','limitations':'Заявления производителя'}]}
        with patch.object(llm,'call',side_effect=[result,{'valid':True}]) as model:
            self.assertEqual(digest.generate(conn,self.now),9)
        self.assertTrue(conn.refreshed)
        self.assertEqual(conn.saved['revision'],3)
        self.assertEqual(conn.saved['items'][0]['title'],'Новинка')
        self.assertEqual(model.call_count,2)
        self.assertFalse(any('DELETE' in sql for sql,args in conn.calls))
        with patch.object(llm,'call') as again:
            self.assertEqual(digest.generate(conn,self.now),9)
        again.assert_not_called()

    def test_failed_refresh_keeps_existing_status_edition(self):
        conn=DigestDB([(4,'New product','https://example.org','Announcement','news',{})],existing=9,
                      existing_content={'items':[],'revision':1,'message':'Анализ не завершён'})
        with patch.object(llm,'call',side_effect=[{'items':[{'material_ids':[4]}]},{'valid':False}]):
            with self.assertRaises(ValueError):
                digest.generate(conn,self.now)
        self.assertFalse(conn.refreshed)
        self.assertEqual(conn.existing_content['revision'],1)

    def test_empty_edition_refreshes_for_new_conclusion_without_model(self):
        conn=DigestDB(existing=9,existing_content={'items':[]},changes=[('RF',2,{'claims':[]},'Новые данные')])
        with patch.object(llm,'call') as model:
            self.assertEqual(digest.generate(conn,self.now),9)
        model.assert_not_called()
        self.assertEqual(conn.saved['revision'],2)
        self.assertEqual(conn.saved['conclusion_updates'][0]['topic'],'RF')

    def test_conclusion_only_edition_is_nonempty_and_immutable(self):
        conn=DigestDB(existing=9,existing_content={'items':[],'conclusion_updates':[{'topic':'RF'}]})
        with patch.object(llm,'call') as model:
            self.assertEqual(digest.generate(conn,self.now),9)
        model.assert_not_called()
        self.assertFalse(conn.refreshed)

    def test_no_updates_needs_no_model(self):
        conn=DigestDB()
        with patch.object(llm,'call') as model:
            self.assertEqual(digest.generate(conn,self.now),7)
            self.assertIn('нет',conn.saved['message'])
            model.assert_not_called()

    def test_pending_analysis_is_not_no_news(self):
        conn=DigestDB(pending=2)
        digest.generate(conn,self.now)
        self.assertIn('не завершён',conn.saved['message'])

    def test_news_without_changed_conclusion_is_included(self):
        conn=DigestDB([(4,'New product','https://example.org','Announcement','news',{})])
        result={'items':[{'material_ids':[4],'title':'Новинка','summary':'Анонс','significance':'Новое','limitations':'Заявления производителя'}]}
        with patch.object(llm,'call',side_effect=[result,{'valid':True}]):
            digest.generate(conn,self.now)
        self.assertEqual(conn.saved['items'][0]['sources'][0]['kind'],'news')

    def test_failed_verification_does_not_save_edition(self):
        conn=DigestDB([(4,'New','https://example.org','Announcement','news',{})])
        with patch.object(llm,'call',side_effect=[{'items':[{'material_ids':[4]}]},{'valid':False}]):
            with self.assertRaises(ValueError): digest.generate(conn,self.now)
        self.assertIsNone(conn.saved)

    def test_budget_exhaustion_creates_honest_status(self):
        conn=DigestDB([(4,'New','https://example.org','Announcement','news',{})])
        with patch.object(llm,'call',side_effect=llm.BudgetExceeded):
            digest.generate(conn,self.now)
        self.assertIn('лимит',conn.saved['message'])
        self.assertNotIn('обновлений нет',conn.saved['message'])

    def test_empty_answer_never_calls_model(self):
        with patch.object(llm,'call') as model:
            result=research.grounded(None,'answer','question',[])
            self.assertEqual(result['claims'],[])
            self.assertTrue(result['missing_data'])
            model.assert_not_called()

    def test_delivery_preserves_long_text_in_bounded_messages(self):
        text='x'*20000
        parts=chunks(text)
        self.assertGreater(len(parts),1)
        self.assertEqual(''.join(parts),text)
        self.assertTrue(all(len(part.encode('utf-16-le'))//2<=3500 for part in parts))
