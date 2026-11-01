"""Opt-in real PostgreSQL tests; fixture writes roll back, no live model/network."""
import os
import unittest
from datetime import datetime,timezone
from unittest.mock import patch
from uuid import uuid4

@unittest.skipUnless(os.environ.get('TEST_DATABASE_URL'), 'Set TEST_DATABASE_URL for a dedicated test database')
class PostgresTests(unittest.TestCase):
    def setUp(self):
        import psycopg
        import db
        self.conn=psycopg.connect(os.environ['TEST_DATABASE_URL'],autocommit=True)
        db.init_schema(self.conn)
        self.conn.execute('BEGIN')
        self.suffix=uuid4().hex

    def tearDown(self):
        self.conn.execute('ROLLBACK')
        self.conn.close()

    def item(self,**changes):
        return dict({'url':'https://example.org/'+self.suffix,'title':'RF skin test '+self.suffix,
                     'text':'Twelve participants completed follow-up. No adverse events were reported.',
                     'text_scope':'abstract'},**changes)

    def test_seed_preserves_admin_settings(self):
        from collector.main import seed_sources
        seed_sources(self.conn)
        self.assertEqual(self.conn.execute('SELECT count(*) FROM sources').fetchone()[0],12)
        self.conn.execute("UPDATE sources SET active=FALSE,interval_hours=72 WHERE name='Europe PMC'")
        seed_sources(self.conn)
        self.assertEqual(self.conn.execute("SELECT active,interval_hours FROM sources WHERE name='Europe PMC'").fetchone(),(False,72))

    def test_doi_dedup_keeps_provenance(self):
        from collector.main import ingest
        doi='10.1234/'+self.suffix
        first=ingest(self.conn,self.item(doi=doi))
        second=ingest(self.conn,self.item(url='https://example.org/other/'+self.suffix,doi=doi,text_scope='full_text'))
        self.assertEqual(first,second)
        self.assertEqual(self.conn.execute('SELECT count(*) FROM material_sources WHERE material_id=%s',(first,)).fetchone()[0],2)
        self.assertEqual(self.conn.execute('SELECT text_scope FROM materials WHERE id=%s',(first,)).fetchone()[0],'full_text')

    def test_web_search_filters_execute_against_real_database(self):
        from collector.main import ingest
        from web.main import list_materials
        mid=ingest(self.conn,self.item())
        rows=list_materials(self.conn,{'q':[self.suffix]})
        self.assertEqual([row['id'] for row in rows],[mid])

    def test_complete_analysis_answer_and_daily_digest(self):
        from collector.main import ingest
        from worker.main import process_material
        from research import answer
        from digest import generate
        import llm
        mid=ingest(self.conn,self.item())
        topic='RF '+self.suffix

        def model(conn,stage,instructions,data):
            if stage=='triage': return {'relevant':True,'important':True,'kind':'research','summary':'Тестовый пересказ','topics':[topic]}
            if stage=='extract': return {'facts':[{'statement':topic+': twelve participants','quote':'Twelve participants completed follow-up.','payload':{'sample_size':12}}],'limitations':['Только аннотация']}
            if stage=='verify': return {'accepted_indices':[0],'limitations':[]}
            if stage in ('synthesis','answer'):
                fid=data['facts'][0]['id']
                return {'claims':[{'text':'Тестовый вывод по цитате','fact_ids':[fid]}],'limitations':['Аннотация'],'contradictions':[],'missing_data':[]}
            if stage in ('synthesis_verify','answer_verify','digest_verify'): return {'valid':True}
            if stage=='search_plan': return {'query':topic,'terms':[self.suffix],'clarification':''}
            if stage=='digest': return {'items':[{'material_ids':[mid],'title':'Исследование','summary':'Тестовый пересказ','significance':'Новые данные','limitations':'Аннотация'}]}
            raise AssertionError(stage)

        with patch.object(llm,'call',side_effect=model),patch('collector.main.search',return_value={'processed':0,'failed_sources':[]}),patch.dict(os.environ,{'DIGEST_TIMEZONE':'UTC','DIGEST_HOUR':'0'}):
            process_material(self.conn,(mid,self.item()['title'],self.item()['text'],'abstract'))
            result=answer(self.conn,topic)
            self.assertEqual(result['sources'][0]['material_id'],mid)
            did=generate(self.conn,datetime.now(timezone.utc))
            self.assertEqual(did,generate(self.conn,datetime.now(timezone.utc)))
        self.assertEqual(self.conn.execute('SELECT count(*) FROM facts WHERE material_id=%s AND active',(mid,)).fetchone()[0],1)
        self.assertEqual(self.conn.execute('SELECT count(*) FROM conclusions WHERE topic=%s',(topic,)).fetchone()[0],1)
        self.assertEqual(self.conn.execute('SELECT content FROM digests WHERE id=%s',(did,)).fetchone()[0]['items'][0]['material_ids'],[mid])

    def test_reprocessing_retires_old_facts_and_versions_conclusion(self):
        from collector.main import ingest
        from worker.main import process_material
        from db import jsonb
        from research import synthesize
        import llm
        mid=ingest(self.conn,self.item())
        topic='RF '+self.suffix
        self.conn.execute("INSERT INTO findings(material_id,summary,model,prompt_version,topics,verified) VALUES (%s,'old','fixture','test',%s,TRUE)",(mid,jsonb([topic])))
        self.conn.execute('INSERT INTO facts(material_id,statement,quote) VALUES (%s,%s,%s)',(mid,topic,'Twelve participants completed follow-up.'))
        self.conn.execute("INSERT INTO conclusions(topic,version,content,reason) VALUES (%s,1,'{}','fixture')",(topic,))
        def model(conn,stage,instructions,data):
            if stage=='triage': return {'relevant':True,'important':True,'kind':'research','summary':'Пересмотр','topics':[topic]}
            if stage=='extract': return {'facts':[],'limitations':['Нет достаточных результатов']}
            if stage=='verify': return {'accepted_indices':[],'limitations':[]}
            raise AssertionError('No synthesis model call should be needed without facts: '+stage)
        with patch.object(llm,'call',side_effect=model):
            process_material(self.conn,(mid,self.item()['title'],self.item()['text'],'abstract'))
        self.assertEqual(self.conn.execute('SELECT count(*) FROM facts WHERE material_id=%s AND active',(mid,)).fetchone()[0],0)
        version,content=self.conn.execute('SELECT version,content FROM conclusions WHERE topic=%s ORDER BY version DESC LIMIT 1',(topic,)).fetchone()
        self.assertEqual(version,2)
        self.assertTrue(content['missing_data'])
