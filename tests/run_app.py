"""Run the app with a debug screenshot; dump Python stacks if it hangs.

Usage: python3 tests/run_app.py OUT.png DELAY TIMEOUT [files...]
"""
import os
import signal
import subprocess
import sys

root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
out, delay, timeout, *files = sys.argv[1:]
code = (
    "import faulthandler, signal, sys;"
    "faulthandler.register(signal.SIGUSR2, all_threads=True);"
    f"sys.path.insert(0, {root!r});"
    "from gnome_markdown_editor.application import main;"
    "sys.exit(main(['gnome-markdown-editor', *sys.argv[1:]]))"
)
env = dict(os.environ, GME_SCREENSHOT=f"{out}:{delay}", GME_NON_UNIQUE="1")
p = subprocess.Popen([sys.executable, "-X", "faulthandler", "-c", code, *files], env=env,
                     stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
try:
    output, _ = p.communicate(timeout=float(timeout))
except subprocess.TimeoutExpired:
    p.send_signal(signal.SIGUSR2)
    try:
        output, _ = p.communicate(timeout=3)
    except subprocess.TimeoutExpired:
        p.kill()
        output, _ = p.communicate()
    print("TIMEOUT")
print(output[-6000:])
print("exit:", p.returncode)
