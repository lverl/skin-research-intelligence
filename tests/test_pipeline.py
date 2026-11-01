"""Offline service tests: no database driver, paid calls or external sends."""
import os
import unittest
from contextlib import nullcontext
from datetime import datetime,timezone
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
    def __init__(self,candidates=(),existing=None,pending=0):
        self.candidates=candidates; self.existing=existing; self.pending=pending; self.saved=None
    def transaction(self): return nullcontext()
    def execute(self,sql,args=None):
        if 'SELECT id FROM digests WHERE day' in sql: return Result([(self.existing,)] if self.existing else [])
        if 'max(created_at)' in sql: return Result([(None,)])
        if 'FROM findings f JOIN materials' in sql: return Result(self.candidates)
        if 'FROM conclusions' in sql: return Result([])
        if 'count(*) FROM materials' in sql: return Result([(self.pending,)])
        if 'INSERT INTO digests' in sql:
            self.saved=args[-1]; return Result([(7,)])
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
        conn=DigestDB(existing=9)
        with patch.object(llm,'call') as model:
            self.assertEqual(digest.generate(conn,self.now),9)
            model.assert_not_called()

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

    def test_delivery_uses_one_bounded_message(self):
        self.assertEqual(len(chunks('x'*20000)),1)
        self.assertLessEqual(len(chunks('x'*20000)[0]),3500)
