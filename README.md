# fqc-UI

Dash-based, graph-first UI for creating/inspecting `fqc` crafting graphs using an interaction model centered on the canvas.

## Run locally

```bash
# Put the fqc repo somewhere local (private repo is fine). Examples:
git clone <your-private-fqc-url> ../fqc-lib
# or
# export FQC_PATH=/absolute/path/to/fqc

python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python app.py
```

`app.py` auto-loads `fqc` from one of these local locations (no pip install for `fqc` required):
- `$FQC_PATH/src`
- `../fqc-lib/src` (relative to this repo)
- `/workspace/fqc-lib/src`

Open `http://localhost:8050`.

## Interaction model

- Pan/zoom/move directly in the graph canvas.
- **Shift+click canvas**: open recipe picker and create a machine node at click location.
- **Ctrl+click canvas**: open item picker and create input/output/throughput IO node at click location.
- **Click node A then node B**: connect nodes with a directed edge.
- Machine-type selection for recipe creation uses side-panel machine-priority ordering (first compatible machine wins).

## Abstraction model

The UI state intentionally separates visualization nodes/edges from `fqc` graph entities:
- Visual nodes can be machine or IO nodes.
- Only machine↔machine edges map to `fqc.Edge`.
- IO nodes map to target outputs, external inputs, or throughput limits when building the `CraftingGraph`.

This keeps the view model flexible while preserving deterministic compilation into `fqc` for calculations.
