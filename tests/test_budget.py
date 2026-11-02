"""Execute the production aggregate SQL against fixture rows, without model calls."""
import sqlite3
import unittest
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
