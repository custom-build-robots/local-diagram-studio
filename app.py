"""
D2 Diagram Generator — Gradio web application.

Chat with a local OpenAI-compatible LLM (e.g. Ollama) to generate D2 diagram
code, render it live as inline animated SVG, and export SVG / MP4 / GIF.

Runs fully locally. See coding-prompt-d2-chat-app.md for the spec.

    pip install gradio openai pyyaml requests
    python app.py
"""

import os
import ast
import base64
import re
import shutil
import subprocess
import tempfile
import uuid
from pathlib import Path

import requests
import yaml
import gradio as gr
from openai import OpenAI

# --------------------------------------------------------------------------- #
# Paths & constants
# --------------------------------------------------------------------------- #
ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.yaml"
DEFAULT_AGENT_PATH = ROOT / "Draw-Agent.md"
DIAGRAMS_AGENT_PATH = ROOT / "Draw-Agent-Diagrams.md"
EXAMPLES_PATH = ROOT / "example.yaml"
AGENTS_DIR = ROOT / "agents"
DIAGRAMS_RUNNER = ROOT / "diagrams_runner.py"
DIAGRAMS_VENV = ROOT / ".venv-diagrams"

LLM_TIMEOUT = 120  # seconds — default; overridden by config["llm"]["timeout"]

# Diagram engines. The active engine is chosen by the agent's frontmatter
# (`engine: d2|diagrams`) unless overridden in config["app"]["engine_override"].
ENGINES = {
    "d2": {
        "key": "d2", "label": "D2 (animated)", "code_lang": "d2",
        "animated": True, "source": "diagram.d2", "render": "diagram.svg",
    },
    "diagrams": {
        "key": "diagrams", "label": "Diagrams (static)", "code_lang": "python",
        "animated": False, "source": "diagram.py", "render": "diagram.png",
    },
}

DEFAULT_CONFIG = {
    "llm": {
        "base_url": "http://192.168.2.57:11434/v1",
        "model": "qwen3.6:27b",
        "api_key": "ollama",
        "timeout": 120,  # seconds to wait for an LLM response
    },
    "app": {
        "d2_binary": "d2",
        "output_dir": "./output",
        "recorder_script": "./recorder/record.js",
        "active_agent": "Draw-Agent.md",
        "engine_override": "auto",  # auto (from agent) | d2 | diagrams
    },
    "export": {
        "fps": 20,            # capture target frames per second
        "duration_sec": 6.0,  # capture length (should cover one animation cycle)
        "max_width": 900,     # rendered width cap, px
        "speed": 1.0,         # playback speed; 1.0 = match the live SVG, <1 = slower
        "gif_fps": 15,        # GIF frame rate
        "gif_scale": 900,     # GIF width, px (height auto)
    },
}

DEFAULT_AGENT_CONTENT = """\
You are a diagram generation assistant. Your task is to generate valid D2
diagram code based on the user's description.

Rules:
- Always output the complete D2 diagram code inside a fenced code block
  marked with ```d2
- Use `style.animated: true` on connections to show data flow
- Use `<->` for bidirectional connections and `->` for unidirectional ones
- Group nodes into containers for logical zones (e.g. LAN segments)
- Use `\\n` inside labels for multi-line node text
- Keep labels concise: name on the first line, IP or port on the second line
- Always output the full diagram, not just the changed parts
- If the user asks to modify an existing diagram, base your output on the
  current diagram code shown in the conversation

Syntax (CRITICAL — D2 is NOT Graphviz/DOT or Mermaid):
- Set attributes with a BRACE block or a dotted key — NEVER square brackets.
    CORRECT:  node: { shape: package }       or   node.shape: package
    WRONG:    node [shape: package]
- Set a label inline or with a dotted key:
    node: "My Label"      node.label: "My Label"
    node: { label: "My Label"; shape: rectangle }
- Style a connection with a BRACE block — NEVER square brackets:
    CORRECT:  a -> b: "label" { style.animated: true }
    WRONG:    a -> b [style.animated: true]
- A container groups children inside braces:
    lan: { shape: package; web: { shape: rectangle } }
- Inside a brace block, separate entries with newlines or a semicolon.

Shapes (only these are valid in D2):
  rectangle, square, page, parallelogram, document, cylinder, queue,
  package, step, callout, stored_data, person, diamond, oval, circle,
  hexagon, cloud, text, code, class, sql_table, image
- NEVER invent shapes. `folder` and `box` are NOT valid D2 shapes.
  Use `package` for folders/groupings and `rectangle` for plain boxes.
- Use `shape: cylinder` for databases and storage nodes.

Correctness:
- Define every node before referencing it. Connection endpoints
  (`a -> b`) must use the EXACT node keys you declared — a typo silently
  creates an unwanted stray node, so double-check every name.

Follow this valid D2 as a structural template:

```d2
direction: right
lan: "Company LAN" {
  shape: package
  user: { shape: person; label: "Employee" }
  app: { shape: rectangle; label: "App Server\\n192.168.1.20" }
  db: { shape: cylinder; label: "Vector DB" }
  user -> app: "query" { style.animated: true }
  app <-> db: "lookup" { style.animated: true }
}
internet: { shape: cloud }
lan.app -> internet: "fetch" { style.animated: true }
```
"""

# Default agent for the Diagrams (mingrammer) engine. The `engine: diagrams`
# frontmatter is what makes selecting this agent switch the rendering engine.
DEFAULT_DIAGRAMS_AGENT_CONTENT = """\
---
engine: diagrams
---
You are a diagram generation assistant that writes Python using the
**diagrams** (mingrammer) library to draw infrastructure/architecture diagrams
with real provider icons.

Rules:
- Always output the complete program inside a fenced code block marked ```python
- Build exactly one diagram using a `with Diagram(...) as ...:` context. Do not
  call `.render()` yourself; do not set `show=`/`filename=`/`outformat=` — the
  app controls output (it renders a PNG).
- Group related nodes with `with Cluster("name"):`.
- Connect nodes with `>>`, `<<`, and `-` (e.g. `web >> db`,
  `lb >> [web1, web2]`).
- Import node classes from the right provider modules, e.g.
    from diagrams import Diagram, Cluster
    from diagrams.onprem.compute import Server
    from diagrams.onprem.database import PostgreSQL
    from diagrams.onprem.inmemory import Redis
    from diagrams.onprem.network import Internet
    from diagrams.generic.os import LinuxGeneral
    from diagrams.aws.compute import EC2
    from diagrams.aws.database import RDS
    from diagrams.k8s.compute import Pod
- Use only the `diagrams` library (and stdlib math/random if needed). Do NOT
  import os, sys, subprocess, socket, requests, or read/write files — such code
  is rejected by a safety check.
- Always output the full program, not just the changed part. If asked to modify
  an existing diagram, base it on the current code shown in the conversation.

Template to follow:

```python
from diagrams import Diagram, Cluster
from diagrams.onprem.compute import Server
from diagrams.onprem.database import PostgreSQL
from diagrams.onprem.network import Internet

with Diagram("Company LAN"):
    net = Internet("internet")
    with Cluster("Company LAN"):
        app = Server("App Server")
        db = PostgreSQL("Vector DB")
        app >> db
    app >> net
```
"""

DEFAULT_EXAMPLES_YAML = """\
examples:
  - name: "Simple two-node flow"
    engine: d2
    description: "Minimal example with two nodes and an animated connection"
    d2: |
      a: "Node A"
      b: "Node B"
      a -> b: "data" {
        style.animated: true
      }

  - name: "LAN with two zones"
    engine: d2
    description: "Two LAN containers with nodes and bidirectional flows"
    d2: |
      direction: down
      lanA: LAN A {
        server: "Server\\n192.168.1.10"
      }
      lanB: LAN B {
        client: "Client\\n192.168.2.20"
      }
      lanA.server <-> lanB.client: "TCP" {
        style.animated: true
      }

  - name: "Diagrams: web app on-prem"
    engine: diagrams
    description: "mingrammer Diagrams — load balancer, web tier, and a database"
    code: |
      from diagrams import Diagram, Cluster
      from diagrams.onprem.network import Nginx
      from diagrams.onprem.compute import Server
      from diagrams.onprem.database import PostgreSQL

      with Diagram("Web Service"):
          lb = Nginx("load balancer")
          with Cluster("Web Tier"):
              web = [Server("web1"), Server("web2"), Server("web3")]
          db = PostgreSQL("db")
          lb >> web >> db

  - name: "Diagrams: RAG stack"
    engine: diagrams
    description: "mingrammer Diagrams — app server with vector DB, cache, and LLM"
    code: |
      from diagrams import Diagram, Cluster
      from diagrams.onprem.compute import Server
      from diagrams.onprem.database import PostgreSQL
      from diagrams.onprem.inmemory import Redis
      from diagrams.onprem.network import Internet

      with Diagram("RAG Stack"):
          net = Internet("internet")
          with Cluster("Company LAN"):
              app = Server("app server")
              vdb = PostgreSQL("vector db")
              cache = Redis("cache")
              llm = Server("LLM server")
              app >> vdb
              app >> cache
              app >> llm
          app >> net
"""

PLACEHOLDER_SVG = (
    "<div style='padding:2rem;text-align:center;color:#888;'>"
    "No diagram yet — describe one in the chat below, or load an example."
    "</div>"
)

# Injected into <head>: bounds the preview size, styles the zoom toolbar, and
# defines the global zoom handler (scripts inserted via innerHTML do NOT run,
# so the handler must live here, not inside the gr.HTML value).
HEAD_HTML = """
<style>
  /* The preview box is the Gradio HTML *component* (#svg-box). Because that
     element persists across diagram updates (only its inner HTML is swapped),
     a height the user drags with the resize handle sticks between renders. */
  #svg-box {
    height: 380px;           /* not !important, so a dragged inline height wins */
    min-height: 140px;
    overflow: auto !important;  /* override Gradio's .block overflow: visible */
    resize: vertical;
    border: 1px solid #ddd;
    border-radius: 8px;
    background: #fff;
    padding: 6px;
  }
  #d2-zoom { width: 100%; }
  #d2-zoom svg { width: 100%; height: auto; display: block; }
  .d2-toolbar button {
    cursor: pointer;
    padding: 2px 12px;
    margin-right: 6px;
    border: 1px solid #ccc;
    border-radius: 6px;
    background: #f6f6f6;
  }
  .d2-toolbar button:hover { background: #ececec; }
</style>
<script>
  window.d2Zoom = function (dir) {
    var box = document.getElementById('svg-box');
    var zm = document.getElementById('d2-zoom');
    if (!zm) return;
    if (dir === 0) {                         // fit to box width
      zm.style.width = '100%';
      return;
    }
    var cur = zm.getBoundingClientRect().width || (box ? box.clientWidth : 600);
    var factor = dir > 0 ? 1.25 : 0.8;       // zoom in / out
    zm.style.width = Math.max(80, Math.round(cur * factor)) + 'px';
  };
</script>
"""


# --------------------------------------------------------------------------- #
# Config helpers
# --------------------------------------------------------------------------- #
def load_config() -> dict:
    if not CONFIG_PATH.exists():
        save_config(DEFAULT_CONFIG)
        return dict(DEFAULT_CONFIG)
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}
    # merge missing keys with defaults so old configs keep working
    for section, defaults in DEFAULT_CONFIG.items():
        cfg.setdefault(section, {})
        for k, v in defaults.items():
            cfg[section].setdefault(k, v)
    return cfg


def save_config(cfg: dict) -> None:
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        yaml.safe_dump(cfg, f, sort_keys=False, allow_unicode=True)


def resolve_path(p: str) -> str:
    """Expand ~ and resolve relative paths against the project root."""
    p = os.path.expanduser(p)
    if not os.path.isabs(p):
        p = str(ROOT / p)
    return p


def resolve_binary(name_or_path: str) -> str | None:
    """Return an absolute path to an executable, resolving via PATH if needed."""
    expanded = os.path.expanduser(name_or_path)
    if os.path.isabs(expanded) and os.path.exists(expanded):
        return expanded
    found = shutil.which(name_or_path) or shutil.which(expanded)
    if found:
        return found
    # last resort: maybe it is a project-relative path
    candidate = ROOT / name_or_path
    return str(candidate) if candidate.exists() else None


def output_dir() -> Path:
    cfg = load_config()
    d = Path(resolve_path(cfg["app"]["output_dir"]))
    return d


def exports_dir() -> Path:
    return output_dir() / "exports"


# --------------------------------------------------------------------------- #
# Engines (D2 vs Diagrams) — selection & shared helpers
# --------------------------------------------------------------------------- #
def parse_agent_frontmatter(text: str):
    """Split an optional leading YAML `--- ... ---` block. Returns (meta, body)."""
    if text.startswith("---"):
        m = re.match(r"^---\s*\n(.*?)\n---\s*\n?(.*)$", text, re.DOTALL)
        if m:
            try:
                meta = yaml.safe_load(m.group(1)) or {}
            except Exception:
                meta = {}
            if isinstance(meta, dict):
                return meta, m.group(2)
    return {}, text


def agent_engine(name: str) -> str:
    """The engine an agent file targets, from its frontmatter (default d2)."""
    meta, _ = parse_agent_frontmatter(read_agent(name))
    e = str(meta.get("engine", "d2")).strip().lower()
    return e if e in ENGINES else "d2"


def resolve_engine() -> str:
    """Active engine: explicit override, else the active agent's engine."""
    cfg = load_config()
    override = cfg["app"].get("engine_override", "auto")
    if override in ENGINES:
        return override
    return agent_engine(cfg["app"].get("active_agent", "Draw-Agent.md"))


def engine_source_path(engine: str) -> Path:
    return output_dir() / ENGINES[engine]["source"]


def engine_render_path(engine: str) -> Path:
    return output_dir() / ENGINES[engine]["render"]


# ---- Diagrams (mingrammer) engine ---------------------------------------- #
def venv_python() -> str | None:
    for rel in ("Scripts/python.exe", "bin/python"):
        p = DIAGRAMS_VENV / rel
        if p.exists():
            return str(p)
    return None


def graphviz_bindir() -> str | None:
    d = shutil.which("dot")
    if d:
        return os.path.dirname(d)
    for c in (r"C:\Program Files\Graphviz\bin", r"C:\Program Files (x86)\Graphviz\bin"):
        if os.path.exists(os.path.join(c, "dot.exe")):
            return c
    return None


def check_diagrams() -> bool:
    """Health of the Diagrams engine: venv interpreter + Graphviz present."""
    return bool(venv_python()) and bool(graphviz_bindir())


# Static safety check for model-generated Python (defense in depth alongside the
# isolated venv subprocess). Import allowlist + denied builtins/dunder access.
_ALLOWED_IMPORT_ROOTS = {
    "diagrams", "math", "random", "itertools", "functools", "typing",
    "collections", "string", "textwrap", "dataclasses", "enum",
}
_DENIED_CALL_NAMES = {
    "eval", "exec", "compile", "__import__", "open", "input", "exit", "quit",
    "globals", "locals", "vars", "getattr", "setattr", "delattr", "memoryview",
    "breakpoint", "help",
}


def ast_safety_check(code: str) -> str | None:
    """Return an error string if the code uses something disallowed, else None."""
    try:
        tree = ast.parse(code)
    except SyntaxError as e:
        return f"Python syntax error: {e}"
    allowed = ", ".join(sorted(_ALLOWED_IMPORT_ROOTS))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                if a.name.split(".")[0] not in _ALLOWED_IMPORT_ROOTS:
                    return f"Import of '{a.name}' is not allowed. Allowed: {allowed}."
        elif isinstance(node, ast.ImportFrom):
            if (node.module or "").split(".")[0] not in _ALLOWED_IMPORT_ROOTS:
                return f"Import from '{node.module}' is not allowed. Allowed: {allowed}."
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            if node.func.id in _DENIED_CALL_NAMES:
                return f"Call to '{node.func.id}()' is not allowed."
        elif isinstance(node, ast.Attribute):
            if node.attr.startswith("__") and node.attr.endswith("__"):
                return f"Access to dunder attribute '{node.attr}' is not allowed."
        elif isinstance(node, ast.Name) and node.id == "__builtins__":
            return "Access to '__builtins__' is not allowed."
    return None


def _diagrams_env(gv_dir: str, tmp: str) -> dict:
    """A stripped environment for the subprocess: only what Windows + Graphviz
    need (no user PATH, proxies, tokens, etc.)."""
    sysroot = os.environ.get("SystemRoot") or os.environ.get("WINDIR") or r"C:\Windows"
    env = {
        "SystemRoot": sysroot,
        "SYSTEMROOT": sysroot,
        "PATH": gv_dir + os.pathsep + os.path.join(sysroot, "System32") + os.pathsep + sysroot,
        "TEMP": tmp, "TMP": tmp,
    }
    for k in ("SystemDrive", "NUMBER_OF_PROCESSORS", "PROCESSOR_ARCHITECTURE"):
        if k in os.environ:
            env[k] = os.environ[k]
    return env


def render_diagrams(source: str):
    """Run model-generated `diagrams` Python in the isolated venv and produce
    output/diagram.png. Writes the source to output/diagram.py. Returns
    (None, error)."""
    out = output_dir()
    out.mkdir(parents=True, exist_ok=True)
    (out / "diagram.py").write_text(source, encoding="utf-8")

    err = ast_safety_check(source)
    if err:
        return None, err
    py = venv_python()
    if not py:
        return None, "Diagrams environment not found (.venv-diagrams). See README."
    gv = graphviz_bindir()
    if not gv:
        return None, "Graphviz 'dot' not found. Install it (winget install Graphviz.Graphviz)."

    with tempfile.TemporaryDirectory(prefix="d2diag-") as td:
        user_code = Path(td) / "user_code.py"
        user_code.write_text(source, encoding="utf-8")
        try:
            r = subprocess.run(
                [py, "-I", str(DIAGRAMS_RUNNER), str(user_code), "diagram_out"],
                cwd=td, env=_diagrams_env(gv, td),
                capture_output=True, text=True, timeout=90,
            )
        except subprocess.TimeoutExpired:
            return None, "Diagrams render timed out (90s)."
        except Exception as e:
            return None, f"Diagrams execution failed: {e}"
        if r.returncode != 0:
            return None, (r.stderr.strip() or "Diagrams returned a non-zero exit code.")
        png = Path(td) / "diagram_out.png"
        if not png.exists():
            pngs = sorted(Path(td).glob("*.png"), key=lambda p: p.stat().st_mtime)
            if not pngs:
                return None, "No image produced. Did the code create a Diagram()?"
            png = pngs[-1]
        shutil.copyfile(png, out / "diagram.png")
    return None, None


# ---- Engine-dispatch wrappers used by the chat/UI ------------------------ #
def render_active(source: str):
    """Render `source` with the active engine. Returns error string or None."""
    engine = resolve_engine()
    if engine == "d2":
        _, err = render_d2(source)
        return err
    _, err = render_diagrams(source)
    return err


def extract_active(text: str):
    """Extract the diagram code block for the active engine from an LLM reply."""
    if resolve_engine() == "d2":
        return extract_d2_code(text)
    m = re.search(r"```(?:python|py)\s*(.*?)```", text, re.DOTALL)
    return m.group(1).strip() if m else None


def engine_health(engine: str) -> bool:
    return check_d2() if engine == "d2" else check_diagrams()


# --------------------------------------------------------------------------- #
# Prerequisite checks (run on startup)
# --------------------------------------------------------------------------- #
def ensure_prereqs() -> dict:
    """Create missing files/dirs and report what was found. Returns a status dict."""
    status = {}

    # config first (others read from it)
    if not CONFIG_PATH.exists():
        save_config(DEFAULT_CONFIG)
        print(f"[prereq] created {CONFIG_PATH.name} with defaults")
    cfg = load_config()

    # output dirs
    out = Path(resolve_path(cfg["app"]["output_dir"]))
    (out / "exports").mkdir(parents=True, exist_ok=True)
    print(f"[prereq] output dir: {out}")

    # agents dir
    AGENTS_DIR.mkdir(exist_ok=True)

    # default agents (D2 + Diagrams)
    if not DEFAULT_AGENT_PATH.exists():
        DEFAULT_AGENT_PATH.write_text(DEFAULT_AGENT_CONTENT, encoding="utf-8")
        print(f"[prereq] created {DEFAULT_AGENT_PATH.name}")
    if not DIAGRAMS_AGENT_PATH.exists():
        DIAGRAMS_AGENT_PATH.write_text(DEFAULT_DIAGRAMS_AGENT_CONTENT, encoding="utf-8")
        print(f"[prereq] created {DIAGRAMS_AGENT_PATH.name}")

    # examples
    if not EXAMPLES_PATH.exists():
        EXAMPLES_PATH.write_text(DEFAULT_EXAMPLES_YAML, encoding="utf-8")
        print(f"[prereq] created {EXAMPLES_PATH.name}")

    # d2 binary
    d2 = resolve_binary(cfg["app"]["d2_binary"])
    status["d2_path"] = d2
    if d2:
        print(f"[prereq] d2 binary: {d2}")
    else:
        print(f"[prereq] WARNING: D2 binary not found "
              f"(configured: {cfg['app']['d2_binary']}).")

    status["node"] = shutil.which("node")
    status["ffmpeg"] = shutil.which("ffmpeg")
    status["diagrams_venv"] = venv_python()
    status["graphviz"] = graphviz_bindir()
    print(f"[prereq] node:          {status['node'] or 'NOT FOUND'}")
    print(f"[prereq] ffmpeg:        {status['ffmpeg'] or 'NOT FOUND'}")
    print(f"[prereq] diagrams venv: {status['diagrams_venv'] or 'NOT FOUND'}")
    print(f"[prereq] graphviz dot:  {status['graphviz'] or 'NOT FOUND'}")
    print(f"[prereq] active engine: {resolve_engine()}")
    return status


# --------------------------------------------------------------------------- #
# Status checks (status bar)
# --------------------------------------------------------------------------- #
def check_d2() -> bool:
    cfg = load_config()
    d2 = resolve_binary(cfg["app"]["d2_binary"])
    if not d2:
        return False
    try:
        r = subprocess.run([d2, "--version"], capture_output=True, text=True, timeout=15)
        return r.returncode == 0
    except Exception:
        return False


def check_llm() -> bool:
    cfg = load_config()
    base = cfg["llm"]["base_url"].rstrip("/")
    try:
        r = requests.get(f"{base}/models", timeout=5)
        return r.status_code == 200
    except Exception:
        return False


def status_html() -> str:
    engine = resolve_engine()
    eng_ok = engine_health(engine)
    llm_ok = check_llm()

    def badge(label, ok):
        mark = "✅" if ok else "❌"
        color = "#1a7f37" if ok else "#cf222e"
        return (f"<span style='margin-left:1rem;color:{color};font-weight:600;'>"
                f"{label}: {mark}</span>")

    return ("<div style='text-align:right;padding:.3rem .6rem;'>"
            f"{badge('Engine: ' + ENGINES[engine]['label'], eng_ok)}"
            f"{badge('LLM', llm_ok)}"
            "</div>")


# --------------------------------------------------------------------------- #
# D2 rendering & extraction
# --------------------------------------------------------------------------- #
# Matches a line ending in a DOT/Mermaid-style attribute bracket, e.g.
#   `node [shape: package]`  or  `a -> b [style.animated: true]`
_BRACKET_ATTR_RE = re.compile(r"^(?P<prefix>.*?)\s*\[(?P<attrs>[^\[\]]*)\]\s*$")


def _has_toplevel_colon(s: str) -> bool:
    """True if s contains a ':' outside of any quoted string."""
    q = None
    for ch in s:
        if q:
            if ch == q:
                q = None
        elif ch in "\"'":
            q = ch
        elif ch == ":":
            return True
    return False


def _commas_to_semicolons(s: str) -> str:
    """Replace top-level commas with ';' (D2's map separator), keeping quotes."""
    out, q = [], None
    for ch in s:
        if q:
            out.append(ch)
            if ch == q:
                q = None
        elif ch in "\"'":
            q = ch
            out.append(ch)
        elif ch == ",":
            out.append(";")
        else:
            out.append(ch)
    return "".join(out)


def autofix_d2(source: str) -> str:
    """Best-effort repair of common non-D2 syntax so a slightly-misbehaving LLM
    still renders. Converts DOT/Mermaid attribute brackets to D2 brace blocks:
        `node [shape: package]`            -> `node: { shape: package }`
        `a -> b [style.animated: true]`    -> `a -> b: { style.animated: true }`
        `a -> b: "x" [style.animated: ..]` -> `a -> b: "x" { style.animated: .. }`
    Valid D2 (which never ends a line in `[...: ...]`) is left untouched.
    """
    fixed = []
    for line in source.splitlines():
        m = _BRACKET_ATTR_RE.match(line)
        # only treat as attributes if the bracket contains a key:value pair —
        # this avoids touching real D2 like `(a -> b)[0].style...`
        if m and ":" in m.group("attrs"):
            prefix = m.group("prefix").rstrip()
            attrs = _commas_to_semicolons(m.group("attrs").strip())
            sep = "" if _has_toplevel_colon(prefix) else ":"
            fixed.append(f"{prefix}{sep} {{ {attrs} }}")
        else:
            fixed.append(line)
    return "\n".join(fixed)


def render_d2(d2_source: str):
    """Write source to output/diagram.d2, render to SVG. Returns (svg_text, error)."""
    cfg = load_config()
    d2 = resolve_binary(cfg["app"]["d2_binary"])
    if not d2:
        return None, "D2 binary not found."
    out = Path(resolve_path(cfg["app"]["output_dir"]))
    out.mkdir(parents=True, exist_ok=True)
    input_file = out / "diagram.d2"
    output_file = out / "diagram.svg"
    d2_source = autofix_d2(d2_source)  # safety net for common non-D2 syntax
    input_file.write_text(d2_source, encoding="utf-8")
    try:
        result = subprocess.run(
            [d2, str(input_file), str(output_file)],
            capture_output=True, text=True, timeout=60,
        )
    except Exception as e:
        return None, f"D2 execution failed: {e}"
    if result.returncode != 0:
        return None, result.stderr.strip() or "D2 returned a non-zero exit code."
    return output_file.read_text(encoding="utf-8"), None


def extract_d2_code(text: str):
    match = re.search(r"```d2\s*(.*?)```", text, re.DOTALL)
    return match.group(1).strip() if match else None


def read_current_d2() -> str:
    """Current source code of the ACTIVE engine (diagram.d2 or diagram.py)."""
    f = engine_source_path(resolve_engine())
    return f.read_text(encoding="utf-8") if f.exists() else ""


def svg_inline_html() -> str:
    """Engine-aware preview wrapped in the zoomable inner layer (#d2-zoom).

    - D2: the SVG is embedded **inline** (NOT <img>) so animations play.
    - Diagrams: the PNG is embedded as a base64 data-URI <img> (self-contained;
      no animation). Zoom/resize still apply via the #svg-box / #d2-zoom layers.
    The scrollable/resizable box is the gr.HTML component itself (#svg-box).
    """
    engine = resolve_engine()
    f = engine_render_path(engine)
    if engine == "d2":
        inner = f.read_text(encoding="utf-8") if f.exists() else PLACEHOLDER_SVG
    else:
        if f.exists():
            b64 = base64.b64encode(f.read_bytes()).decode()
            inner = (f"<img src='data:image/png;base64,{b64}' "
                     "style='width:100%;display:block' alt='diagram'>")
        else:
            inner = PLACEHOLDER_SVG
    return f"<div id='d2-zoom'>{inner}</div>"


# --------------------------------------------------------------------------- #
# Agents
# --------------------------------------------------------------------------- #
def list_agent_files() -> list[str]:
    files = ["Draw-Agent.md", "Draw-Agent-Diagrams.md"]
    if AGENTS_DIR.exists():
        files += sorted(p.name for p in AGENTS_DIR.glob("*.md"))
    # de-dup while preserving order
    seen, out = set(), []
    for f in files:
        if f not in seen:
            seen.add(f)
            out.append(f)
    return out


def agent_path(name: str) -> Path:
    """Resolve an agent display name to a path (root agents live in ROOT)."""
    if name == "Draw-Agent.md":
        return DEFAULT_AGENT_PATH
    if name == "Draw-Agent-Diagrams.md":
        return DIAGRAMS_AGENT_PATH
    return AGENTS_DIR / name


def read_agent(name: str) -> str:
    p = agent_path(name)
    return p.read_text(encoding="utf-8") if p.exists() else ""


def load_active_agent() -> str:
    """Active agent's system prompt, with any `--- engine: ... ---` frontmatter
    stripped (the frontmatter selects the engine; it is not sent to the LLM)."""
    cfg = load_config()
    name = cfg["app"].get("active_agent", "Draw-Agent.md")
    content = read_agent(name) or DEFAULT_AGENT_CONTENT
    _, body = parse_agent_frontmatter(content)
    return body.strip() or content


# --------------------------------------------------------------------------- #
# Chat
# --------------------------------------------------------------------------- #
def chat_turn(user_message: str, history: list, current_code: str):
    """
    Both the prose prompt AND the current D2 code are sent to the LLM.

    - `current_code` is the (editable) D2 Code box content. We treat it as the
      authoritative current diagram: it is committed to output/diagram.d2 and
      re-rendered, so the user's manual edits take effect and the preview matches.
    - That current diagram is injected into the request as fresh context every
      turn (right before the user message). It is NOT stored in the visible chat
      history — so the transcript stays clean and the model always sees the real
      current diagram, even after an example load, a manual edit, or the auto-fix.

    history is a list of {"role","content"} dicts (gr.Chatbot, messages format).
    Returns (new_history, svg_html, d2_code, cleared_input).
    """
    history = history or []
    user_message = (user_message or "").strip()
    if not user_message:
        return history, gr.update(), gr.update(), ""

    engine = resolve_engine()
    lang = ENGINES[engine]["code_lang"]

    # 1. Commit the user's (possibly edited) code as the current diagram.
    current_code = (current_code or "").strip()
    render_error = None
    if current_code:
        render_error = render_active(current_code)  # writes source + render file
        current_code = read_current_d2()            # normalised current source

    history = history + [{"role": "user", "content": user_message}]

    cfg = load_config()
    try:
        client = OpenAI(
            base_url=cfg["llm"]["base_url"],
            api_key=cfg["llm"]["api_key"],
            timeout=float(cfg["llm"].get("timeout", LLM_TIMEOUT)),
        )
        # messages = system + prior turns + [current diagram context] + new prompt.
        # The context message is ephemeral (not added to the displayed history).
        messages = [{"role": "system", "content": load_active_agent()}]
        messages += history[:-1]  # prior turns (exclude the just-added user msg)
        if current_code:
            messages.append({
                "role": "system",
                "content": ("This is the diagram the user is currently looking at. "
                            "Treat it as the authoritative current state and base any "
                            "changes on it. Always output the full updated diagram.\n\n"
                            f"```{lang}\n{current_code}\n```"),
            })
        messages.append(history[-1])  # the new user message
        response = client.chat.completions.create(
            model=cfg["llm"]["model"],
            messages=messages,
        )
        assistant_text = response.choices[0].message.content or ""
    except Exception as e:
        err = f"⚠️ LLM request failed: {e}"
        history = history + [{"role": "assistant", "content": err}]
        return history, svg_inline_html(), read_current_d2(), ""

    history = history + [{"role": "assistant", "content": assistant_text}]

    new_code = extract_active(assistant_text)
    if new_code:
        error = render_active(new_code)
        if error:
            history = history + [
                {"role": "assistant", "content": f"⚠️ Render error:\n```\n{error}\n```"}
            ]
    elif render_error:
        # no new code from the model, but the user's edited code didn't render
        history = history + [
            {"role": "assistant",
             "content": f"⚠️ Your edited code has an error:\n```\n{render_error}\n```"}
        ]
    return history, svg_inline_html(), read_current_d2(), ""


def clear_chat():
    return [], svg_inline_html(), read_current_d2()


def clear_diagram():
    """Reset the active engine's diagram: empty its source file and remove its
    render so the preview shows the placeholder. Leaves the chat history intact.
    Returns (preview_html, empty_code).
    """
    engine = resolve_engine()
    src = engine_source_path(engine)
    rendered = engine_render_path(engine)
    src.write_text("", encoding="utf-8")
    if rendered.exists():
        rendered.unlink()
    return svg_inline_html(), ""


# --------------------------------------------------------------------------- #
# Exports
# --------------------------------------------------------------------------- #
def _download_copy(src: Path, suffix: str) -> str:
    """Copy a freshly-rendered file to a UNIQUELY-named download file.

    Exports otherwise reuse a constant filename (diagram.mp4/.gif/.svg), so the
    browser/Gradio serve a stale cached copy on repeat exports. A unique name per
    export changes the download URL and guarantees the latest file is delivered.
    Old downloads of the same type are removed to avoid clutter.
    """
    d = exports_dir()
    d.mkdir(parents=True, exist_ok=True)
    for old in d.glob(f"download-*{suffix}"):
        try:
            old.unlink()
        except OSError:
            pass
    dst = d / f"download-{uuid.uuid4().hex[:8]}{suffix}"
    shutil.copyfile(src, dst)
    return str(dst)


def export_image():
    """Download the active engine's rendered image (SVG for D2, PNG for Diagrams)."""
    engine = resolve_engine()
    f = engine_render_path(engine)
    if not f.exists():
        return gr.update(visible=False), "No diagram to export yet."
    dl = _download_copy(f, f.suffix)
    return gr.update(visible=True, value=dl), f"{ENGINES[engine]['render']} ready."


def _record_mp4():
    """Record the CURRENT diagram.svg to the canonical output/exports/diagram.mp4.
    Returns (mp4_path, None) or (None, error). Used by both MP4 and GIF export so
    the GIF always reflects the current diagram regardless of button order."""
    cfg = load_config()
    node = shutil.which("node")
    if not node:
        return None, "❌ node not found in PATH — cannot record MP4."
    svg = output_dir() / "diagram.svg"
    if not svg.exists():
        return None, "No SVG to export yet."
    script = resolve_path(cfg["app"]["recorder_script"])
    if not os.path.exists(script):
        return None, f"❌ recorder script not found: {script}"
    mp4 = exports_dir() / "diagram.mp4"
    exports_dir().mkdir(parents=True, exist_ok=True)
    exp = cfg.get("export", {})
    env = dict(os.environ)
    env.update({
        "D2REC_FPS": str(exp.get("fps", 20)),
        "D2REC_DURATION": str(exp.get("duration_sec", 6)),
        "D2REC_MAXWIDTH": str(exp.get("max_width", 900)),
        "D2REC_SPEED": str(exp.get("speed", 1.0)),
    })
    try:
        r = subprocess.run(
            [node, script, str(svg), str(mp4)],
            capture_output=True, text=True, timeout=300, env=env,
        )
    except Exception as e:
        return None, f"❌ MP4 export failed: {e}"
    if r.returncode != 0 or not mp4.exists():
        return None, f"❌ MP4 export failed:\n{r.stderr.strip()}"
    return mp4, None


def export_mp4():
    mp4, err = _record_mp4()
    if err:
        return gr.update(visible=False), err
    return gr.update(visible=True, value=_download_copy(mp4, ".mp4")), "MP4 ready."


def export_gif():
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        return gr.update(visible=False), "❌ ffmpeg not found in PATH — cannot make GIF."
    # Re-record the current diagram first so the GIF matches what's on screen,
    # even if the user never clicked Export MP4 for this diagram.
    mp4, err = _record_mp4()
    if err:
        return gr.update(visible=False), err
    palette = exports_dir() / "palette.png"
    gif = exports_dir() / "diagram.gif"
    exp = load_config().get("export", {})
    gfps = exp.get("gif_fps", 15)
    gscale = exp.get("gif_scale", 900)
    try:
        r1 = subprocess.run(
            [ffmpeg, "-y", "-i", str(mp4),
             "-vf", f"fps={gfps},scale={gscale}:-1:flags=lanczos,palettegen",
             str(palette)],
            capture_output=True, text=True, timeout=300,
        )
        if r1.returncode != 0:
            return gr.update(visible=False), f"❌ palettegen failed:\n{r1.stderr.strip()}"
        r2 = subprocess.run(
            [ffmpeg, "-y", "-i", str(mp4), "-i", str(palette),
             "-filter_complex",
             f"fps={gfps},scale={gscale}:-1:flags=lanczos[x];[x][1:v]paletteuse",
             str(gif)],
            capture_output=True, text=True, timeout=300,
        )
        if r2.returncode != 0 or not gif.exists():
            return gr.update(visible=False), f"❌ GIF assembly failed:\n{r2.stderr.strip()}"
    except Exception as e:
        return gr.update(visible=False), f"❌ GIF export failed: {e}"
    return gr.update(visible=True, value=_download_copy(gif, ".gif")), "GIF ready."


# --------------------------------------------------------------------------- #
# Examples
# --------------------------------------------------------------------------- #
def load_examples() -> list[dict]:
    if not EXAMPLES_PATH.exists():
        return []
    with open(EXAMPLES_PATH, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    return data.get("examples", [])


def example_names() -> list[str]:
    return [e.get("name", f"Example {i}") for i, e in enumerate(load_examples())]


def get_example(name: str) -> dict | None:
    for e in load_examples():
        if e.get("name") == name:
            return e
    return None


def example_code(e: dict) -> str:
    """An example's diagram source — `code` (any engine) or legacy `d2`."""
    return e.get("code") or e.get("d2", "")


def example_engine(e: dict) -> str:
    eng = str(e.get("engine", "d2")).strip().lower()
    return eng if eng in ENGINES else "d2"


def show_example(name: str):
    e = get_example(name)
    if not e:
        return "", ""
    return e.get("description", ""), example_code(e)


def use_example(name: str, history: list):
    """Load an example: switch to its engine, render it, jump to the Diagram tab.
    Outputs: chatbot, svg_view, code, tabs, engine_dd, mp4_btn, gif_btn, status, note.
    """
    e = get_example(name)
    if not e:
        return (history, *(gr.update(),) * 8)
    engine = example_engine(e)
    cfg = load_config()
    cfg["app"]["engine_override"] = engine
    save_config(cfg)
    err = render_active(example_code(e))
    history = (history or []) + [{
        "role": "assistant",
        "content": f"[Example loaded: {name}] — you can now describe your changes.",
    }]
    if err:
        history += [{"role": "assistant", "content": f"⚠️ Render error:\n```\n{err}\n```"}]
    animated = ENGINES[engine]["animated"]
    return (history, svg_inline_html(), read_current_d2(),
            gr.update(selected="diagram"), engine,
            gr.update(visible=animated), gr.update(visible=animated),
            status_html(), engine_note_text(engine))


# --------------------------------------------------------------------------- #
# LLM config tab actions
# --------------------------------------------------------------------------- #
def load_models(base_url: str):
    base = (base_url or "").rstrip("/")
    if not base:
        return gr.update(choices=[]), "Enter a Base URL first."
    try:
        r = requests.get(f"{base}/models", timeout=10)
        if r.status_code != 200:
            return gr.update(choices=[]), f"❌ /models returned HTTP {r.status_code}."
        data = r.json()
        ids = [m.get("id") for m in data.get("data", []) if m.get("id")]
        if not ids:
            return gr.update(choices=[]), "Endpoint reachable but returned no models."
        return gr.update(choices=ids), f"Loaded {len(ids)} model(s)."
    except Exception as e:
        return gr.update(choices=[]), f"❌ Could not reach endpoint: {e}"


def pick_model(selected: str):
    return selected or gr.update()


def save_llm_config(base_url: str, model: str, timeout):
    cfg = load_config()
    cfg["llm"]["base_url"] = (base_url or "").strip()
    cfg["llm"]["model"] = (model or "").strip()
    try:
        cfg["llm"]["timeout"] = max(1, int(float(timeout)))
    except (TypeError, ValueError):
        return "❌ Timeout must be a number (seconds)."
    save_config(cfg)
    return (f"✅ Saved. Base URL = {cfg['llm']['base_url']}, "
            f"model = {cfg['llm']['model']}, timeout = {cfg['llm']['timeout']}s")


# --------------------------------------------------------------------------- #
# Draw Agent tab actions
# --------------------------------------------------------------------------- #
def save_agent(filename: str, content: str):
    filename = (filename or "").strip()
    if not filename:
        return gr.update(), "Enter a filename (without .md)."
    filename = filename[:-3] if filename.endswith(".md") else filename
    if filename == "Draw-Agent":
        DEFAULT_AGENT_PATH.write_text(content or "", encoding="utf-8")
        saved_name = "Draw-Agent.md"
    elif filename == "Draw-Agent-Diagrams":
        DIAGRAMS_AGENT_PATH.write_text(content or "", encoding="utf-8")
        saved_name = "Draw-Agent-Diagrams.md"
    else:
        AGENTS_DIR.mkdir(exist_ok=True)
        (AGENTS_DIR / f"{filename}.md").write_text(content or "", encoding="utf-8")
        saved_name = f"{filename}.md"
    return gr.update(choices=list_agent_files(), value=saved_name), f"✅ Saved {saved_name}."


def on_agent_select(name: str):
    """Load an agent's content and show which engine it targets."""
    return read_agent(name), f"Targets engine: **{ENGINES[agent_engine(name)]['label']}**"


def set_active_agent(name: str):
    if not name:
        return "Select an agent file first.", status_html()
    cfg = load_config()
    cfg["app"]["active_agent"] = name
    save_config(cfg)
    extra = ""
    if cfg["app"].get("engine_override", "auto") == "auto":
        extra = f" Engine → {ENGINES[agent_engine(name)]['label']}."
    return f"✅ Active agent set to {name}.{extra}", status_html()


# --------------------------------------------------------------------------- #
# Export-settings tab actions
# --------------------------------------------------------------------------- #
def save_export_config(fps, duration_sec, max_width, speed, gif_fps, gif_scale):
    cfg = load_config()
    cfg.setdefault("export", {})
    try:
        cfg["export"]["fps"] = max(1, int(float(fps)))
        cfg["export"]["duration_sec"] = max(0.5, float(duration_sec))
        cfg["export"]["max_width"] = max(100, int(float(max_width)))
        cfg["export"]["speed"] = max(0.1, float(speed))
        cfg["export"]["gif_fps"] = max(1, int(float(gif_fps)))
        cfg["export"]["gif_scale"] = max(100, int(float(gif_scale)))
    except (TypeError, ValueError):
        return "❌ All fields must be numbers."
    save_config(cfg)
    e = cfg["export"]
    return (f"✅ Saved. Capture {e['fps']} fps × {e['duration_sec']}s, "
            f"width {e['max_width']}px, speed {e['speed']}× · "
            f"GIF {e['gif_fps']} fps @ {e['gif_scale']}px.")


# --------------------------------------------------------------------------- #
# Diagram-tab engine selector
# --------------------------------------------------------------------------- #
ENGINE_CHOICES = [
    ("Auto (from agent)", "auto"),
    ("D2 — animated SVG", "d2"),
    ("Diagrams — static PNG", "diagrams"),
]


def engine_note_text(engine: str) -> str:
    if ENGINES[engine]["animated"]:
        return ""
    return ("ℹ️ The **Diagrams** engine renders a *static* image (Python + "
            "Graphviz icons). MP4/GIF export is disabled for it.")


def on_engine_change(value: str):
    cfg = load_config()
    cfg["app"]["engine_override"] = value if (value in ENGINES or value == "auto") else "auto"
    save_config(cfg)
    engine = resolve_engine()
    animated = ENGINES[engine]["animated"]
    return (svg_inline_html(), read_current_d2(),
            gr.update(visible=animated), gr.update(visible=animated),
            status_html(), engine_note_text(engine))


# --------------------------------------------------------------------------- #
# UI
# --------------------------------------------------------------------------- #
def build_ui(prereq: dict):
    cfg = load_config()
    active_engine = resolve_engine()
    animated0 = ENGINES[active_engine]["animated"]

    with gr.Blocks(title="D2 Diagram Generator", head=HEAD_HTML) as demo:
        # ---- Status bar (all tabs) ----
        with gr.Row():
            gr.Markdown("### D2 Diagram Generator")
            status = gr.HTML(status_html())
            refresh_btn = gr.Button("Refresh", scale=0)

        timer = gr.Timer(30)
        timer.tick(status_html, outputs=status)
        refresh_btn.click(status_html, outputs=status)

        with gr.Tabs() as tabs:
            # ============================ Tab 1 ============================ #
            with gr.Tab("Diagram", id="diagram"):
                with gr.Row():
                    engine_dd = gr.Dropdown(
                        choices=ENGINE_CHOICES,
                        value=cfg["app"].get("engine_override", "auto"),
                        label="Diagram engine", scale=2,
                    )
                    zoom_out_btn = gr.Button("➖ Zoom out", scale=0)
                    zoom_fit_btn = gr.Button("⤢ Fit", scale=0)
                    zoom_in_btn = gr.Button("➕ Zoom in", scale=0)
                engine_note = gr.Markdown(engine_note_text(active_engine))
                svg_view = gr.HTML(svg_inline_html(), elem_id="svg-box")
                gr.Markdown(
                    "<span style='color:#888;font-size:.85em'>Tip: drag the "
                    "bottom-right corner of the preview to resize it.</span>"
                )
                zoom_out_btn.click(None, js="() => window.d2Zoom(-1)")
                zoom_fit_btn.click(None, js="() => window.d2Zoom(0)")
                zoom_in_btn.click(None, js="() => window.d2Zoom(1)")
                chatbot = gr.Chatbot(height=320, label="Chat")
                with gr.Row():
                    user_in = gr.Textbox(
                        placeholder="Describe the diagram you want…",
                        scale=8, show_label=False, lines=3,
                    )
                    send_btn = gr.Button("Send", variant="primary", scale=1)
                with gr.Accordion("Diagram code — editable; your edits are sent "
                                  "with your next message", open=False):
                    d2_code_view = gr.Code(value=read_current_d2(), language=None,
                                           label=None, interactive=True)
                with gr.Row():
                    svg_btn = gr.Button("Export image")
                    mp4_btn = gr.Button("Export MP4", visible=animated0)
                    gif_btn = gr.Button("Export GIF", visible=animated0)
                export_msg = gr.Markdown("")
                download = gr.File(label="Download", visible=False)
                with gr.Row():
                    clear_btn = gr.Button("Clear Chat")
                    clear_diagram_btn = gr.Button("Clear Diagram")

                send_btn.click(
                    chat_turn, [user_in, chatbot, d2_code_view],
                    [chatbot, svg_view, d2_code_view, user_in],
                )
                user_in.submit(
                    chat_turn, [user_in, chatbot, d2_code_view],
                    [chatbot, svg_view, d2_code_view, user_in],
                )
                clear_btn.click(clear_chat, outputs=[chatbot, svg_view, d2_code_view])
                clear_diagram_btn.click(clear_diagram,
                                        outputs=[svg_view, d2_code_view])
                engine_dd.change(
                    on_engine_change, [engine_dd],
                    [svg_view, d2_code_view, mp4_btn, gif_btn, status, engine_note],
                )

                svg_btn.click(export_image, outputs=[download, export_msg])
                mp4_btn.click(export_mp4, outputs=[download, export_msg])
                gif_btn.click(export_gif, outputs=[download, export_msg])

            # ============================ Tab 2 ============================ #
            with gr.Tab("LLM Config", id="llm"):
                base_url_in = gr.Textbox(
                    value=cfg["llm"]["base_url"],
                    label="OpenAI-compatible API Base URL "
                          "(e.g. http://192.168.2.57:11434/v1)",
                )
                load_models_btn = gr.Button("Load Models")
                model_dd = gr.Dropdown(choices=[], label="Available models",
                                       interactive=True)
                model_in = gr.Textbox(
                    value=cfg["llm"]["model"],
                    label="Active model name (edit manually if the endpoint "
                          "does not provide a model list)",
                )
                timeout_in = gr.Number(
                    value=cfg["llm"].get("timeout", LLM_TIMEOUT), precision=0,
                    label="Request timeout (seconds) — increase for slow local "
                          "models / large prompts",
                )
                save_cfg_btn = gr.Button("Save Config", variant="primary")
                cfg_msg = gr.Markdown("")

                load_models_btn.click(load_models, [base_url_in], [model_dd, cfg_msg])
                model_dd.change(pick_model, [model_dd], [model_in])
                save_cfg_btn.click(save_llm_config,
                                   [base_url_in, model_in, timeout_in], [cfg_msg])

            # ============================ Tab 3 ============================ #
            with gr.Tab("Draw Agent", id="agent"):
                agent_dd = gr.Dropdown(
                    choices=list_agent_files(),
                    value=cfg["app"].get("active_agent", "Draw-Agent.md"),
                    label="Agent file",
                )
                _active = cfg["app"].get("active_agent", "Draw-Agent.md")
                agent_engine_md = gr.Markdown(
                    f"Targets engine: **{ENGINES[agent_engine(_active)]['label']}**"
                )
                agent_text = gr.Textbox(
                    value=read_agent(_active), lines=20, label="Agent content",
                )
                gr.Markdown(
                    "<span style='color:#888;font-size:.85em'>An agent's engine "
                    "is set by a `--- engine: d2|diagrams ---` header at the top "
                    "of the file. Setting an agent active also switches the engine "
                    "(unless you pinned one on the Diagram tab).</span>"
                )
                with gr.Row():
                    fname_in = gr.Textbox(
                        placeholder="Draw-Agent-compact",
                        label="Filename (without .md)", scale=4,
                    )
                    save_agent_btn = gr.Button("Save as *.md", scale=1)
                set_active_btn = gr.Button("Set as active", variant="primary")
                agent_msg = gr.Markdown("")

                agent_dd.change(on_agent_select, [agent_dd],
                                [agent_text, agent_engine_md])
                save_agent_btn.click(save_agent, [fname_in, agent_text],
                                     [agent_dd, agent_msg])
                set_active_btn.click(set_active_agent, [agent_dd],
                                     [agent_msg, status])

            # ============================ Tab 4 ============================ #
            with gr.Tab("Examples", id="examples"):
                names = example_names()
                ex_dd = gr.Dropdown(choices=names,
                                    value=names[0] if names else None,
                                    label="Example")
                ex_desc = gr.Textbox(label="Description", interactive=False)
                ex_code = gr.Code(label="Diagram code preview", language=None,
                                  interactive=False)
                use_btn = gr.Button("Use as starting point", variant="primary")

                ex_dd.change(show_example, [ex_dd], [ex_desc, ex_code])
                use_btn.click(
                    use_example, [ex_dd, chatbot],
                    [chatbot, svg_view, d2_code_view, tabs, engine_dd,
                     mp4_btn, gif_btn, status, engine_note],
                )

                # initialise preview for the first example
                if names:
                    demo.load(show_example, [ex_dd], [ex_desc, ex_code])

            # ============================ Tab 5 ============================ #
            with gr.Tab("Export Settings", id="export"):
                exp = cfg.get("export", {})
                gr.Markdown(
                    "Control how the animated SVG is turned into MP4 / GIF.\n\n"
                    "If the marching-ant (dotted) animation looks **too fast** "
                    "in the exported video, lower **Playback speed** (e.g. 0.5 = "
                    "half speed) or raise **Capture duration**. Speed `1.0` aims "
                    "to match the live SVG in the browser."
                )
                with gr.Row():
                    fps_in = gr.Number(value=exp.get("fps", 20), precision=0,
                                       label="Capture fps (frames/sec)")
                    dur_in = gr.Number(value=exp.get("duration_sec", 6.0),
                                       label="Capture duration (seconds)")
                with gr.Row():
                    width_in = gr.Number(value=exp.get("max_width", 900), precision=0,
                                         label="Render width cap (px)")
                    speed_in = gr.Number(value=exp.get("speed", 1.0),
                                         label="Playback speed (1.0 = live, <1 slower)")
                with gr.Row():
                    gfps_in = gr.Number(value=exp.get("gif_fps", 15), precision=0,
                                        label="GIF fps")
                    gscale_in = gr.Number(value=exp.get("gif_scale", 900), precision=0,
                                          label="GIF width (px)")
                save_export_btn = gr.Button("Save Export Settings", variant="primary")
                export_cfg_msg = gr.Markdown("")

                save_export_btn.click(
                    save_export_config,
                    [fps_in, dur_in, width_in, speed_in, gfps_in, gscale_in],
                    [export_cfg_msg],
                )

    return demo


def main():
    print("=" * 60)
    print("D2 Diagram Generator — starting up")
    print("=" * 60)
    prereq = ensure_prereqs()
    demo = build_ui(prereq)
    demo.launch(share=False, server_name="0.0.0.0")


if __name__ == "__main__":
    main()
