import importlib.util, os, tempfile, unittest
from pathlib import Path
from unittest.mock import patch
spec=importlib.util.spec_from_file_location("runtime_bridge",Path(__file__).resolve().parents[1]/"scripts/bridge.py")
bridge=importlib.util.module_from_spec(spec);spec.loader.exec_module(bridge)
class RuntimePaths(unittest.TestCase):
 def test_preserves_path_and_discovers_newest_nvm_first(self):
  with tempfile.TemporaryDirectory() as t:
   home=Path(t)
   for version in ("v9.9.9","v22.12.0","v24.12.0"):(home/".nvm/versions/node"/version/"bin").mkdir(parents=True)
   with patch.object(bridge.Path,"home",return_value=home),patch.dict(os.environ,{"PATH":"/explicit/bin","GEMINI_API_KEY":"fake"}):
    e=bridge.build_clean_env();paths=e["PATH"].split(os.pathsep)
    self.assertEqual(paths[0],"/explicit/bin");self.assertNotIn("GEMINI_API_KEY",e)
    found=[p for p in paths if "/.nvm/" in p]
    self.assertEqual([Path(p).parent.name for p in found],["v24.12.0","v22.12.0","v9.9.9"])
