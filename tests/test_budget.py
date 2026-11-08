"""Execute the production aggregate SQL against fixture rows, without model calls."""
import sqlite3
import unittest
import os
from decimal import Decimal
from unittest.mock import patch
import llm
from llm import MONTHLY_SPEND_SQL

class BudgetTests(unittest.TestCase):
    def setUp(self):
        self.conn=sqlite3.connect(':memory:')
        self.conn.create_function('now',0,lambda:'2026-11-02T12:00:00')
        self.conn.create_function('date_trunc',2,lambda unit,date:'2026-11-01T00:00:00')
        self.conn.execute('CREATE TABLE model_calls(reserved_usd REAL,actual_usd REAL,created_at TEXT)')
    def tearDown(self): self.conn.close()

    def test_completed_calls_use_actual_cost_pending_calls_keep_reserve(self):
        self.conn.executemany('INSERT INTO model_calls VALUES (?,?,?)',[(1,0.01,'2026-11-02T10:00:00'),(1,None,'2026-11-02T11:00:00')])
        self.assertAlmostEqual(self.conn.execute(MONTHLY_SPEND_SQL).fetchone()[0],1.01)

    def test_zero_actual_cost_is_not_replaced_by_reserve_and_old_month_excluded(self):
        self.conn.executemany('INSERT INTO model_calls VALUES (?,?,?)',[(1,0,'2026-11-02T10:00:00'),(1,100,'2026-10-31T10:00:00')])
        self.assertEqual(self.conn.execute(MONTHLY_SPEND_SQL).fetchone()[0],0)

    def summary(self,rows):
        self.conn.executemany('INSERT INTO model_calls VALUES (?,?,?)',rows)
        statements=[]
        self.conn.set_trace_callback(statements.append)
        try:
            with patch.dict(os.environ,{'MODEL_MONTHLY_USD':'10','MODEL_CALL_RESERVE_USD':'1'}), \
                    patch.object(llm.db,'connect',side_effect=AssertionError('Summary must use the supplied connection')), \
                    patch.object(llm.urllib.request,'urlopen',side_effect=AssertionError('No external calls from budget diagnostics')):
                result=llm.budget_summary(self.conn)
        finally:
            self.conn.set_trace_callback(None)
        self.assertEqual(len(statements),1)
        self.assertTrue(statements[0].lstrip().upper().startswith('SELECT'))
        self.assertEqual(result['accounted_usd'],Decimal(str(self.conn.execute(MONTHLY_SPEND_SQL).fetchone()[0])))
        for key in ('actual_usd','unknown_reserved_usd','accounted_usd','monthly_limit_usd','next_reserve_usd','available_usd'):
            self.assertIsInstance(result[key],Decimal)
        self.assertEqual(result['monthly_limit_usd'],Decimal('10'))
        self.assertEqual(result['next_reserve_usd'],Decimal('1'))
        self.assertRegex(result['reason'],'[А-Яа-я]')
        return result

    def test_budget_summary_explains_five_known_plus_five_unknown_as_blocked(self):
        rows=[(1,5,'2026-11-02T10:00:00')]+[(1,None,'2026-11-02T11:00:00')]*5
        result=self.summary(rows)
        self.assertEqual(result['actual_usd'],Decimal('5'))
        self.assertEqual(result['unknown_reserved_usd'],Decimal('5'))
        self.assertEqual(result['unknown_calls'],5)
        self.assertEqual(result['accounted_usd'],Decimal('10'))
        self.assertEqual(result['available_usd'],Decimal('0'))
        self.assertFalse(result['allowed'])

    def test_budget_summary_allows_next_one_dollar_after_five_known_only(self):
        result=self.summary([(1,5,'2026-11-02T10:00:00')])
        self.assertEqual(result['actual_usd'],Decimal('5'))
        self.assertEqual(result['unknown_reserved_usd'],Decimal('0'))
        self.assertEqual(result['unknown_calls'],0)
        self.assertEqual(result['available_usd'],Decimal('5'))
        self.assertTrue(result['allowed'])

    def test_budget_summary_zero_actual_is_not_a_reserve_and_old_month_is_excluded(self):
        result=self.summary([(7,0,'2026-11-02T10:00:00'),(3,None,'2026-11-02T11:00:00'),
                             (1,100,'2026-10-31T10:00:00')])
        self.assertEqual(result['actual_usd'],Decimal('0'))
        self.assertEqual(result['unknown_reserved_usd'],Decimal('3'))
        self.assertEqual(result['unknown_calls'],1)
        self.assertEqual(result['accounted_usd'],Decimal('3'))
        self.assertEqual(result['available_usd'],Decimal('7'))
        self.assertTrue(result['allowed'])
