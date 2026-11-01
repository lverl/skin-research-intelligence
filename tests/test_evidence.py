import unittest
from evidence import validate_facts,validate_claims
from unittest.mock import patch
import net

class EvidenceTests(unittest.TestCase):
    def test_real_quote_and_whitespace(self):
        facts=[{'statement':'Observed result','quote':'Twelve participants completed follow-up.'}]
        self.assertEqual(len(validate_facts(facts,'Twelve participants\ncompleted follow-up.')),1)

    def test_invented_quote_is_rejected(self):
        with self.assertRaises(ValueError):
            validate_facts([{'statement':'Effect','quote':'A fabricated result from a trial.'}],'No results available.')

    def test_nonexistent_fact_cannot_support_answer(self):
        with self.assertRaises(ValueError):
            validate_claims({'claims':[{'text':'Works','fact_ids':[999]}]},{1,2})

    def test_each_claim_needs_evidence(self):
        with self.assertRaises(ValueError):
            validate_claims({'claims':[{'text':'Works','fact_ids':[]}]},{1})

    def test_private_addresses_rejected(self):
        for ip in ('127.0.0.1','169.254.169.254','10.0.0.1','::1'):
            with patch('socket.getaddrinfo',return_value=[(2,1,6,'',(ip,80))]):
                with self.assertRaises(ValueError): net.validate_url('http://example.org/test')

    def test_non_http_and_credentials_rejected(self):
        for url in ('file:///etc/passwd','http://user:password@example.org','ftp://example.org'):
            with self.assertRaises(ValueError): net.validate_url(url)
