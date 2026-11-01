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
        env=dict(os.environ,SERVICE_PASSWORD_POSTGRES='testOnlyDatabasePassword32',SERVICE_PASSWORD_APP='testOnlyApplicationToken32',SERVICE_PASSWORD_ADMIN='testOnlyAdminToken32',
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

    def test_same_generated_database_password_used_by_all_clients(self):
        services=json.loads(self.config().stdout)['services']
        password=services['postgres']['environment']['POSTGRES_PASSWORD']
        for name in ('worker','web','bot'):
            self.assertEqual(services[name]['environment']['DATABASE_URL'],f'postgresql://skin:{password}@postgres:5432/skin_research')

    def test_generated_web_passwords_override_legacy_manual_names(self):
        result=self.config(POSTGRES_PASSWORD='',APP_TOKEN='',ADMIN_TOKEN='')
        self.assertEqual(result.returncode,0,result.stderr)
        web=json.loads(result.stdout)['services']['web']['environment']
        self.assertEqual(web['APP_TOKEN'],'testOnlyApplicationToken32')
        self.assertEqual(web['ADMIN_TOKEN'],'testOnlyAdminToken32')

    def test_compose_declares_coolify_generation_without_manual_required_fields(self):
        source=(ROOT/'docker-compose.yml').read_text(encoding='utf-8')
        for name in ('POSTGRES','APP','ADMIN'):
            self.assertIn('${SERVICE_PASSWORD_'+name+'}',source)
        self.assertNotIn(':?Set',source)
