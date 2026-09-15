import unittest
from datetime import datetime,timedelta,timezone
from agent.growth_execution import GrowthExecutor,audit_opportunity,permitted_route,funnel,experiment_decision,channel_rank,fetch_owned
class GrowthPolicyTests(unittest.TestCase):
 def test_owned_bofu_opportunity(self):
  op=audit_opportunity('https://locenix.com/blog/google-maps-ranking-verbessern','<title>Maps</title><h1>Maps</h1>')
  self.assertEqual(op['potential_value'],'visibility_checks')
 def test_existing_attributed_cta_is_deduped(self):
  self.assertIsNone(audit_opportunity('https://locenix.com/blog/google-maps','<a href="/tools/local-seo-check?utm_campaign=a">Check</a>'))
 def test_approval_alone_is_not_consent(self):
  self.assertFalse(permitted_route({'Email Send Approved':True,'Email':'info@example.com'})[0])
 def test_consent_and_dnc(self):
  r={'Contact Route':'consented_email','Contact Permission Evidence':'crm:record:consent','Email Consent At':'2026-09-14'}
  self.assertTrue(permitted_route(r)[0]);r['Do Not Contact']=True;self.assertFalse(permitted_route(r)[0])
 def test_no_invented_conversion_rate(self):
  self.assertIsNone(funnel([{'metric_key':'visibility_checks','value':4}])['rates'][0]['rate'])
 def test_dropoff(self):
  rows=[{'metric_key':k,'value':v} for k,v in [('visitors',100),('visibility_checks',10),('trials',5),('customers',4)]]
  self.assertEqual(funnel(rows)['bottleneck']['to'],'visibility_checks')
 def test_tests_excluded(self):
  self.assertEqual(funnel([{'metric_key':'revenue','value':29,'dimensions':{'is_test':True}}])['totals']['revenue'],0)
 def test_downstream_beats_impressions(self):
  rows=[{'metric_key':'impressions','value':100000,'dimensions':{'channel':'A'}},{'metric_key':'trials','value':1,'dimensions':{'channel':'B'}}]
  self.assertEqual(channel_rank(rows)[0][0],'B')
 def test_deadlines_and_actions(self):
  now=datetime.now(timezone.utc)
  for observed,sample,expected in [(2,25,'SCALE'),(1,25,'ITERATE'),(0,25,'KILL'),(0,2,'PAUSE')]:
   self.assertEqual(experiment_decision(observed,2,sample,20,now-timedelta(seconds=1),now,'trials'),expected)
  self.assertIsNone(experiment_decision(0,2,0,20,now+timedelta(days=1),now,'trials'))
 def test_vanity_primary_blocked(self):
  now=datetime.now(timezone.utc)
  with self.assertRaises(ValueError):experiment_decision(100,1,20,20,now,now,'impressions')
 def test_ssrf_blocked(self):
  for url in ['http://locenix.com','https://127.0.0.1','https://locenix.com.evil.test','https://user@locenix.com']:
   with self.assertRaises(ValueError):fetch_owned(url)
 def test_controlled_worker_failure_happens_only_on_first_attempt(self):
  executor=GrowthExecutor(None)
  job={'attempt':0,'input':{'is_test':True,'failure_injection':'worker_failure_once'}}
  with self.assertRaisesRegex(RuntimeError,'INJECTED_WORKER_FAILURE'):executor.resilience_verify(job)
  job['attempt']=1
  executor.distribute=lambda current:{'evidence':'verified'}
  self.assertTrue(executor.resilience_verify(job)['recovery_verified'])
 def test_resilience_injection_requires_test_flag(self):
  executor=GrowthExecutor(None)
  with self.assertRaisesRegex(ValueError,'CONTROLLED_TEST_FLAG_REQUIRED'):
   executor.resilience_verify({'attempt':0,'input':{'failure_injection':'worker_failure_once'}})
if __name__=='__main__':unittest.main()
