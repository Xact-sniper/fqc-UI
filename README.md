# fqc-UI

Streamlit UI for building `fqc` crafting graphs, running back-calculation, and inspecting machine/network analysis.

## Run locally

```bash
# Put the fqc repo somewhere local (private repo is fine). Examples:
git clone <your-private-fqc-url> ../fqc-lib
# or
# export FQC_PATH=/absolute/path/to/fqc

python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
streamlit run app.py
```

`app.py` auto-loads `fqc` from one of these local locations (no pip install for `fqc` required):
- `$FQC_PATH/src`
- `../fqc-lib/src` (relative to this repo)
- `/workspace/fqc-lib/src`

## Features

- Graph-first layout with the canvas as the main workspace.
- Sidebar-driven quick add, edge editing, and machine module slot editing.
- Machine module editing via clickable slots and module picker buttons.
- Draw directed edges directly between machine nodes, then edit item/share/quality metadata per edge.
- Optional icon rendering by resolving `item_dump.lua` icon paths (`__base__`/`__core__`) against a local Factorio install path.
- Run `backcalculate_requirements` in `fractional` or `integer` mode.
- Run `MachineAnalysis` and inspect utilization and required external inputs.
