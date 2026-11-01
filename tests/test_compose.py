"""Check actual Compose interpolation without a Docker daemon or real secrets."""
import json
import os
from pathlib import Path
import subprocess
import unittest

ROOT=Path(__file__).resolve().parent.parent
COMPOSE=ROOT/'downloads'/'docker-compose.exe'

@unittest.skipUnless(COMPOSE.exists(), 'Standalone Docker Compose is required')
class ComposeTests(unittest.TestCase):
    def config(self,**changes):
        env=dict(os.environ,POSTGRES_PASSWORD='testOnlyDatabasePassword32',APP_TOKEN='testOnlyApplicationToken32',
                 OPENROUTER_API_KEY='testOnlyModelKey',MODEL='test/model',MODEL_MONTHLY_USD='0',
                 MODEL_CALL_RESERVE_USD='1',ADMIN_TOKEN='testOnlyAdminToken32',TELEGRAM_TOKEN='testOnlyTelegramToken',
                 TELEGRAM_CHAT_IDS='123',NCBI_API_KEY='',OPENFDA_API_KEY='',MFDS_API_KEY='')
        env.update(changes)
        return subprocess.run([str(COMPOSE),'-f',str(ROOT/'docker-compose.yml'),'config','--format','json'],
                              cwd=ROOT,env=env,capture_output=True,text=True,timeout=30)

    def test_configuration_does_not_require_local_env_file(self):
        result=self.config()
        self.assertEqual(result.returncode,0,result.stderr)
        services=json.loads(result.stdout)['services']
        self.assertEqual(services['worker']['environment']['OPENROUTER_API_KEY'],'testOnlyModelKey')
        self.assertEqual(services['web']['environment']['APP_TOKEN'],'testOnlyApplicationToken32')
        self.assertEqual(services['bot']['environment']['TELEGRAM_CHAT_IDS'],'123')
        for service in services.values(): self.assertNotIn('env_file',service)
        self.assertNotIn('OPENROUTER_API_KEY',services['postgres']['environment'])
        self.assertNotIn('APP_TOKEN',services['bot']['environment'])

    def test_database_password_required(self):
        result=self.config(POSTGRES_PASSWORD='')
        self.assertNotEqual(result.returncode,0)
        self.assertIn('POSTGRES_PASSWORD',result.stderr)

    def test_web_access_token_required(self):
        result=self.config(APP_TOKEN='')
        self.assertNotEqual(result.returncode,0)
        self.assertIn('APP_TOKEN',result.stderr)
