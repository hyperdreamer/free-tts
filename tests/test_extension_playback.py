from pathlib import Path
import subprocess
ROOT = Path(__file__).resolve().parents[1]

def run_node(argv):
    completed = subprocess.run(argv, cwd=ROOT, capture_output=True, text=True, timeout=120, check=False)
    assert completed.returncode == 0, (
        f"command failed ({completed.returncode}): {argv}\n"
        f"stdout:\n{completed.stdout}\nstderr:\n{completed.stderr}"
    )
    return completed.stdout

def test_extension_playback_node_suite():
    output = run_node(["node", "--test", "tests/extension_playback.test.cjs"])
    assert "W01" in output and "W31" in output

def test_extension_dom_smoke():
    output = run_node(["node", "tests/extension_dom_smoke.cjs", "--chromium", "/usr/bin/chromium"])
    assert "G01" in output and "G08" in output
