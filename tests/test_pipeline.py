"""Offline service tests: no database driver, paid calls or external sends."""
import os
import unittest
from contextlib import nullcontext
from datetime import datetime,timedelta,timezone
from unittest.mock import patch
import digest
import research
import llm
import relevance
from bot.main import chunks,format_digest,send

class Result:
    def __init__(self,rows): self.rows=rows
    def fetchone(self): return self.rows[0] if self.rows else None
    def fetchall(self): return self.rows

class DigestDB:
    """Edition persistence only: findings are explicit, already checked inputs.

    Admission/editorial SQL is tested on real PostgreSQL in reader_findings.
    """
    def __init__(self,findings=(),existing=None,pending=0,existing_content=None,changes=()):
        self.findings=list(findings); self.existing=existing; self.pending=pending; self.saved=None
        self.editorial_errors=[]
        self.existing_content=existing_content if existing_content is not None else {'items':[],'editorial_version':digest.EDITORIAL_VERSION}
        self.existing_content.setdefault('editorial_version',digest.EDITORIAL_VERSION)
        self.existing_at=datetime(2026,11,1,19,tzinfo=timezone.utc)
        self.changes=changes
        self.refreshed=False
        self.calls=[]
    def transaction(self): return nullcontext()
    def execute(self,sql,args=None):
        self.calls.append((sql,args))
        if 'SELECT id,content,created_at FROM digests WHERE day' in sql: return Result([(self.existing,self.existing_content,self.existing_at)] if self.existing else [])
        if 'max(created_at)' in sql: return Result([(None,)])
        if 'SELECT m.id FROM reader_findings' in sql: return Result([(f['id'],) for f in self.findings])
        if 'FROM conclusions' in sql: return Result(self.changes)
        if 'count(*) FROM materials' in sql: return Result([(self.pending,)])
        if 'INSERT INTO digests' in sql:
            self.saved=args[-1]; self.existing=7; self.existing_content=self.saved
            self.changes=[]
            return Result([(7,)])
        if 'UPDATE digests SET content=' in sql:
            self.saved=args[0]; self.existing_content=args[0]
            self.refreshed=True
            self.existing_at=datetime(2026,11,1,20,tzinfo=timezone.utc)
            self.changes=[]
        return Result([])

def saved_finding(mid=4):
    """An editorial cache row supplied at this unit test's declared boundary."""
    return {'id':mid,'finding_id':100+mid,'material_ids':[mid],'fact_ids':[1000+mid],
            'title':'Находка об уходе','summary':'Подтверждённое объяснение результата.',
            'limitations':'Доступен фрагмент.','sources':[{'url':'https://example.org/'+str(mid),
                'kind':'expert','source_role':'professional_information','scope':'excerpt','name':'Источник'}]}

class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.env=patch.dict(os.environ,{'DIGEST_TIMEZONE':'UTC','DIGEST_HOUR':'19',
            'MODEL':'offline-editorial-fixture','OPENROUTER_API_KEY':'offline-fixture',
            'MODEL_MONTHLY_USD':'10','MODEL_CALL_RESERVE_USD':'1'})
        self.env.start()
        self.now=datetime(2026,11,1,20,tzinfo=timezone.utc)
        self.json=patch.object(digest,'Jsonb',side_effect=lambda x:x)
        self.json.start()
        # These tests exercise formatting/generation, not source-coverage SQL.
        # Declare completed fixture checks explicitly; real SQL is covered on PG.
        def fixture_coverage(conn,since,until):
            pending=conn.pending
            return {'complete':not pending,'expected':2,'checked':2,'failed':0,'pending':pending,
                    'checks':[],'missing_source_ids':[],'daily_status':'disabled',
                    'period_start':since.isoformat(),'period_end':until.isoformat(),
                    'fingerprint':'fixture-pending-'+str(pending)}
        self.coverage=patch.object(digest,'source_coverage',side_effect=fixture_coverage)
        self.coverage.start()
        self.addCleanup(self.coverage.stop)
        self.real_prepare=digest.prepare_findings
        self.preparation=patch.object(digest,'prepare_findings',side_effect=lambda conn:
            {'prepared':0,'errors':list(conn.editorial_errors)})
        self.preparation.start()
        self.addCleanup(self.preparation.stop)
        self.finding_errors=patch.object(digest,'_finding_errors',side_effect=lambda conn:list(conn.editorial_errors))
        self.finding_errors.start()
        self.addCleanup(self.finding_errors.stop)
        def cached(conn,material_ids=None,*,limit=10,offset=0):
            rows=[f for f in conn.findings if material_ids is None or f['id'] in material_ids]
            return rows[offset:offset+limit]
        self.cached=patch.object(digest,'current_findings',side_effect=cached)
        self.cached.start()
        self.addCleanup(self.cached.stop)
    def tearDown(self): self.json.stop(); self.env.stop()

    def test_daily_edition_not_before_configured_hour(self):
        self.assertIsNone(digest.generate(DigestDB(),self.now.replace(hour=18)))

    def test_daily_edition_idempotent(self):
        conn=DigestDB([saved_finding()])
        with patch.object(llm,'call') as model:
            first=digest.generate(conn,self.now)
            original=conn.saved
            conn.refreshed=False
            self.assertEqual(digest.generate(conn,self.now),first)
            model.assert_not_called()
        self.assertFalse(conn.refreshed)
        self.assertEqual(conn.saved,original)

    def test_empty_edition_without_new_data_is_not_regenerated(self):
        conn=DigestDB(pending=1)
        with patch.object(llm,'call') as model:
            first=digest.generate(conn,self.now)
            original=conn.saved
            conn.refreshed=False
            self.assertEqual(digest.generate(conn,self.now),first)
        model.assert_not_called()
        self.assertFalse(conn.refreshed)
        self.assertEqual(conn.saved,original)

    def test_empty_edition_refreshes_with_new_findings_same_id(self):
        conn=DigestDB([saved_finding()],existing=9,
                      existing_content={'items':[],'revision':2,'profile_version':relevance.load_profile()['version']})
        with patch.object(llm,'call') as model:
            self.assertEqual(digest.generate(conn,self.now),9)
        self.assertTrue(conn.refreshed)
        self.assertEqual(conn.saved['revision'],3)
        self.assertEqual(conn.saved['items'],[saved_finding()])
        model.assert_not_called()
        self.assertFalse(any('DELETE' in sql for sql,args in conn.calls))
        with patch.object(llm,'call') as again:
            self.assertEqual(digest.generate(conn,self.now),9)
        again.assert_not_called()

    def test_failed_refresh_keeps_unverified_items_out_of_existing_status_edition(self):
        conn=DigestDB(existing=9,
                      existing_content={'items':[],'revision':1,'message':'Анализ не завершён'})
        conn.editorial_errors=[{'material_id':4,'code':'verification_format','stage':'verification','reason':'Проверка находки не завершена'}]
        self.assertEqual(digest.generate(conn,self.now),9)
        self.assertEqual(conn.saved['items'],[])
        self.assertEqual(conn.saved['coverage_status'],'incomplete')
        self.assertTrue(conn.saved['editorial_errors'])

    def test_empty_edition_refreshes_for_new_conclusion_without_model(self):
        conn=DigestDB(existing=9,existing_content={'items':[]},changes=[('RF',2,{'claims':[]},'Новые данные')])
        with patch.object(llm,'call') as model:
            self.assertEqual(digest.generate(conn,self.now),9)
        model.assert_not_called()
        self.assertEqual(conn.saved['revision'],2)
        self.assertEqual(conn.saved['conclusion_updates'][0]['topic'],'RF')

    def test_conclusion_only_edition_is_nonempty_and_immutable(self):
        conn=DigestDB(changes=[('RF',2,{'claims':[]},'Новые данные')])
        with patch.object(llm,'call') as model:
            first=digest.generate(conn,self.now)
            self.assertEqual(conn.saved['conclusion_updates'][0]['topic'],'RF')
            conn.refreshed=False
            self.assertEqual(digest.generate(conn,self.now),first)
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
        self.assertIn('не заверш',conn.saved['message'])

    def test_news_without_changed_conclusion_is_included(self):
        conn=DigestDB([saved_finding()])
        with patch.object(llm,'call') as model:
            digest.generate(conn,self.now)
        self.assertEqual(conn.saved['items'][0]['sources'][0]['kind'],'expert')
        self.assertEqual(conn.saved['conclusion_updates'],[])
        model.assert_not_called()

    def test_failed_verification_saves_honest_status_without_rejected_items(self):
        conn=DigestDB()
        conn.editorial_errors=[{'material_id':4,'code':'verification_format','stage':'verification','reason':'Проверка находки не завершена'}]
        digest.generate(conn,self.now)
        self.assertEqual(conn.saved['items'],[])
        self.assertEqual(conn.saved['coverage_status'],'incomplete')
        self.assertNotIn('обновлений нет',conn.saved['message'])

    def test_budget_exhaustion_creates_honest_status(self):
        conn=DigestDB()
        conn.editorial_errors=[{'material_id':4,'code':'budget_exceeded','stage':'processing','reason':'Лимит расходов исчерпан'}]
        digest.generate(conn,self.now)
        self.assertIn('лимит',conn.saved['message'])
        self.assertNotIn('обновлений нет',conn.saved['message'])

    def test_legacy_edition_rebuilt_once_even_without_new_candidates(self):
        conn=DigestDB(existing=9,existing_content={'items':[{'title':'Mice'}],'revision':3,'editorial_version':1})
        digest.generate(conn,self.now)
        self.assertTrue(conn.refreshed)
        self.assertEqual(conn.saved['editorial_version'],digest.EDITORIAL_VERSION)
        self.assertEqual(conn.saved['revision'],4)
        self.assertEqual(conn.saved['items'],[])
        conn.refreshed=False
        digest.generate(conn,self.now)
        self.assertFalse(conn.refreshed)

    def test_telegram_hides_versions_and_labels_reader_sections(self):
        content={'items':[{'title':'Уход','summary':'Данные','significance':'Смысл','limitations':'Аннотация','sources':[{'url':'https://example.org','kind':'research'}]}],
                 'conclusion_updates':[{'topic':'Клетки','version':1}]}
        text=format_digest('2026-11-02',content)
        self.assertIn('• Уход',text)
        self.assertIn('Данные',text)
        self.assertNotIn('Смысл',text)
        self.assertIn('Аннотация',text)
        self.assertIn('https://example.org',text)
        self.assertNotIn('версия',text)
        self.assertNotIn('research',text)

    def test_digest_batches_five_items_with_links_preserved(self):
        content={'items':[{'summary':f'Материал {i}','sources':[{'url':f'https://example.org/{i}','name':'Источник'}]} for i in range(10)]}
        parts=chunks(format_digest('2026-11-02',content))
        self.assertEqual(len(parts),2)
        self.assertTrue(all(part.count('• ')==5 for part in parts))
        for i in range(10):
            self.assertIn(f'https://example.org/{i}', '\n'.join(parts))

    def test_digest_summary_is_clickable_with_utf16_offsets(self):
        text=format_digest('2026-11-02',{'items':[{'summary':'Уход 🌿','sources':[{'url':'https://example.org'}]}]})
        with patch('bot.main.api') as api:
            send(123,text)
        payload=api.call_args.args[1]
        entity=payload['entities'][0]
        self.assertEqual(entity['url'],'https://example.org')
        encoded=text.encode('utf-16-le')
        self.assertEqual(encoded[entity['offset']*2:(entity['offset']+entity['length'])*2].decode('utf-16-le'),'Уход 🌿')

    def test_empty_answer_never_calls_model(self):
        with patch.object(llm,'call') as model:
            result=research.grounded(None,'answer','question',[])
            self.assertEqual(result['claims'],[])
            self.assertTrue(result['missing_data'])
            model.assert_not_called()

    def test_catalogues_and_individual_incidents_never_reach_digest_model(self):
        rows=[(1,'Cream','https://world.openbeautyfacts.org/product/123','Claim','product',{}),
              (2,'Incident','https://www.accessdata.fda.gov/scripts/cdrh/cfdocs/cfmaude/detail.cfm?id=123','Report','regulatory',{}),
              (3,'Launch','https://example.org/launch','Ad','commercial',{})]
        self.assertTrue(all(not digest.digest_eligible({'url':row[2],'kind':row[4]}) for row in rows))
        conn=DigestDB(existing=9,existing_content={'items':[{'title':'Old products'}],'editorial_version':2})
        with patch.object(llm,'call') as model:
            digest.generate(conn,self.now)
        model.assert_not_called()
        self.assertEqual(conn.saved['items'],[])
        self.assertEqual(conn.saved['editorial_version'],digest.EDITORIAL_VERSION)

    def test_official_recall_and_independent_product_research_remain_eligible(self):
        for url,kind in [('https://www.fda.gov/safety/recalls-market-withdrawals-safety-alerts/example','regulatory'),
                         ('https://europepmc.org/article/MED/123','research')]:
            self.assertTrue(digest.digest_eligible({'url':url,'kind':kind}))

    def test_generation_and_verification_receive_anti_advertising_rules(self):
        # This unit exercises editorial prompts at an explicit candidate boundary;
        # it makes no claim to cover SQL admission or persistence (real PG does).
        class EditorialDB(DigestDB):
            def execute(self,sql,args=None):
                if 'pg_try_advisory_lock' in sql: return Result([(True,)])
                if 'FROM findings f JOIN materials' in sql:
                    return Result([(4,'Adult facial study','https://example.org/study','research',
                        'research_candidate',None,'abstract','Источник','text-v1','facts-v1',
                        [{'id':11,'statement':'Synthetic adult facial observation','quote':'Synthetic source quote','payload':{}}])])
                return super().execute(sql,args)
        result={'items':[{'material_ids':[4],'fact_ids':[11],'title':'Наблюдение в исследовании','summary':'Результаты исследования.'}]}
        with patch.object(llm,'call',side_effect=[result,{'items':[{'index':0,'valid':True,'code':'supported'}]}]) as model, \
                patch.object(digest,'_store_finding',return_value=True):
            self.real_prepare(EditorialDB())
        self.assertEqual(model.call_count,2)
        for call in model.call_args_list:
            self.assertIn(digest.READER_RULES,call.args[2])

    def test_delivery_preserves_long_text_in_bounded_messages(self):
        text='x'*20000
        parts=chunks(text)
        self.assertGreater(len(parts),1)
        self.assertEqual(''.join(parts),text)
        self.assertTrue(all(len(part.encode('utf-16-le'))//2<=3500 for part in parts))
