import contextlib,importlib.util,io,json,tempfile,unittest,shutil,os
from pathlib import Path
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('safe_setup',ROOT/'scripts/setup.py');setup=importlib.util.module_from_spec(spec);spec.loader.exec_module(setup)
class InstallerSafety(unittest.TestCase):
 def test_command_conflict_is_rejected_before_copy_or_registration(self):
  with tempfile.TemporaryDirectory() as tmp:
   home=Path(tmp)/'home';cmd=home/'.local/bin/gemini-bridge';cmd.parent.mkdir(parents=True);cmd.write_text('owned by someone else')
   with patch.object(setup,'run_check',return_value={'ok':True}),patch.object(setup,'get_home_dir',return_value=home),patch.object(setup,'get_codex_home',return_value=home/'.codex'),patch.object(setup,'copy_plugin_allowlist') as copy,patch.object(setup,'register_codex_plugin') as register:
    with self.assertRaises(setup.SetupError):setup.run_install()
    copy.assert_not_called();register.assert_not_called()
   self.assertEqual(cmd.read_text(),'owned by someone else');self.assertFalse((home/'.codex').exists())
 def test_late_failure_reports_registered_plugin_and_completed_changes(self):
  with tempfile.TemporaryDirectory() as tmp:
   home=Path(tmp)/'home';home.mkdir()
   checks={'ok':True,'discovered_bin_dirs':[],'codex':{'executable':'fake'},'agy':{'executable':'fake'}}
   with patch.object(setup,'run_check',return_value=checks),patch.object(setup,'get_home_dir',return_value=home),patch.object(setup,'get_codex_home',return_value=home/'.codex'),patch.object(setup,'copy_plugin_allowlist',return_value={'copied':True}),patch.object(setup,'write_marketplace_manifest',return_value=home/'manifest'),patch.object(setup,'register_codex_plugin',return_value={}),patch.object(setup,'configure_chrome_mcp',side_effect=setup.SetupError('simulated failure',step='chrome')),contextlib.redirect_stdout(io.StringIO()) as output:
    self.assertEqual(setup.main(['install','--with-chrome','--json']),1)
   result=json.loads(output.getvalue());self.assertTrue(result['installed']);self.assertTrue(result['partial']);self.assertIn('codex_plugin_registered',result['completed_steps']);self.assertEqual(result['failed_step'],'chrome')
 def test_failed_source_swap_restores_previous_runtime(self):
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp);dest=root/'gemini-bridge/plugins/antigravity-bridge';dest.mkdir(parents=True);(dest/'old.txt').write_text('keep old runtime')
   original=setup.os.replace
   def replacement(src,target):
    if Path(src).name.startswith('.stage-') and Path(target)==dest:raise OSError('simulated rename failure')
    return original(src,target)
   with patch.object(setup.os,'replace',side_effect=replacement):
    with self.assertRaises(OSError):setup.copy_plugin_allowlist(ROOT,dest)
   self.assertEqual((dest/'old.txt').read_text(),'keep old runtime');self.assertFalse(list(dest.parent.glob('.stage-*')))
 def test_successful_install_records_actual_verified_cache_path(self):
  with tempfile.TemporaryDirectory() as tmp:
   home=Path(tmp);cache=home/'plugins/cache/gemini-bridge-local/antigravity-bridge/0.3.2';cache.mkdir(parents=True)
   (cache/'plugin.json').write_text(json.dumps({'name':'antigravity-bridge','version':'0.3.2'}))
   registration={'plugin_add':{'output':json.dumps({'installedPath':str(cache),'version':'0.3.2'})}}
   self.assertTrue(setup.record_current_runtime(home,registration))
   p=home/'plugin-marketplaces/gemini-bridge/active-runtime.json';d=json.loads(p.read_text())
   self.assertEqual(d['path'],str(cache.resolve()));self.assertEqual(p.stat().st_mode&0o777,0o600)
 def test_outside_cache_receipt_cannot_choose_executor(self):
  with tempfile.TemporaryDirectory() as tmp:
   home=Path(tmp);outside=home/'other';outside.mkdir();(outside/'plugin.json').write_text(json.dumps({'name':'antigravity-bridge','version':'0.3.2'}))
   with self.assertRaises(setup.SetupError):setup.record_current_runtime(home,{'plugin_add':{'output':json.dumps({'installedPath':str(outside),'version':'0.3.2'})}})
   self.assertFalse((home/'plugin-marketplaces/gemini-bridge/active-runtime.json').exists())
