import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional
from uuid import uuid4

import dash_cytoscape as cyto
import pandas as pd
from dash import Dash, Input, Output, State, ctx, dcc, html, dash_table
from dash_extensions import EventListener


def _configure_local_fqc_import_path() -> None:
    env_path = os.getenv("FQC_PATH")
    candidates = [
        Path(env_path) if env_path else None,
        Path(__file__).resolve().parent.parent / "fqc-lib",
        Path(__file__).resolve().parent.parent / "fqc",
        Path("/workspace/fqc-lib"),
    ]
    for base in [c for c in candidates if c]:
        src_path = base / "src"
        if (src_path / "fqc").exists():
            if str(src_path) not in sys.path:
                sys.path.insert(0, str(src_path))
            return


_configure_local_fqc_import_path()

from fqc import CraftingGraph, Edge, Item, Machine, MachineAnalysis, MachineType, Quality, RecipeName  # noqa: E402
from fqc.data import MACHINE_SUPPORTED_RECIPES  # noqa: E402


@dataclass
class NodeKind:
    MACHINE = "machine"
    IO = "io"


def default_state() -> Dict:
    return {
        "nodes": [],
        "edges": [],
        "targets": [],
        "externals": [],
        "throughputs": [],
        "machine_priority": [m.value for m in MachineType],
    }


def make_cyto_elements(state: Dict, weights: Optional[Dict[str, float]] = None) -> List[Dict]:
    els: List[Dict] = []
    for node in state["nodes"]:
        label = node["label"]
        color = "#2563eb" if node["kind"] == NodeKind.MACHINE else "#16a34a"
        shape = "round-rectangle" if node["kind"] == NodeKind.MACHINE else "ellipse"
        els.append(
            {
                "data": {"id": node["id"], "label": label, "kind": node["kind"]},
                "position": {"x": node["x"], "y": node["y"]},
                "classes": f"{shape}",
                "style": {"background-color": color},
            }
        )
    for edge in state["edges"]:
        edge_id = edge["id"]
        w = (weights or {}).get(edge_id, max(edge.get("share", 0.1), 0.05))
        els.append(
            {
                "data": {
                    "id": edge_id,
                    "source": edge["source"],
                    "target": edge["target"],
                    "label": edge["label"],
                    "weight": float(w),
                }
            }
        )
    return els


def build_fqc_graph(state: Dict) -> CraftingGraph:
    graph = CraftingGraph()
    machine_ids = set()
    for node in state["nodes"]:
        if node["kind"] != NodeKind.MACHINE:
            continue
        graph.add_machine(
            Machine(
                id=node["id"],
                machine_type=MachineType(node["machine_type"]),
                recipe=RecipeName(node["recipe"]),
                machine_quality=Quality(node.get("quality", "normal")),
                count=node.get("count"),
                modules=(),
            )
        )
        machine_ids.add(node["id"])

    for edge in state["edges"]:
        if edge["source"] in machine_ids and edge["target"] in machine_ids:
            q = None if edge.get("quality", "(any)") == "(any)" else Quality(edge["quality"])
            graph.add_edge(
                Edge(
                    source=edge["source"],
                    target=edge["target"],
                    item=Item(edge["item"]),
                    share=float(edge.get("share", 1.0)),
                    quality=q,
                )
            )

    for io in state["nodes"]:
        if io["kind"] != NodeKind.IO:
            continue
        if io.get("io_mode") == "output":
            graph.add_target_output(io["machine_id"], Item(io["item"]), Quality(io["quality"]), float(io["rate"]))
        elif io.get("io_mode") == "input":
            graph.add_external_input(io["machine_id"], Item(io["item"]), Quality(io["quality"]), float(io["rate"]))
        elif io.get("io_mode") == "throughput":
            graph.add_throughput_limit(io["machine_id"], Item(io["item"]), Quality(io["quality"]), float(io["rate"]))
    return graph


def choose_machine_for_recipe(recipe: str, priority: List[str]) -> str:
    for machine_type in priority:
        if RecipeName(recipe) in MACHINE_SUPPORTED_RECIPES[MachineType(machine_type)]:
            return machine_type
    return priority[0]


app = Dash(__name__)
server = app.server

app.layout = html.Div(
    style={"display": "flex", "height": "100vh", "fontFamily": "sans-serif"},
    children=[
        dcc.Store(id="state", data=default_state()),
        dcc.Store(id="weights", data={}),
        dcc.Store(id="last-click", data={"x": 80, "y": 80}),
        dcc.Store(id="connect-source", data=None),
        html.Div(
            style={"width": "280px", "borderRight": "1px solid #ddd", "padding": "10px", "overflowY": "auto"},
            children=[
                html.H4("Machine priority"),
                html.Div("Used when shift-click adding recipe with multiple valid machines."),
                dash_table.DataTable(id="priority-table", columns=[{"name": "machine", "id": "machine"}], data=[]),
                dcc.Dropdown(id="priority-select"),
                html.Button("Move up", id="prio-up"),
                html.Button("Move down", id="prio-down", style={"marginLeft": "8px"}),
                html.Hr(),
                html.Div("Interaction"),
                html.Ul([
                    html.Li("Shift + click canvas: add machine from recipe"),
                    html.Li("Ctrl + click canvas: add I/O/throughput node"),
                    html.Li("Click node A then node B: connect edge"),
                    html.Li("Drag node: move"),
                    html.Li("Wheel: zoom, Drag background: pan"),
                ]),
                html.Button("Run analysis", id="run", style={"width": "100%"}),
                html.Button("Clear", id="clear", style={"width": "100%", "marginTop": "6px"}),
                html.Pre(id="status", style={"fontSize": "12px", "whiteSpace": "pre-wrap"}),
            ],
        ),
        html.Div(
            style={"flex": "1", "position": "relative", "display": "flex", "flexDirection": "column"},
            children=[
                EventListener(
                    id="canvas-events",
                    events=[{"event": "click", "props": ["shiftKey", "ctrlKey", "offsetX", "offsetY"]}],
                    children=cyto.Cytoscape(
                        id="graph",
                        elements=[],
                        layout={"name": "preset"},
                        style={"width": "100%", "height": "72vh"},
                        stylesheet=[
                            {"selector": "node", "style": {"label": "data(label)", "text-wrap": "wrap", "text-max-width": "130px", "font-size": 10}},
                            {"selector": "edge", "style": {"curve-style": "bezier", "target-arrow-shape": "triangle", "label": "data(label)", "width": "mapData(weight,0,20,1,14)", "font-size": 9}},
                        ],
                        userPanningEnabled=True,
                        userZoomingEnabled=True,
                        autoungrabify=False,
                    ),
                ),
                html.Div(id="analysis", style={"padding": "8px", "overflowY": "auto"}),
                html.Div(
                    id="recipe-popup",
                    style={"position": "absolute", "right": "20px", "top": "20px", "padding": "10px", "background": "#fff", "border": "1px solid #ccc", "display": "none", "zIndex": 2},
                    children=[
                        html.B("Add machine recipe"),
                        dcc.Dropdown(id="recipe-select", options=[{"label": r.value, "value": r.value} for r in RecipeName], style={"width": "300px"}),
                        html.Button("Create", id="create-recipe-node"),
                    ],
                ),
                html.Div(
                    id="item-popup",
                    style={"position": "absolute", "right": "20px", "top": "130px", "padding": "10px", "background": "#fff", "border": "1px solid #ccc", "display": "none", "zIndex": 2},
                    children=[
                        html.B("Add IO node"),
                        dcc.Dropdown(id="item-select", options=[{"label": i.value, "value": i.value} for i in Item], value=Item.IRON_PLATE.value),
                        dcc.Dropdown(id="io-mode", options=[{"label": "input", "value": "input"}, {"label": "output", "value": "output"}, {"label": "throughput", "value": "throughput"}], value="input"),
                        dcc.Input(id="io-rate", type="number", value=1.0, style={"width": "100%"}),
                        dcc.Dropdown(id="io-quality", options=[{"label": q.value, "value": q.value} for q in Quality], value=Quality.NORMAL.value),
                        dcc.Dropdown(id="io-machine", placeholder="Attach machine id"),
                        html.Button("Create", id="create-io-node"),
                    ],
                ),
            ],
        ),
    ],
)


@app.callback(Output("priority-table", "data"), Output("priority-select", "options"), Output("priority-select", "value"), Input("state", "data"))
def show_priority(state: Dict):
    opts = [{"label": x, "value": x} for x in state["machine_priority"]]
    value = state["machine_priority"][0] if state["machine_priority"] else None
    return [{"machine": x} for x in state["machine_priority"]], opts, value


@app.callback(Output("state", "data", allow_duplicate=True), Input("prio-up", "n_clicks"), Input("prio-down", "n_clicks"), State("priority-select", "value"), State("state", "data"), prevent_initial_call=True)
def reorder_priority(_, __, selected: str, state: Dict):
    order = list(state["machine_priority"])
    if selected not in order:
        return state
    i = order.index(selected)
    if ctx.triggered_id == "prio-up" and i > 0:
        order[i - 1], order[i] = order[i], order[i - 1]
    if ctx.triggered_id == "prio-down" and i < len(order) - 1:
        order[i + 1], order[i] = order[i], order[i + 1]
    state["machine_priority"] = order
    return state


@app.callback(Output("io-machine", "options"), Input("state", "data"))
def io_machine_opts(state: Dict):
    return [{"label": n["id"], "value": n["id"]} for n in state["nodes"] if n["kind"] == NodeKind.MACHINE]


@app.callback(Output("last-click", "data"), Output("recipe-popup", "style"), Output("item-popup", "style"), Input("canvas-events", "event"), State("last-click", "data"), prevent_initial_call=True)
def canvas_click(event, last):
    if not event:
        return last, {"display": "none"}, {"display": "none"}
    click = {"x": float(event.get("offsetX", 80)), "y": float(event.get("offsetY", 80))}
    base = {"position": "absolute", "right": "20px", "padding": "10px", "background": "#fff", "border": "1px solid #ccc", "zIndex": 2}
    if event.get("shiftKey"):
        return click, {**base, "top": "20px", "display": "block"}, {**base, "top": "130px", "display": "none"}
    if event.get("ctrlKey"):
        return click, {**base, "top": "20px", "display": "none"}, {**base, "top": "130px", "display": "block"}
    return click, {**base, "top": "20px", "display": "none"}, {**base, "top": "130px", "display": "none"}


@app.callback(
    Output("state", "data", allow_duplicate=True),
    Output("status", "children"),
    Input("create-recipe-node", "n_clicks"),
    State("recipe-select", "value"),
    State("last-click", "data"),
    State("state", "data"),
    prevent_initial_call=True,
)
def create_recipe_node(_, recipe: str, click: Dict, state: Dict):
    if not recipe:
        return state, "Pick a recipe"
    mt = choose_machine_for_recipe(recipe, state["machine_priority"])
    nid = f"m-{uuid4().hex[:6]}"
    state["nodes"].append({"id": nid, "kind": NodeKind.MACHINE, "label": f"{nid}\n{recipe}", "recipe": recipe, "machine_type": mt, "quality": "normal", "count": None, "x": click["x"], "y": click["y"]})
    return state, f"Created machine {nid} ({mt})"


@app.callback(
    Output("state", "data", allow_duplicate=True),
    Output("status", "children", allow_duplicate=True),
    Input("create-io-node", "n_clicks"),
    State("item-select", "value"),
    State("io-mode", "value"),
    State("io-rate", "value"),
    State("io-quality", "value"),
    State("io-machine", "value"),
    State("last-click", "data"),
    State("state", "data"),
    prevent_initial_call=True,
)
def create_io(_, item, mode, rate, quality, machine_id, click, state):
    if not machine_id:
        return state, "Attach IO node to machine"
    nid = f"io-{uuid4().hex[:6]}"
    state["nodes"].append({"id": nid, "kind": NodeKind.IO, "label": f"{mode}:{item}\n{rate}/s", "item": item, "io_mode": mode, "rate": float(rate or 0), "quality": quality, "machine_id": machine_id, "x": click["x"], "y": click["y"]})
    return state, f"Created {mode} node"


@app.callback(Output("state", "data", allow_duplicate=True), Output("connect-source", "data"), Output("status", "children", allow_duplicate=True), Input("graph", "tapNodeData"), State("connect-source", "data"), State("state", "data"), prevent_initial_call=True)
def connect_nodes(node_data, src, state):
    if not node_data:
        return state, src, ""
    node_id = node_data["id"]
    if not src:
        return state, node_id, f"Source selected: {node_id}; click target to connect"
    if src == node_id:
        return state, None, "Canceled connect"
    edge = {"id": f"e-{uuid4().hex[:6]}", "source": src, "target": node_id, "item": Item.IRON_PLATE.value, "share": 1.0, "quality": "(any)", "label": "flow"}
    state["edges"].append(edge)
    return state, None, f"Connected {src} -> {node_id}"


@app.callback(Output("state", "data", allow_duplicate=True), Output("status", "children", allow_duplicate=True), Input("clear", "n_clicks"), prevent_initial_call=True)
def clear_all(_):
    return default_state(), "Cleared"


@app.callback(Output("weights", "data"), Output("analysis", "children"), Output("status", "children", allow_duplicate=True), Input("run", "n_clicks"), State("state", "data"), prevent_initial_call=True)
def run_analysis(_, state: Dict):
    try:
        graph = build_fqc_graph(state)
        graph.backcalculate_requirements(mode="fractional")
        report = MachineAnalysis().analyze(graph)
        weights = {}
        for e in state["edges"]:
            s = report.machines.get(e["source"])
            t = report.machines.get(e["target"])
            if s and t:
                weights[e["id"]] = max(0.05, min(sum(s.production_rates.values()), sum(t.consumption_rates.values())))
            else:
                weights[e["id"]] = max(0.05, float(e.get("share", 1.0)))
        machine_rows = [{"machine_id": mid, "count": m.count, "run_rate": m.required_run_rate} for mid, m in graph.machines.items()]
        ext_rows = [{"item": i.value, "quality": q.value, "rate": r} for (i, q), r in graph.required_external_inputs.items()]
        ui = [
            html.H4("Machines"),
            dash_table.DataTable(data=pd.DataFrame(machine_rows).to_dict("records"), page_size=8),
            html.H4("Required external inputs"),
            dash_table.DataTable(data=pd.DataFrame(ext_rows).to_dict("records"), page_size=8),
        ]
        return weights, ui, "Analysis complete"
    except Exception as exc:
        return {}, [html.Pre(str(exc))], f"Analysis failed: {exc}"


@app.callback(Output("graph", "elements"), Input("state", "data"), Input("weights", "data"))
def render(state: Dict, weights: Dict):
    return make_cyto_elements(state, weights)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "8050")), debug=False)
