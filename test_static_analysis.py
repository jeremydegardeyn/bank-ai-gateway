"""Undefined names, across the whole repo.

This repo has no CI. FinChat does, and its pyflakes step caught a bug that had already
shipped here in identical form:

    except Exception as workload_err:
        pass
    ...
    print(f"... ({type(workload_err).__name__})")   # NameError

Python unbinds an `except ... as NAME` target at the end of the handler — deliberately,
to break the reference cycle. So the name is gone by the time the code that explains the
failure runs, and the explanation raises instead of printing. It fires only on the path
that already went wrong, which is the path least likely to be exercised.

Binding to a second name (`except Exception as e: workload_err = e`) keeps it. That is
easy to get right and just as easy to forget, which is what a static check is for.
"""
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent
SKIP = {".git", ".venv", "venv", "__pycache__", "node_modules"}


def _sources():
    return sorted(p for p in REPO.rglob("*.py")
                  if not SKIP.intersection(p.relative_to(REPO).parts))


def test_no_undefined_names():
    files = [str(p) for p in _sources()]
    assert files, "no Python sources found — has the layout changed?"
    result = subprocess.run([sys.executable, "-m", "pyflakes", *files],
                            capture_output=True, text=True)
    bad = [line for line in result.stdout.splitlines()
           if "undefined name" in line or "unable to detect undefined names" in line]
    assert not bad, "undefined names:\n" + "\n".join(bad)
