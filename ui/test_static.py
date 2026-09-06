"""The UI is one HTML file with one inline <script>. If it does not parse, nothing works.

This is not a hypothetical. A single malformed string literal shipped to production and
took the whole block down — including `window.onload`, which is what initialises Google
sign-in. The page rendered its heading and its sign-in prompt and then did nothing, with
one line in the browser console and nothing at all on the server. Every backend health
check was green.

A syntax error in an inline script is silent in a way a Python one never is: there is no
import, no traceback, no failed request. So it needs a check that actually parses.
"""
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

INDEX = Path(__file__).resolve().parent / "static" / "index.html"


def _scripts() -> str:
    html = INDEX.read_text(encoding="utf-8")
    inline = re.findall(r"<script>(.*?)</script>", html, re.S)
    assert inline, "no inline <script> found — has the page been restructured?"
    # The server substitutes this before serving; leaving the placeholder in would be a
    # syntax error in the check and not in the page.
    return "\n".join(inline).replace("{{CLIENT_ID}}", "test-client-id")


def test_the_inline_script_parses():
    node = shutil.which("node")
    assert node, ("node is required to check the UI script. It is the only thing here "
                  "that can tell a broken page from a working one before a browser does.")
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False,
                                     encoding="utf-8") as f:
        f.write(_scripts())
        path = f.name
    try:
        result = subprocess.run([node, "--check", path], capture_output=True, text=True)
    finally:
        Path(path).unlink(missing_ok=True)
    assert result.returncode == 0, f"index.html script does not parse:\n{result.stderr}"


def test_every_function_the_markup_calls_is_defined():
    """`onclick="foo()"` referring to a function that no longer exists is a dead button.

    It fails only when someone clicks it, and it fails in the console rather than
    anywhere a test or a log would notice.
    """
    html = INDEX.read_text(encoding="utf-8")
    called = {m.group(1) for m in re.finditer(r'on\w+="(\w+)\(', html)}
    defined = set(re.findall(r"(?:async\s+)?function\s+(\w+)", _scripts()))
    missing = sorted(called - defined)
    assert not missing, f"markup calls undefined functions: {missing}"
