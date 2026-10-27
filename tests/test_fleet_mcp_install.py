import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(sys.argv.pop(1)).resolve() if len(sys.argv) > 1 else Path(__file__).resolve().parents[1]
sys.dont_write_bytecode = True
spec = importlib.util.spec_from_file_location("fleet_mcp", ROOT / "lib/fleet_mcp.py")
fleet = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fleet)

ORIGINAL = '''import json,os,pathlib,sys
with pathlib.Path(os.environ["FLEET_TEST_LOG"]).open("a") as output:
 output.write("original\\n")
print(json.dumps({"args":sys.argv[1:],"cwd":os.getcwd(),"env":dict(os.environ),"input":sys.stdin.read()}))
sys.exit(int(os.environ.get("FLEET_TEST_EXIT","0")))
'''
PROXY = '''import os,pathlib,sys
assert sys.argv[1]=="--"
assert sys.flags.isolated and sys.flags.ignore_environment and sys.dont_write_bytecode
with pathlib.Path(os.environ["FLEET_TEST_LOG"]).open("a") as output:
 output.write("proxy\\n")
os.execv(sys.argv[2],sys.argv[2:])
'''


class McpInstallTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.home = self.root / "home ü ' $;"
        self.home.mkdir()
        self.cwd = self.root / "project ü with spaces"
        self.cwd.mkdir()
        self.helper = self.home / ".local/share/fleet/current/lib/fleet_mcp_proxy.py"
        self.log = self.root / "starts.log"
        self.original = self.root / "original ü ' $;.py"
        self.original.write_text(ORIGINAL)
        self.command = [sys.executable, "-B", str(self.original), "", "two words", "ü 日本語",
                        "$(touch injected)", "`touch injected`", "; touch injected;", "*", "'\"\\\n$HOME", "--"]
        self.env = {"HOME": str(self.home), "PATH": "/usr/bin:/bin", "LC_ALL": "C.UTF-8",
                    "FLEET_TEST_LOG": str(self.log), "FLEET_TEST_VALUE": "value ü ' $;\nnext"}

    def install_helper(self, source=PROXY):
        self.helper.parent.mkdir(parents=True)
        self.helper.write_text(source)

    def run_registration(self, registration, environment=None):
        return subprocess.run([registration["command"], *registration["args"]], cwd=self.cwd,
                              env=self.env if environment is None else environment,
                              input="request ü\n", capture_output=True, text=True, timeout=10)

    def check_original(self, result, starts, environment=None, status=0):
        self.assertEqual(result.returncode, status, result.stderr)
        self.assertEqual(result.stderr, "")
        output = json.loads(result.stdout)
        self.assertEqual(output["args"], self.command[3:])
        self.assertEqual(Path(output["cwd"]).resolve(), self.cwd.resolve())
        self.assertEqual(output["input"], "request ü\n")
        for name, value in (self.env if environment is None else environment).items():
            self.assertEqual(output["env"].get(name), value)
        self.assertEqual(self.log.read_text().splitlines(), starts)
        self.assertFalse((self.cwd / "injected").exists())

    def test_descriptor_uses_one_fixed_script_and_positional_argv(self):
        original = self.command.copy()
        registration = fleet.proxy_registration(self.home, self.command)
        alternate = fleet.proxy_registration(self.root / "other home", ["other", "$(false)"], python="/other python")
        self.assertEqual(set(registration), {"command", "args"})
        self.assertEqual(registration["command"], "/bin/sh")
        arguments = registration["args"]
        self.assertEqual(arguments[:1], ["-c"])
        self.assertIsInstance(arguments[1], str)
        self.assertEqual(arguments[1], alternate["args"][1])
        self.assertEqual(arguments[2:], ["fleet-mcp", str(self.helper), sys.executable, *self.command])
        self.assertEqual(alternate["args"][2:], ["fleet-mcp", str(self.root / "other home/.local/share/fleet/current/lib/fleet_mcp_proxy.py"),
                                               "/other python", "other", "$(false)"])
        self.assertEqual(self.command, original)

    def test_missing_helper_runs_original_once(self):
        self.check_original(self.run_registration(fleet.proxy_registration(self.home, self.command)), ["original"])

    def test_present_helper_runs_proxy_then_original_once(self):
        self.install_helper()
        python = self.root / "python ü ' $;"
        python.symlink_to(sys.executable)
        registration = fleet.proxy_registration(self.home, self.command, python=str(python))
        self.check_original(self.run_registration(registration), ["proxy", "original"])

    def test_eager_one_bypasses_proxy_but_zero_does_not(self):
        self.install_helper()
        registration = fleet.proxy_registration(self.home, self.command)
        for value, starts in (("1", ["original"]), ("0", ["proxy", "original"])):
            with self.subTest(eager=value):
                self.log.write_text("")
                environment = {**self.env, "FLEET_MCP_EAGER": value}
                self.check_original(self.run_registration(registration, environment), starts, environment)

    def test_missing_or_non_executable_python_runs_original_once(self):
        self.install_helper()
        for present in (False, True):
            with self.subTest(present=present):
                python = self.root / "unavailable python"
                if present:
                    python.write_text("not executable")
                    python.chmod(0o600)
                self.log.write_text("")
                registration = fleet.proxy_registration(self.home, self.command, python=str(python))
                self.check_original(self.run_registration(registration), ["original"])

    def test_started_proxy_failure_does_not_replay_original(self):
        self.install_helper('import os,pathlib,sys\npathlib.Path(os.environ["FLEET_TEST_LOG"]).write_text("proxy\\n")\nsys.exit(23)\n')
        result = self.run_registration(fleet.proxy_registration(self.home, self.command))
        self.assertEqual(result.returncode, 23, result.stderr)
        self.assertEqual(result.stdout, "")
        self.assertEqual(self.log.read_text().splitlines(), ["proxy"])

    def test_started_original_failure_does_not_replay(self):
        self.install_helper()
        environment = {**self.env, "FLEET_TEST_EXIT": "23"}
        registration = fleet.proxy_registration(self.home, self.command)
        self.check_original(self.run_registration(registration, environment), ["proxy", "original"], environment, 23)

    def test_cli_outputs_json_without_running_command(self):
        bootstrap = '''import pwd,runpy,sys,types
home,script,root,*command=sys.argv[1:]
pwd.getpwuid=lambda uid:types.SimpleNamespace(pw_dir=home)
sys.argv=[script,root,"mcp","command","--",*command]
runpy.run_path(script,run_name="__main__")
'''
        result = subprocess.run([sys.executable, "-B", "-I", "-c", bootstrap, str(self.home),
                                 str(ROOT / "lib/fleet_mcp.py"), str(ROOT), *self.command],
                                cwd=self.cwd, env=self.env, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")
        self.assertEqual(json.loads(result.stdout), fleet.proxy_registration(self.home, self.command))
        self.assertFalse(self.log.exists())

    def test_public_proxy_keeps_native_path_and_working_directory(self):
        python = self.root / "native-python"
        python.symlink_to(sys.executable)
        environment = {**self.env, "PATH": str(self.root) + ":/usr/bin:/bin", "FLEET_MCP_EAGER": "1"}
        registration = {"command": str(ROOT / "bin/fleet"),
                        "args": ["mcp", "proxy", "--", "native-python", *self.command[1:]]}
        self.check_original(self.run_registration(registration, environment), ["original"], environment)


if __name__ == "__main__":
    unittest.main()
