"""Telegram tests never make external requests or paid model calls."""
import os
import unittest
from contextlib import nullcontext
from unittest.mock import patch

import bot.main as bot


class Result:
    def __init__(self, rows=()): self.rows = list(rows)
    def fetchall(self): return self.rows
    def fetchone(self): return self.rows[0] if self.rows else None


class TelegramDB:
    def __init__(self):
        self.updates = {}
        self.offset = 0
        self.deliveries = {}
        self.digest = []

    def transaction(self): return nullcontext()

    def execute(self, sql, args=()):
        if 'INSERT INTO telegram_updates' in sql:
            uid, chat, response = args
            self.updates.setdefault(uid, {'chat': chat, 'status': 'received', 'response': response, 'next': 0})
        elif 'INSERT INTO telegram_state' in sql:
            self.offset = max(self.offset, args[0])
        elif 'SELECT value FROM telegram_state' in sql:
            return Result([(self.offset,)])
        elif 'FROM telegram_updates WHERE status' in sql:
            return Result([(uid, row['chat'], row['status'], row['response'], row['next']) for uid, row in sorted(self.updates.items()) if row['status'] in ('received','processing','ready')])
        elif 'UPDATE telegram_updates' in sql:
            row = self.updates[args[-1]]
            if 'SET attempts=' in sql:
                row['attempts']=row.get('attempts',0)+1
                row['status']='abandoned' if args[0] or row['attempts']>=5 else 'ready'
            elif "status='abandoned'" in sql: row['status']='abandoned'
            elif "status='processing'" in sql: row['status'] = 'processing'
            elif "status='ready'" in sql: row.update(status='ready', response=args[0])
            elif "status='sent'" in sql: row['status'] = 'sent'
            elif 'next_part=' in sql: row['next'] = args[0]
        elif 'SELECT id,day,content FROM digests' in sql:
            return Result(self.digest)
        elif 'INSERT INTO digest_deliveries' in sql:
            self.deliveries.setdefault(args, {'status': 'pending', 'next': 0,'revision':1})
        elif 'SELECT status,next_part,revision FROM digest_deliveries' in sql:
            row = self.deliveries[args]
            return Result([(row['status'], row['next'],row['revision'])])
        elif 'UPDATE digest_deliveries' in sql:
            row = self.deliveries[args[-2:]]
            if 'SET revision=' in sql: row.update(status='pending',next=0,revision=args[0])
            elif 'next_part=' in sql: row.update(status='sending', next=args[0])
            elif "status='sent'" in sql: row['status'] = 'sent'
            else: row['status'] = 'failed'
        return Result()


class TelegramTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {'TELEGRAM_CHAT_IDS': '12', 'TELEGRAM_TOKEN': 'fake'})
        self.env.start()
        self.json = patch.object(bot, 'Jsonb', side_effect=lambda value: value)
        self.json.start()

    def tearDown(self):
        self.json.stop(); self.env.stop()

    def test_chunks_preserve_entire_unicode_text(self):
        text = ('Процедуры 🔬🧴\n\n' * 1500) + 'Последний источник https://example.org'
        parts = bot.chunks(text)
        self.assertGreater(len(parts), 1)
        self.assertEqual(''.join(parts), text)
        self.assertTrue(all(bot.utf16_length(part) <= 3500 for part in parts))
        self.assertTrue(parts[-1].endswith('https://example.org'))

    def test_unbroken_emoji_string_respects_utf16(self):
        text = '🔬' * 5000
        parts = bot.chunks(text)
        self.assertEqual(''.join(parts), text)
        self.assertTrue(all(bot.utf16_length(part) <= 3500 for part in parts))

    def test_unauthorized_and_group_users_cannot_trigger_research(self):
        with patch.object(bot.research, 'answer') as answer:
            self.assertIn('Доступ', bot.command_response(None, 99, 'RF?'))
            self.assertIn('личном', bot.command_response(None, 12, 'RF?', 'group'))
            self.assertIn('99', bot.command_response(None, 99, '/id'))
            answer.assert_not_called()

    def test_allowed_question_uses_research(self):
        with patch.object(bot.research, 'answer', return_value={'claims': [{'text': 'Результат'}], 'sources': []}) as answer:
            text = bot.command_response(None, 12, 'Что известно?')
        answer.assert_called_once_with(None, 'Что известно?')
        self.assertIn('Результат', text)

    def test_replayed_update_uses_stored_answer(self):
        conn = TelegramDB()
        event = {'update_id': 7, 'message': {'chat': {'id': 12, 'type': 'private'}, 'text': 'Вопрос'}}
        bot.enqueue_updates(conn, [event, event])
        with patch.object(bot, 'command_response', return_value='Ответ') as generate, patch.object(bot, 'send') as send:
            bot.process_updates(conn)
            bot.enqueue_updates(conn, [event])
            bot.process_updates(conn)
        self.assertEqual(conn.offset, 8)
        self.assertEqual(generate.call_count, 1)
        self.assertEqual(send.call_count, 1)

    def test_failed_send_retries_without_paid_answer(self):
        conn = TelegramDB()
        bot.enqueue_updates(conn, [{'update_id': 1, 'message': {'chat': {'id': 12, 'type': 'private'}, 'text': 'Вопрос'}}])
        with patch.object(bot, 'command_response', return_value='Ответ') as generate, patch.object(bot, 'send', side_effect=[OSError(), None]):
            bot.process_updates(conn)
            bot.process_updates(conn)
        self.assertEqual(generate.call_count, 1)
        self.assertEqual(conn.updates[1]['status'], 'sent')

    def test_interrupted_paid_analysis_is_not_repeated(self):
        conn = TelegramDB()
        conn.updates[1] = {'chat': 12, 'status': 'processing', 'response': {}, 'next': 0}
        with patch.object(bot.research, 'answer') as answer, patch.object(bot, 'send') as send:
            bot.process_updates(conn)
        answer.assert_not_called()
        self.assertIn('прервался', send.call_args.args[1])

    def test_digest_resumes_after_last_confirmed_part(self):
        conn = TelegramDB()
        conn.digest = [(7, '2026-11-01', {'items': [], 'message': 'x' * 8000})]
        with patch.object(bot, 'send', side_effect=[None, OSError()]) as first:
            self.assertEqual(bot.deliver(conn), 0)
        self.assertEqual(conn.deliveries[(7, 12)]['next'], 1)
        with patch.object(bot, 'send') as resumed:
            self.assertEqual(bot.deliver(conn), 1)
        self.assertEqual(resumed.call_count, 2)
        self.assertNotEqual(resumed.call_args_list[0].args[1], first.call_args_list[0].args[1])

    def test_answer_has_all_sources_without_web_redirect(self):
        content = {'claims': [{'text': 'Есть ограничения'}], 'sources': [{'url': 'https://example.org', 'title': 'Статья', 'scope': 'abstract'}]}
        result = bot.format_answer(content)
        self.assertIn('изучена аннотация', result)
        self.assertIn('https://example.org', result)
        self.assertNotIn('веб-интерфейс', result)

    def test_revoked_access_blocks_cached_private_answer(self):
        conn=TelegramDB()
        conn.updates[1]={'chat':99,'status':'ready','response':{'parts':['private'],'restricted':True},'next':0}
        with patch.object(bot,'send') as send:
            bot.process_updates(conn)
        send.assert_not_called()
        self.assertEqual(conn.updates[1]['status'],'abandoned')

    def test_blocked_chat_does_not_poison_reply_queue(self):
        conn=TelegramDB()
        conn.updates[1]={'chat':12,'status':'ready','response':{'parts':['answer'],'restricted':True},'next':0}
        with patch.object(bot,'send',side_effect=bot.TelegramError(403)) as send:
            bot.process_updates(conn)
            bot.process_updates(conn)
        self.assertEqual(send.call_count,1)
        self.assertEqual(conn.updates[1]['status'],'abandoned')

    def test_refreshed_digest_is_delivered_after_waiting_status(self):
        conn=TelegramDB()
        conn.digest=[(7,'2026-11-02',{'items':[],'message':'Waiting'})]
        with patch.object(bot,'send') as send:
            bot.deliver(conn)
            bot.deliver(conn)
            self.assertEqual(send.call_count,1)
            conn.digest=[(7,'2026-11-02',{'items':[],'message':'New findings','revision':2})]
            bot.deliver(conn)
            bot.deliver(conn)
            self.assertEqual(send.call_count,2)
            self.assertIn('New findings',send.call_args.args[1])


if __name__ == '__main__':
    unittest.main()
