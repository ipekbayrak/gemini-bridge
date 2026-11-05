import importlib.util,json,os,shlex,subprocess,sys,tempfile,unittest
from pathlib import Path
spec=importlib.util.spec_from_file_location('command_setup',Path(__file__).resolve().parents[1]/'scripts/setup.py')
setup=importlib.util.module_from_spec(spec);spec.loader.exec_module(setup)
class CurrentCommand(unittest.TestCase):
 def test_managed_command_preserves_access_path_and_spaces(self):
  with tempfile.TemporaryDirectory(prefix='bridge command with spaces ') as t:
   root=Path(t);home=root/'home';source=root/'installed source';scripts=source/'scripts';scripts.mkdir(parents=True)
   code='import json,sys;print(json.dumps(sys.argv[1:]))'
   launcher=scripts/'launch.sh';launcher.write_text('#!/bin/sh\nexec '+shlex.quote(sys.executable)+' -c '+shlex.quote(code)+' "$@"\n');launcher.chmod(0o755)
   scope=root/'approved scope.json';scope.write_text('{"read_files":[]}');before=scope.read_bytes()
   r=setup.install_current_command(source,home);command=Path(r['path'])
   args=['start','--access-file',str(scope),'--task-file','task with spaces.txt']
   p=subprocess.run([str(command),*args],text=True,capture_output=True,check=True)
   self.assertEqual(json.loads(p.stdout),['--current',*args]);self.assertEqual(scope.read_bytes(),before)
   self.assertEqual(command.stat().st_mode&0o777,0o755)
   setup.install_current_command(source,home)
   self.assertEqual(subprocess.run([str(command),'--where'],capture_output=True,text=True,check=True).stdout.strip(),'["--current", "--where"]')
 def test_unrelated_command_and_symlink_are_not_overwritten(self):
  with tempfile.TemporaryDirectory() as t:
   root=Path(t);command=root/'home/.local/bin/gemini-bridge';command.parent.mkdir(parents=True)
   command.write_text('#!/bin/sh\necho unrelated\n');before=command.read_bytes()
   with self.assertRaises(setup.SetupError):setup.install_current_command(root/'source',root/'home')
   self.assertEqual(command.read_bytes(),before);command.unlink()
   target=root/'other';target.write_text('unrelated');command.symlink_to(target)
   with self.assertRaises(setup.SetupError):setup.install_current_command(root/'source',root/'home')
   self.assertTrue(command.is_symlink());self.assertEqual(target.read_text(),'unrelated')
