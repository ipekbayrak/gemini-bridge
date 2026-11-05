"""Contract tests using the official agy event envelope observed on this Mac."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

BRIDGE = Path(__file__).resolve().parents[1] / 'scripts/bridge.py'
spec = importlib.util.spec_from_file_location('native_bridge', BRIDGE)
bridge = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bridge)

class NativeEvents(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.ws = self.root/'workspace'; self.ws.mkdir()
        self.bin = self.root/'bin'; self.bin.mkdir()
        self.env = dict(os.environ, PATH=str(self.bin)+os.pathsep+os.environ['PATH'], AGY_BRIDGE_STATE=str(self.root/'state'))
        self.task = self.root/'task.txt'; self.task.write_text('Do the bounded test.')
        self.jobs = []
    def tearDown(self):
        for j in self.jobs:
            self.cli('cancel', j)
        self.tmp.cleanup()
    def fake(self, body):
        p=self.bin/'agy';p.write_text('#!'+sys.executable+'\nimport json,sys,time,os\n'+body+'\n');p.chmod(0o755)
    def cli(self,*args):
        p=subprocess.run([sys.executable,str(BRIDGE),*args],env=self.env,capture_output=True,text=True,timeout=30)
        return p.returncode,json.loads(p.stdout)
    def run_job(self,profile='heavy'):
        code,j=self.cli('start','--workspace',str(self.ws),'--task-file',str(self.task),'--profile',profile,'--timeout','30')
        self.assertEqual(code,0,j);self.jobs.append(j['job_id'])
        for _ in range(80):
            _,s=self.cli('status',j['job_id'])
            if s['status'] in bridge.TERMINAL_STATUSES:break
            time.sleep(.05)
        _,r=self.cli('result',j['job_id']);return r
    def test_actual_event_envelope_success_and_model(self):
        self.fake('''assert sys.argv[sys.argv.index('--model')+1]=='gemini-4-argon-high'
print(json.dumps({'event':'step_update','step_update':{'state':'DONE','tool_name':'write_to_file','step_type':'tool'}}))
print(json.dumps({'event':'result','result':{'status':'SUCCESS','response':'Native result','usage':{'input_tokens':12,'output_tokens':3}}}))''')
        r=self.run_job();self.assertEqual(r['status'],'succeeded',r);self.assertEqual(r['response'],'Native result');self.assertEqual(r['usage']['input_tokens'],12);self.assertEqual(r['last_tool'],'write_to_file');self.assertFalse(r['verified'])
    def test_structured_denial_without_stderr(self):
        self.fake("print(json.dumps({'event':'result','result':{'status':'SUCCESS','response':'','denied_actions':[{'action':'command','display_name':'RunCommand'}]}}))")
        r=self.run_job();self.assertEqual(r['status'],'needs_review',r)
    def test_non_success_terminal_is_not_success(self):
        self.fake("print(json.dumps({'event':'result','result':{'status':'WAITING','response':'Need approval'}}))")
        r=self.run_job();self.assertNotEqual(r['status'],'succeeded',r)
    def test_medium_model_and_literal_prompt(self):
        self.task.write_text('Literal $(touch SHOULD_NOT_EXIST) `echo unsafe`')
        self.fake('''assert sys.argv[sys.argv.index('--model')+1]=='gemini-4-argon-medium'
assert '$(touch SHOULD_NOT_EXIST)' in sys.argv[sys.argv.index('-p')+1]
print(json.dumps({'event':'result','result':{'status':'SUCCESS','response':'medium'}}))''')
        r=self.run_job('medium');self.assertEqual(r['status'],'succeeded',r);self.assertFalse((self.ws/'SHOULD_NOT_EXIST').exists())
    def test_success_envelope_on_cli_print_timeout_is_timeout(self):
        self.fake("print(json.dumps({'event':'result','result':{'status':'SUCCESS','response':''}}));sys.stderr.write('[agy] print timeout after 30s with turn in progress; returning partial output\\n')")
        r=self.run_job();self.assertEqual(r['status'],'timed_out',r)
    def test_empty_success_needs_review(self):
        self.fake("print(json.dumps({'event':'result','result':{'status':'SUCCESS','response':''}}))")
        r=self.run_job();self.assertEqual(r['status'],'needs_review',r)
    def test_reconcile_never_signals_persisted_pid(self):
        with patch.dict(os.environ,self.env):
            j='job-stale';d=bridge.get_state_dir()/'jobs'/j;d.mkdir()
            bridge._atomic_write_json(d/'state.json',{'job_id':j,'workspace':str(self.ws),'status':'running','worker_pid':99999999,'pid':os.getpid()})
            with patch.object(bridge.os,'killpg') as kill:
                r=bridge._load_and_reconcile_job(j)
                kill.assert_not_called()
                self.assertEqual(r['status'],'interrupted')

    def test_policy_hook_tool_error_cannot_be_success(self):
        self.fake("print(json.dumps({'event':'step_update','step_update':{'state':'ERROR','step_type':'tool','tool_name':'run_command','tool_info':{'parameters':{'CommandLine':'gradlew test'},'error':{'type':'TOOL_ERROR','message':'tool call denied by pre-tool hook: security policy'}}}}));print(json.dumps({'event':'result','result':{'status':'SUCCESS','response':'Stopped after denial'}}))")
        r=self.run_job();self.assertEqual(r['status'],'needs_review',r);self.assertTrue(r['permission_denials']);self.assertEqual(r['tool_failures'][0]['tool'],'run_command')
    def test_nonzero_tool_exit_requires_review(self):
        self.fake("print(json.dumps({'event':'step_update','step_update':{'state':'DONE','step_type':'tool','tool_name':'run_command','tool_info':{'parameters':{'CommandLine':'gradlew test'},'output':'Command exited with code 1. Could not connect to daemon'}}}));print(json.dumps({'event':'result','result':{'status':'SUCCESS','response':'Build did not pass'}}))")
        r=self.run_job();self.assertEqual(r['status'],'needs_review',r);self.assertEqual(r['tool_failures'][0]['exit_code'],1)
    def test_successful_same_command_retry_clears_tool_failure(self):
        self.fake("\nfor code in [1,0]: print(json.dumps({'event':'step_update','step_update':{'state':'DONE','step_type':'tool','tool_name':'run_command','tool_info':{'parameters':{'CommandLine':'test'},'output':f'Command exited with code {code}.'}}}))\nprint(json.dumps({'event':'result','result':{'status':'SUCCESS','response':'Retest passed'}}))")
        r=self.run_job();self.assertEqual(r['status'],'succeeded',r);self.assertFalse(r['tool_failures'])

if __name__=='__main__':unittest.main()
