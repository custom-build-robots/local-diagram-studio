# local-diagram-studio

A fully-local web app that turns a natural-language chat into **architecture diagrams**. A local LLM writes the diagram code, the app renders it live in the browser, and you export it as **SVG / PNG / MP4 / GIF**.

Two rendering engines, chosen by the selected agent (or pinned in the UI):

| Engine | Language | Output | Best for |
|--------|----------|--------|----------|
| **[D2](https://d2lang.com)** | D2 DSL | **animated** SVG (+ MP4/GIF) | flows, animated data movement |
| **[Diagrams](https://diagrams.mingrammer.com/)** (mingrammer) | Python | static PNG | cloud/infra diagrams with real provider icons (AWS/GCP/K8s/on-prem) |

Everything runs locally. The LLM is reached over any OpenAI-compatible endpoint
(e.g. [Ollama](https://ollama.com)) no cloud services, no external APIs.

## How it works

- You chat the LLM returns diagram code, which is rendered and shown live.
- The current diagram code is editable and is sent back to the model with every message, so refinements ("add a database", "make it bidirectional") build on the exact diagram on screen.
- The app keeps the full conversation as memory across turns.

## Requirements

| Component | Why | Linux | Windows |
|-----------|-----|-------|---------|
| Python 3.10+ | runs the app | distro package | python.org |
| [D2](https://d2lang.com) | D2 engine (SVG) | `curl -fsSL https://d2lang.com/install.sh \| sh -s --` | `winget install Terrastruct.D2` |
| [Graphviz](https://graphviz.org) | Diagrams engine (PNG) | `apt install graphviz` | `winget install Graphviz.Graphviz` |
| [ffmpeg](https://ffmpeg.org) | MP4/GIF export (D2) | `apt install ffmpeg` | `winget install Gyan.FFmpeg` |
| Node.js 18+ | SVG→MP4 recorder | distro / nodesource | `winget install OpenJS.NodeJS` |
| An OpenAI-compatible LLM | generates diagram code | e.g. Ollama | e.g. Ollama |

Only install Graphviz if you want the Diagrams engine; only install ffmpeg + Node if you want MP4/GIF export.

## Setup

```bash
# 1. Clone
git clone https://github.com/custom-build-robots/local-diagram-studio/ d2-app && cd d2-app

# 2. Main Python dependencies
python -m venv .venv
. .venv/bin/activate            # Windows: .venv\Scripts\activate
pip install -r requirements.txt

# 3. Recorder (D2 → MP4) — optional
cd recorder
npm install
npx playwright install chromium     # add `--with-deps` on a fresh Linux server
cd ..

# 4. Diagrams engine — optional, isolated environment
python -m venv .venv-diagrams
.venv-diagrams/bin/python -m pip install diagrams    # Windows: .venv-diagrams\Scripts\python
```

> **Why a second venv?** The Diagrams engine executes Python that the LLM
> generates. It runs in an isolated subprocess using `.venv-diagrams` (which
> contains only the `diagrams` library), with a stripped environment, a temp
> working directory, a timeout, and a static safety check that allows only the
> `diagrams` library. This is **not** a full OS sandbox — only use it with a
> trusted local model.

## Configure

Edit `config.yaml` (or use the in-app **LLM Config** tab):

```yaml
llm:
  base_url: "http://YOUR-OLLAMA-HOST:11434/v1"
  model: "your-model-name"
  api_key: "ollama"          # placeholder required by the OpenAI client
  timeout: 360               # seconds; raise for slow local models
app:
  engine_override: "auto"    # auto (from agent) | d2 | diagrams
```

## Run

```bash
python app.py
```

The app listens on `0.0.0.0:7860`. Open `http://SERVER-IP:7860`.

### Running as a service (Linux, systemd)

`/etc/systemd/system/d2-app.service`:

```ini
[Unit]
Description=D2 + Diagrams Generator
After=network.target

[Service]
User=www-data
WorkingDirectory=/opt/d2-app
ExecStart=/opt/d2-app/.venv/bin/python /opt/d2-app/app.py
Restart=on-failure
# ensure d2/dot/ffmpeg/node are on PATH for the service:
Environment=PATH=/usr/local/bin:/usr/bin:/bin

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now d2-app
```

## Usage

1. **(Optional) pick the engine** on the Diagram tab — *Auto (from agent)*,
   *D2 — animated*, or *Diagrams — static*.
2. **Describe a diagram** in the chat and press Send. It renders live; D2
   animates, Diagrams shows a static image.
3. **Refine it** by chatting, or expand **Diagram code** and edit it by hand —
   your edits are sent with your next message.
4. **Export**: *Export image* (SVG/PNG), and for D2 also *Export MP4* / *Export
   GIF*. Tune capture fps / duration / playback speed on the **Export Settings**
   tab (lower playback speed if the animation looks too fast).
5. **Agents** (Draw Agent tab) hold the system prompt and select the engine via a
   `--- engine: d2|diagrams ---` header. **Examples** tab has starting points for
   both engines.

## Security notes

- The app only makes outbound calls to the LLM endpoint **you** configure.
- The Diagrams engine runs model-generated Python (isolated as described above).
  For untrusted models, run the whole app in a container or restrict the host.
  If Docker is available, the Diagrams runner can be moved into a
  `docker run --network none` call — see `CLAUDE.md`.

## Troubleshooting

| Symptom | Fix |
|---------|-----|
| Status **Engine (D2) ❌** | Install D2 and ensure `d2` is on PATH (or set `app.d2_binary`). |
| Status **Engine (Diagrams) ❌** | Install Graphviz and create `.venv-diagrams`. |
| Status **LLM ❌** | Check Base URL / that the model server is reachable. |
| "LLM request timed out" | Raise **Request timeout** in LLM Config. |
| MP4/GIF export fails | Install Node + run the recorder setup; ensure ffmpeg is on PATH. |
| Diagrams: "Import of '…' not allowed" | The engine only permits the `diagrams` library. |

## Project layout

```
app.py                     main application (backend + Gradio UI)
diagrams_runner.py         isolated runner for the Diagrams engine
config.yaml                configuration
Draw-Agent.md              D2 system prompt
Draw-Agent-Diagrams.md     Diagrams system prompt
example.yaml               example diagrams (both engines)
recorder/record.js         SVG → MP4 recorder (Playwright)
agents/                    extra agent prompts
output/                    generated diagrams + exports (git-ignored)
```
