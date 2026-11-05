import importlib.util,json,os,subprocess,sys,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('review_setup',ROOT/'scripts/setup.py')
setup=importlib.util.module_from_spec(spec);spec.loader.exec_module(setup)

class DistributionReview(unittest.TestCase):
 def test_node_engine_range(self):
  for version in ['v20.19.0','22.12.0','v24.12.0','v23.0.0']:
   self.assertTrue(setup.supported_node_version(version),version)
  for version in ['v18.20.0','v20.18.3','v21.0.0','v22.11.9','','node']:
   self.assertFalse(setup.supported_node_version(version),version)
 def test_url_permissions_use_hostnames(self):
  actual={r for r in setup.DEVELOPMENT_ALLOW_PERMISSIONS if r.startswith(('read_url(', 'execute_url('))}
  self.assertEqual(actual,{'read_url(localhost)','read_url(127.0.0.1)','execute_url(localhost)','execute_url(127.0.0.1)','read_url(registry.npmjs.org)'})
 def test_failed_chrome_update_does_not_remove_existing(self):
  with tempfile.TemporaryDirectory() as t:
   launcher=Path(t)/'launch-chrome.sh';launcher.write_text('stub')
   with patch.object(setup,'run_command',side_effect=[(0,'chrome-devtools stdio existing',''),(1,'','failed to update')]) as call:
    with self.assertRaises(setup.SetupError):setup.configure_chrome_mcp('agy',launcher,{},replace_chrome=True)
    self.assertEqual([x.args[0][1:3] for x in call.call_args_list],[['mcp','list'],['mcp','add']])
 def test_import_does_not_mutate_package_modes(self):
  with tempfile.TemporaryDirectory() as t:
   p=Path(t);script=p/'setup.py';script.write_bytes((ROOT/'scripts/setup.py').read_bytes())
   for name in ['setup.py','launch.sh','launch-chrome.sh']:
    f=p/name
    if not f.exists():f.write_text('stub')
    f.chmod(0o644)
   subprocess.run([sys.executable,'-c','import setup'],cwd=p,check=True,capture_output=True)
   for name in ['setup.py','launch.sh','launch-chrome.sh']:self.assertEqual((p/name).stat().st_mode&0o777,0o644)
 def test_marketplace_has_explicit_policy(self):
  with tempfile.TemporaryDirectory() as t:
   root=Path(t);plugin=root/'plugins/antigravity-bridge';plugin.mkdir(parents=True)
   (plugin/'plugin.json').write_text('{"version":"0.2.0"}')
   manifest=setup.write_marketplace_manifest(root,plugin)
   d=json.loads(manifest.read_text())['plugins'][0]
   self.assertEqual(d['policy']['installation'],'AVAILABLE');self.assertEqual(d['category'],'Developer Tools')

 def test_browser_setup_grants_named_server_without_general_command_access(self):
  with tempfile.TemporaryDirectory() as t:
   p=Path(t)/'settings.json'
   p.write_text(json.dumps({'unrelated':'keep','permissions':{'deny':['command(sudo)'],'ask':['mcp(other/server)'],'allow':['command(ls)']}}))
   first=setup.configure_browser_permissions(p);second=setup.configure_browser_permissions(p)
   d=json.loads(p.read_text());self.assertTrue(first['changed']);self.assertFalse(second['changed'])
   self.assertEqual(d['unrelated'],'keep');self.assertEqual(d['permissions']['deny'],['command(sudo)'])
   self.assertEqual(d['permissions']['ask'],['mcp(other/server)'])
   self.assertIn('mcp(chrome-devtools/*)',d['permissions']['allow'])
   self.assertNotIn('command(*)',d['permissions']['allow']);self.assertNotIn('mcp(*)',d['permissions']['allow'])
