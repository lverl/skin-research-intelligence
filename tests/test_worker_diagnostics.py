import os
import unittest
from unittest.mock import Mock,patch
import llm
from worker.main import process_new

class DiagnosticsTests(unittest.TestCase):
    def test_missing_model_settings_do_not_exhaust_document_retries(self):
        conn=Mock()
        with patch.dict(os.environ,{'MODEL':'','OPENROUTER_API_KEY':'','MODEL_MONTHLY_USD':'0'}):
            with patch('db.log') as log:
                process_new(conn)
        conn.execute.assert_not_called()
        self.assertIn('MODEL',log.call_args.args[1])
        self.assertIn('OPENROUTER_API_KEY',log.call_args.args[1])

    def test_diagnostics_do_not_include_secret_values(self):
        with patch.dict(os.environ,{'MODEL':'test/model','OPENROUTER_API_KEY':'private-test-key','MODEL_MONTHLY_USD':'0'}):
            issues=llm.configuration_issues()
        self.assertNotIn('private-test-key',' '.join(issues))
        self.assertIn('MODEL_MONTHLY_USD',' '.join(issues))

    def test_http_errors_are_actionable_without_server_body(self):
        from urllib.error import HTTPError
        error=HTTPError('https://example.org/private-test-key',401,'sensitive server message',{},None)
        message=llm.safe_error(error)
        error.close()
        self.assertIn('401',message)
        self.assertNotIn('private-test-key',message)
        self.assertNotIn('sensitive',message)
