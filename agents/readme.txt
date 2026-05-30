Custom agents go here
=====================

This folder is for YOUR OWN agent files (system prompts that tell the LLM how to
draw). Any *.md file you put here automatically shows up in the "Draw Agent" tab
dropdown, alongside the two built-in defaults.

The two default agents live in the project root, not in this folder:
  - Draw-Agent.md            -> the D2 engine (animated SVG)
  - Draw-Agent-Diagrams.md   -> the Diagrams engine (static PNG, real icons)
These ship with the app and are recreated automatically if deleted.

How to add your own:
  - Easiest: open the "Draw Agent" tab, edit the text, type a filename (without
    .md), and click "Save as *.md". It is saved into this folder.
  - Or drop a *.md file here directly, then restart the app (or reopen the tab).

Selecting which engine an agent uses:
  Put a small header at the very top of the file to choose the engine. If you
  omit it, the agent defaults to the D2 engine.

    ---
    engine: diagrams        # or: d2
    ---
    You are a diagram assistant that ...

  Everything after the closing --- is the system prompt sent to the model.

Tip: "Set as active" in the Draw Agent tab makes an agent the one used in the
chat. If the engine override on the Diagram tab is "Auto", the active agent's
engine is what gets used.
