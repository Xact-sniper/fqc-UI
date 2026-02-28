# fqc-UI

Streamlit UI for building `fqc` crafting graphs, running back-calculation, and inspecting machine/network analysis.

## Run locally

```bash
# Optional but recommended if you want the fqc source available locally:
git clone https://github.com/Xact-sniper/fqc.git ../fqc-lib

python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
streamlit run app.py
```

## Features

- Place machines in a 2D flow canvas and drag nodes to organize layout.
- Draw directed edges directly between machine nodes, then edit item/share/quality metadata per edge.
- Add machines with recipe, quality, count, and optional module configuration.
- Add target output demand, throughput limits, and external input rates.
- Run `backcalculate_requirements` in `fractional` or `integer` mode.
- Run `MachineAnalysis` and inspect utilization, per-machine consumption/production, and unconsumed output.
- Inspect full graph state as JSON for debugging and reproducibility.
