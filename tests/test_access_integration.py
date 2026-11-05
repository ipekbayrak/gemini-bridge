"""Read scopes cross the actual CLI/MCP boundary without using provider quota."""
import json,os,stat,subprocess,sys,unittest
import test_native_events as native
from pathlib import Path
from test_native_events import BRIDGE

class AccessIntegration(unittest.TestCase):
 tearDown=native.NativeEvents.tearDown
 fake=native.NativeEvents.fake
 cli=native.NativeEvents.cli
 run_job=native.NativeEvents.run_job
 def setUp(self):
  native.NativeEvents.setUp(self)
  home=self.root/'home';home.mkdir();self.env['HOME']=str(home)
  self.policy=home/'.gemini/antigravity-cli/settings.json';self.policy.parent.mkdir(parents=True)
  self.policy.write_text(json.dumps({'other':'keep','permissions':{'allow':['command(ls)'],'deny':['command(sudo)'],'ask':['mcp(other/tool)']}}))
  self.fixture=self.root/'public.json';self.fixture.write_text('{"fixture":true}')
  self.scope=self.root/'access.json';self.scope.write_text(json.dumps({'read_urls':[f'https://site{i}.example.invalid/page' for i in range(21)],'read_files':[str(self.fixture)]}))
 def test_scoped_read_cli_persists_private_session_only_settings(self):
  before=self.policy.read_bytes()
  self.fake('''from pathlib import Path
settings=Path(sys.argv[sys.argv.index('--settings')+1])
d=json.loads(settings.read_text());a=d['permissions']['allow']
assert len([r for r in a if r.startswith('read_url(')])==21
assert any(r=='read_file('+str(Path(os.environ['TEST_FIXTURE']).resolve())+')' for r in a)
assert not any(r.startswith(('execute_url(', 'write_file(')) for r in a)
assert d['permissions']['deny']==['command(sudo)']
assert d['permissions']['ask']==['mcp(other/tool)']
assert settings.stat().st_mode & 0o777==0o600
print(json.dumps({'event':'result','result':{'status':'SUCCESS','response':'Scoped fixture passed'}}))''')
  self.env['TEST_FIXTURE']=str(self.fixture)
  code,j=self.cli('start','--workspace',str(self.ws),'--task-file',str(self.task),'--access-file',str(self.scope))
  self.assertEqual(code,0,j);self.jobs.append(j['job_id'])
  import time
  for _ in range(100):
   _,s=self.cli('status',j['job_id'])
   if s['status']!='running':break
   time.sleep(.03)
  _,r=self.cli('result',j['job_id']);self.assertEqual(r['status'],'succeeded',r)
  self.assertEqual(r['access_summary']['read_url_count'],21);self.assertEqual(self.policy.read_bytes(),before)
 def test_conflicting_policy_cli_returns_structured_block_without_worker(self):
  self.policy.write_text(json.dumps({'permissions':{'deny':['read_url(example.invalid)']}}));before=self.policy.read_bytes()
  self.fake("raise AssertionError('provider must not start')")
  code,r=self.cli('start','--workspace',str(self.ws),'--task-file',str(self.task),'--access-file',str(self.scope))
  self.assertNotEqual(code,0);self.assertEqual(r['code'],'policy_conflict');self.assertTrue(r['blocked_resources'])
  self.assertEqual(self.policy.read_bytes(),before);self.assertFalse((self.root/'state/jobs').exists())
 def test_mcp_accepts_access_and_rejects_unknown_access_fields(self):
  request={'jsonrpc':'2.0','id':1,'method':'tools/call','params':{'name':'agy_start','arguments':{'workspace':str(self.ws),'task':'audit','access':{'write_files':[str(self.fixture)]}}}}
  p=subprocess.run([sys.executable,str(BRIDGE),'serve'],env=self.env,input=json.dumps(request)+'\n',capture_output=True,text=True,timeout=30)
  r=json.loads(p.stdout)['result'];self.assertTrue(r['isError']);self.assertEqual(r['structuredContent']['code'],'invalid_access')


 def test_runtime_read_denial_reports_exact_resource_without_retry(self):
  event={'event':'step_update','step_update':{'state':'ERROR','step_type':'tool','tool_name':'read_file','tool_info':{'parameters':{'Path':str(self.fixture)},'error':{'message':'permission denied by existing policy'}}}}
  result={'event':'result','result':{'status':'SUCCESS','response':'May I change permissions?'}}
  self.fake('print('+repr(json.dumps(event))+');print('+repr(json.dumps(result))+')')
  r=self.run_job()
  self.assertEqual(r['status'],'needs_review');self.assertEqual(r['next_action'],'report_blocker_no_retry')
  self.assertEqual(r['blocked_access'][0]['resource'],str(self.fixture))
