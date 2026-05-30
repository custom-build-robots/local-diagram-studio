#!/usr/bin/env python
"""
diagrams_runner.py — executes user-supplied `diagrams` (mingrammer) Python code
in isolation and produces a PNG.

Run by the dedicated `.venv-diagrams` interpreter in an isolated subprocess
(`python -I`), inside a temp working directory, with a stripped environment and
a timeout — see DiagramsEngine in app.py. It is NOT imported by the app.

    python -I diagrams_runner.py <user_code.py> <out_basename>

Regardless of what the user code passes to Diagram(...), we force:
  show=False         (never pop open an image viewer)
  filename=<base>    (deterministic output name)
  outformat="png"    (self-contained raster; SVG would reference icon files by
                      path and not embed, so it can't be shown inline over HTTP)

Exit 0 on success (writes <out_basename>.png in cwd); non-zero with a message on
stderr otherwise.
"""

import runpy
import sys


def main() -> int:
    if len(sys.argv) != 3:
        print("usage: diagrams_runner.py <user_code.py> <out_basename>", file=sys.stderr)
        return 2
    code_path, out_base = sys.argv[1], sys.argv[2]

    try:
        import diagrams
    except Exception as e:  # pragma: no cover
        print(f"diagrams not importable: {e}", file=sys.stderr)
        return 3

    # Force safe/deterministic Diagram defaults no matter what the user wrote.
    _orig_init = diagrams.Diagram.__init__

    def _patched_init(self, *args, **kwargs):
        kwargs["show"] = False
        kwargs["filename"] = out_base
        kwargs["outformat"] = "png"
        return _orig_init(self, *args, **kwargs)

    diagrams.Diagram.__init__ = _patched_init

    try:
        runpy.run_path(code_path, run_name="__main__")
    except SystemExit:
        pass
    except Exception as e:
        print(f"{type(e).__name__}: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
