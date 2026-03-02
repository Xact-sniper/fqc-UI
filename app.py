import json
import os
import re
import sys
import time
from copy import deepcopy
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple
from uuid import uuid4

import dash_cytoscape as cyto
import pandas as pd
from dash import ALL, Dash, Input, Output, State, ctx, dcc, html, dash_table
from dash.exceptions import PreventUpdate
from dash_extensions import EventListener


def _configure_local_fqc_import_path() -> None:
    env_path = os.getenv("FQC_PATH")
    candidates = [
        Path(env_path) if env_path else None,
        Path(__file__).resolve().parent.parent / "fqc-lib",
        Path("/workspace/fqc-lib"),
    ]
    for base in [c for c in candidates if c]:
        src_path = base / "src"
        if (src_path / "fqc").exists() and str(src_path) not in sys.path:
            sys.path.insert(0, str(src_path))
            return


_configure_local_fqc_import_path()

from fqc import CraftingGraph, Edge, Item, Machine, MachineAnalysis, MachineType, Quality, RecipeName  # noqa: E402
from fqc.data import MACHINE_SUPPORTED_RECIPES  # noqa: E402


class NodeKind:
    MACHINE = "machine"
    IO = "io"


def default_state() -> Dict:
    return {
        "nodes": [],
        "edges": [],
        "machine_priority": [m.value for m in MachineType],
    }


RECIPE_OPTIONS = sorted([r.value for r in RecipeName])
ITEM_OPTIONS = sorted([i.value for i in Item])


def load_recipe_io_maps() -> Tuple[Dict[str, Set[str]], Dict[str, List[str]]]:
    path = Path(__file__).resolve().parent / "recipe_dump.lua"
    if not path.exists():
        return {}, {}
    text = path.read_text(encoding="utf-8")
    wrapped = "{" + text + "}"
    wrapped = re.sub(r",(\s*[}\]])", r"\1", wrapped)
    try:
        parsed = json.loads(wrapped)
    except Exception:
        return {}, {}
    recipes = parsed.get("recipe", {})
    ins: Dict[str, Set[str]] = {}
    outs: Dict[str, List[str]] = {}

    def _name(entry):
        if isinstance(entry, dict):
            return entry.get("name")
        if isinstance(entry, list) and entry and isinstance(entry[0], str):
            return entry[0]
        return None

    for rname, rdata in recipes.items():
        if not isinstance(rdata, dict):
            continue
        r_ins = {_name(x) for x in rdata.get("ingredients", [])}
        r_outs = [_name(x) for x in rdata.get("results", [])]
        r_ins = {x for x in r_ins if x}
        r_outs = [x for x in r_outs if x]
        if r_ins:
            ins[rname] = r_ins
        if r_outs:
            outs[rname] = r_outs
    return ins, outs


RECIPE_INPUTS, RECIPE_OUTPUTS = load_recipe_io_maps()


def choose_machine_for_recipe(recipe: str, priority: List[str]) -> str:
    for machine_type in priority:
        if RecipeName(recipe) in MACHINE_SUPPORTED_RECIPES[MachineType(machine_type)]:
            return machine_type
    return priority[0]


def node_by_id(state: Dict, node_id: str) -> Optional[Dict]:
    for node in state["nodes"]:
        if node["id"] == node_id:
            return node
    return None


def resolve_io_attachment(state: Dict, io_id: str) -> Optional[str]:
    io_node = node_by_id(state, io_id)
    if not io_node or io_node.get("kind") != NodeKind.IO:
        return None
    for edge in state["edges"]:
        src = node_by_id(state, edge["source"])
        tgt = node_by_id(state, edge["target"])
        if not src or not tgt:
            continue
        if io_node.get("io_mode") == "input" and edge["source"] == io_id and tgt["kind"] == NodeKind.MACHINE:
            return tgt["id"]
        if io_node.get("io_mode") in {"output", "throughput"} and edge["target"] == io_id and src["kind"] == NodeKind.MACHINE:
            return src["id"]
    return None


def make_cyto_elements(state: Dict, weights: Optional[Dict[str, float]] = None) -> List[Dict]:
    els: List[Dict] = []
    for node in state["nodes"]:
        color = "#2563eb" if node["kind"] == NodeKind.MACHINE else {"input": "#10b981", "output": "#f59e0b", "throughput": "#8b5cf6"}.get(node.get("io_mode"), "#16a34a")
        els.append({"data": {"id": node["id"], "label": node["label"]}, "position": {"x": node["x"], "y": node["y"]}, "style": {"background-color": color}})
    for edge in state["edges"]:
        eid = edge["id"]
        w = float((weights or {}).get(eid, max(float(edge.get("share", 0.1)), 0.05)))
        label_item = edge.get("item", "flow")
        els.append({"data": {"id": eid, "source": edge["source"], "target": edge["target"], "label": f"{label_item} ({w:.2f}/s)", "width": 2 + min(20.0, (w ** 0.5) * 3.0)}})
    return els


def build_fqc_graph(state: Dict) -> CraftingGraph:
    graph = CraftingGraph()
    machine_ids = set()
    for node in state["nodes"]:
        if node["kind"] != NodeKind.MACHINE:
            continue
        graph.add_machine(Machine(id=node["id"], machine_type=MachineType(node["machine_type"]), recipe=RecipeName(node["recipe"]), machine_quality=Quality(node.get("quality", "normal")), count=node.get("count"), modules=()))
        machine_ids.add(node["id"])
    for edge in state["edges"]:
        if edge["source"] in machine_ids and edge["target"] in machine_ids:
            q = Quality(edge.get("quality", Quality.NORMAL.value))
            graph.add_edge(Edge(source=edge["source"], target=edge["target"], item=Item(edge["item"]), share=float(edge.get("share", 1.0)), quality=q))
    for io in state["nodes"]:
        if io["kind"] != NodeKind.IO:
            continue
        mid = resolve_io_attachment(state, io["id"])
        if not mid:
            continue
        if io.get("io_mode") == "output":
            graph.add_target_output(mid, Item(io["item"]), Quality(io["quality"]), float(io["rate"]))
        elif io.get("io_mode") == "input":
            graph.add_external_input(mid, Item(io["item"]), Quality(io["quality"]), float(io["rate"]))
        elif io.get("io_mode") == "throughput":
            graph.add_throughput_limit(mid, Item(io["item"]), Quality(io["quality"]), float(io["rate"]))
    return graph


def _remaining_share(state: Dict, source_id: str, item: str) -> float:
    used = sum(float(e.get("share", 0.0)) for e in state["edges"] if e["source"] == source_id and e.get("item") == item)
    return max(0.0, 1.0 - used)


def _can_source(node: Dict) -> bool:
    # Input nodes only source flow; output nodes only sink flow.
    return node["kind"] == NodeKind.MACHINE or node.get("io_mode") != "output"


def _can_target(node: Dict) -> bool:
    return node["kind"] == NodeKind.MACHINE or node.get("io_mode") != "input"


def validate_connection(state: Dict, source: Dict, target: Dict) -> Tuple[Optional[str], Optional[float], str]:
    if not _can_source(source):
        return None, None, "Output I/O nodes cannot have outgoing connections."
    if not _can_target(target):
        return None, None, "Input I/O nodes cannot have incoming connections."

    if source["kind"] == NodeKind.IO:
        item = source.get("item")
        if target["kind"] == NodeKind.MACHINE and item not in RECIPE_INPUTS.get(target.get("recipe"), set()):
            return None, None, f"{item} is not an input for {target.get('recipe')}."
        return item, 1.0, ""

    src_outputs = RECIPE_OUTPUTS.get(source.get("recipe"), [])
    if not src_outputs:
        return None, None, f"No outputs found for {source.get('recipe')}"

    if target["kind"] == NodeKind.IO:
        item = target.get("item")
        if item not in src_outputs:
            return None, None, f"{item} is not produced by {source.get('recipe')}."
        share = _remaining_share(state, source["id"], item)
        if share <= 0:
            return item, 0.0, ""
        return item, share, ""

    tgt_inputs = RECIPE_INPUTS.get(target.get("recipe"), set())
    for item in src_outputs:
        if item in tgt_inputs:
            share = _remaining_share(state, source["id"], item)
            if share > 0:
                return item, share, ""
            return item, 0.0, ""
    return None, None, "No valid item/share found for this machine connection."


def machine_label(machine_type: Optional[str]) -> str:
    if not machine_type:
        return "Machine"
    return machine_type.replace("-", " ").title()


app = Dash(__name__)
server = app.server

app.layout = html.Div([
    dcc.Store(id="state", data=default_state()),
    dcc.Store(id="weights", data={}),
    dcc.Store(id="analysis-data", data={}),
    dcc.Store(id="popup", data={"type": None, "open": False, "x": 20, "y": 20}),
    dcc.Store(id="selected-node", data=None),
    dcc.Store(id="selected-edge", data=None),
    dcc.Store(id="connect-source", data=None),
    dcc.Store(id="last-tap", data={"node": None, "ts": 0.0}),
    dcc.Store(id="last-context", data={"kind": None, "id": None, "ts": 0.0}),
    html.Div(style={"display": "flex", "height": "100vh", "fontFamily": "sans-serif"}, children=[
        html.Div(style={"width": "300px", "borderRight": "1px solid #ddd", "padding": "10px", "overflowY": "auto"}, children=[
            html.H4("Machine priority"),
            dash_table.DataTable(
                id="priority-table",
                columns=[{"name": "machine", "id": "machine"}],
                data=[],
                editable=True,
            ),
        ]),
        html.Div(style={"flex": "1", "position": "relative", "display": "flex", "flexDirection": "column"}, children=[
            html.Div(style={"display": "none"}, children=[
                html.Div(id="selected-node-label"),
                html.Div(id="edge-rate"),
                html.Div(id="hover-info"),
                html.Pre(id="status"),
                html.Button("Load demo graph", id="load-demo"),
                html.Button("Auto flow layout", id="flow-layout"),
                html.Button("Run analysis", id="run"),
                html.Button("Clear", id="clear"),
                dcc.Dropdown(id="priority-select"),
                html.Button("Move up", id="prio-up"),
                html.Button("Move down", id="prio-down"),
            ]),
            EventListener(
                id="canvas-events",
                events=[
                    {"event": "click", "props": ["shiftKey", "ctrlKey", "offsetX", "offsetY"]},
                    {"event": "contextmenu", "props": ["offsetX", "offsetY"]},
                ],
                children=cyto.Cytoscape(
                    id="graph",
                    elements=[],
                    layout={"name": "preset"},
                    style={"width": "100%", "height": "72vh"},
                    stylesheet=[
                        {"selector": "node", "style": {"label": "data(label)", "text-wrap": "wrap", "text-max-width": "130px", "font-size": 10}},
                        {"selector": "edge", "style": {"curve-style": "bezier", "target-arrow-shape": "triangle", "label": "data(label)", "width": "data(width)", "font-size": 9, "line-color": "#dc2626", "target-arrow-color": "#dc2626"}},
                    ],
                    userPanningEnabled=True,
                    userZoomingEnabled=True,
                    autoungrabify=False,
                ),
            ),
            html.Div(id="analysis", style={"padding": "8px", "overflowY": "auto"}),
            html.Div(id="recipe-popup", style={"position": "absolute", "display": "none", "background": "white", "border": "1px solid #ccc", "padding": "8px", "zIndex": 10}, children=[html.B("Add machine"), dcc.Dropdown(id="recipe-select", options=[{"label": r, "value": r} for r in RECIPE_OPTIONS], style={"width": "320px"}), dcc.Dropdown(id="recipe-quality", options=[{"label": q.value, "value": q.value} for q in Quality], value=Quality.NORMAL.value), html.Button("Create", id="create-recipe-node")]),
            html.Div(id="item-popup", style={"position": "absolute", "display": "none", "background": "white", "border": "1px solid #ccc", "padding": "8px", "zIndex": 10}, children=[html.B("Add I/O"), dcc.Dropdown(id="item-select", options=[{"label": i, "value": i} for i in ITEM_OPTIONS], value=Item.IRON_PLATE.value), dcc.Dropdown(id="io-mode", options=[{"label": "input", "value": "input"}, {"label": "output", "value": "output"}, {"label": "throughput", "value": "throughput"}], value="input"), dcc.Input(id="io-rate", type="number", value=1.0, style={"width": "100%"}), dcc.Dropdown(id="io-quality", options=[{"label": q.value, "value": q.value} for q in Quality], value=Quality.NORMAL.value), html.Button("Create", id="create-io-node")]),
            html.Div(id="info-popup", style={"position": "absolute", "display": "none", "background": "white", "border": "1px solid #94a3b8", "padding": "8px", "zIndex": 12, "maxWidth": "420px", "whiteSpace": "pre-wrap", "fontSize": "11px"}),

            html.Div(id="node-config-popup", style={"position": "absolute", "display": "none", "background": "white", "border": "1px solid #ccc", "padding": "8px", "zIndex": 11, "width": "360px"}, children=[
                html.B("Configure node"),
                dcc.Dropdown(id="node-machine-type", options=[{"label": m.value, "value": m.value} for m in MachineType], placeholder="Machine type"),
                dcc.Dropdown(id="node-recipe", options=[{"label": r, "value": r} for r in RECIPE_OPTIONS], placeholder="Recipe"),
                dcc.Dropdown(id="node-io-item", options=[{"label": i, "value": i} for i in ITEM_OPTIONS], placeholder="I/O item"),
                dcc.Dropdown(id="node-io-mode", options=[{"label": "input", "value": "input"}, {"label": "output", "value": "output"}, {"label": "throughput", "value": "throughput"}], placeholder="I/O mode"),
                dcc.Input(id="node-rate", type="number", step=0.1, placeholder="I/O rate", style={"width": "100%"}),
                dcc.Dropdown(id="node-quality", options=[{"label": q.value, "value": q.value} for q in Quality], placeholder="Quality"),
                dcc.Input(id="node-count", type="number", step=0.1, placeholder="Machine count", style={"width": "100%"}),
                html.Div("Outgoing edge shares"),
                html.Div(id="share-sliders"),
                html.Button("Save node", id="save-node", style={"width": "100%", "marginTop": "6px"}),
            ]),
        ]),
    ]),
])


@app.callback(Output("priority-table", "data"), Input("state", "data"))
def show_priority(state: Dict):
    return [{"machine": x} for x in state["machine_priority"]]


@app.callback(Output("state", "data", allow_duplicate=True), Input("priority-table", "data"), State("state", "data"), prevent_initial_call=True)
def update_priority_from_table(rows: List[Dict], state: Dict):
    st = deepcopy(state)
    if not rows:
        return st
    ordered = [r.get("machine") for r in rows if r.get("machine") in [m.value for m in MachineType]]
    if ordered:
        st["machine_priority"] = ordered
    return st


@app.callback(Output("state", "data", allow_duplicate=True), Input("prio-up", "n_clicks"), Input("prio-down", "n_clicks"), State("priority-select", "value"), State("state", "data"), prevent_initial_call=True)
def reorder_priority(_, __, selected: str, state: Dict):
    state = deepcopy(state)
    order = list(state["machine_priority"])
    if selected in order:
        i = order.index(selected)
        if ctx.triggered_id == "prio-up" and i > 0:
            order[i - 1], order[i] = order[i], order[i - 1]
        if ctx.triggered_id == "prio-down" and i < len(order) - 1:
            order[i + 1], order[i] = order[i], order[i + 1]
    state["machine_priority"] = order
    return state


@app.callback(Output("popup", "data"), Output("last-context", "data", allow_duplicate=True), Input("canvas-events", "event"), State("selected-node", "data"), State("selected-edge", "data"), State("state", "data"), State("last-context", "data"), prevent_initial_call=True)
def canvas_event(event: Dict, selected_node: Optional[str], selected_edge: Optional[str], state: Dict, last_context: Dict):
    if not event:
        raise PreventUpdate
    xy = {"x": int(event.get("offsetX", 20)), "y": int(event.get("offsetY", 20))}
    if event.get("event") == "contextmenu":
        raise PreventUpdate
    if event.get("shiftKey"):
        return {"type": "recipe", "open": True, "x": xy["x"], "y": xy["y"]}, last_context
    if event.get("ctrlKey"):
        return {"type": "io", "open": True, "x": xy["x"], "y": xy["y"]}, last_context
    raise PreventUpdate


@app.callback(Output("recipe-popup", "style"), Output("item-popup", "style"), Output("node-config-popup", "style"), Output("info-popup", "style"), Input("popup", "data"))
def popup_style(popup: Dict):
    base = {"position": "absolute", "left": f"{int(popup.get('x', 20))}px", "top": f"{int(popup.get('y', 20))}px", "background": "white", "border": "1px solid #ccc", "padding": "8px", "zIndex": 10}
    r = dict(base)
    i = dict(base)
    info = dict(base)
    r["display"] = "block" if popup.get("open") and popup.get("type") == "recipe" else "none"
    i["display"] = "block" if popup.get("open") and popup.get("type") == "io" else "none"
    n = dict(base)
    n["display"] = "block" if popup.get("open") and popup.get("type") == "node-config" else "none"
    info["display"] = "block" if popup.get("open") and popup.get("type") == "info" else "none"
    return r, i, n, info


@app.callback(Output("state", "data", allow_duplicate=True), Output("popup", "data", allow_duplicate=True), Output("status", "children", allow_duplicate=True), Input("create-recipe-node", "n_clicks"), State("recipe-select", "value"), State("recipe-quality", "value"), State("popup", "data"), State("state", "data"), prevent_initial_call=True)
def create_recipe(_, recipe: Optional[str], recipe_quality: Optional[str], popup: Dict, state: Dict):
    if not recipe:
        return state, popup, "Pick a recipe."
    st = deepcopy(state)
    nid = f"m-{uuid4().hex[:6]}"
    mt = choose_machine_for_recipe(recipe, st["machine_priority"])
    st["nodes"].append({"id": nid, "kind": NodeKind.MACHINE, "label": recipe, "recipe": recipe, "machine_type": mt, "quality": recipe_quality or Quality.NORMAL.value, "count": None, "x": popup["x"], "y": popup["y"]})
    return st, {"type": None, "open": False, "x": popup["x"], "y": popup["y"]}, f"Created machine {nid}."


@app.callback(Output("state", "data", allow_duplicate=True), Output("popup", "data", allow_duplicate=True), Output("status", "children", allow_duplicate=True), Input("create-io-node", "n_clicks"), State("item-select", "value"), State("io-mode", "value"), State("io-rate", "value"), State("io-quality", "value"), State("popup", "data"), State("state", "data"), prevent_initial_call=True)
def create_io(_, item: str, mode: str, rate: float, quality: str, popup: Dict, state: Dict):
    st = deepcopy(state)
    nid = f"io-{uuid4().hex[:6]}"
    st["nodes"].append({"id": nid, "kind": NodeKind.IO, "label": f"{mode}\n{item}", "item": item, "io_mode": mode, "rate": float(rate or 0), "quality": quality, "x": popup["x"], "y": popup["y"]})
    return st, {"type": None, "open": False, "x": popup["x"], "y": popup["y"]}, f"Created {mode} node {nid}."


@app.callback(Output("state", "data", allow_duplicate=True), Input("graph", "elements"), State("state", "data"), prevent_initial_call=True)
def sync_positions(elements: List[Dict], state: Dict):
    if not elements:
        raise PreventUpdate
    st = deepcopy(state)
    changed = False
    pos_by_id = {}
    for el in elements:
        data = el.get("data", {})
        pos = el.get("position")
        if data.get("id") and pos:
            pos_by_id[data["id"]] = pos
    for n in st["nodes"]:
        p = pos_by_id.get(n["id"])
        if p and (float(n.get("x", 0)) != float(p.get("x", 0)) or float(n.get("y", 0)) != float(p.get("y", 0))):
            n["x"] = float(p.get("x", n["x"]))
            n["y"] = float(p.get("y", n["y"]))
            changed = True
    if not changed:
        raise PreventUpdate
    return st


@app.callback(Output("state", "data", allow_duplicate=True), Output("connect-source", "data", allow_duplicate=True), Output("selected-node", "data", allow_duplicate=True), Output("selected-edge", "data", allow_duplicate=True), Output("last-tap", "data", allow_duplicate=True), Output("status", "children", allow_duplicate=True), Input("graph", "tapNodeData"), State("connect-source", "data"), State("last-tap", "data"), State("state", "data"), prevent_initial_call=True)
def select_or_connect(node_data: Dict, source: Optional[str], last_tap: Dict, state: Dict):
    if not node_data:
        raise PreventUpdate
    st = deepcopy(state)
    node_id = node_data["id"]
    now = time.time()
    is_double = (last_tap or {}).get("node") == node_id and now - float((last_tap or {}).get("ts", 0.0)) < 0.6

    if source is not None:
        src = node_by_id(st, source)
        tgt = node_by_id(st, node_id)
        if not src or not tgt:
            return st, None, node_id, None, {"node": node_id, "ts": now}, "Missing source/target"
        if any(e["source"] == source and e["target"] == node_id for e in st["edges"]):
            return st, None, node_id, None, {"node": node_id, "ts": now}, "Connection already exists."
        item, share, err = validate_connection(st, src, tgt)
        if err:
            return st, None, node_id, None, {"node": node_id, "ts": now}, err
        st["edges"].append({"id": f"e-{uuid4().hex[:6]}", "source": source, "target": node_id, "item": item, "share": share, "quality": Quality.NORMAL.value, "label": item})
        return st, None, node_id, None, {"node": node_id, "ts": now}, f"Connected {source} -> {node_id} ({item}, share={share:.2f})"

    if is_double:
        return st, node_id, node_id, None, {"node": node_id, "ts": now}, f"Connection mode started from {node_id}. Click target node."
    return st, None, node_id, None, {"node": node_id, "ts": now}, f"Selected node {node_id}. Double-click to start connection."


@app.callback(Output("popup", "data", allow_duplicate=True), Output("info-popup", "children"), Input("graph", "tapNodeData"), Input("graph", "tapEdgeData"), State("state", "data"), State("weights", "data"), State("analysis-data", "data"), prevent_initial_call=True)
def open_info_popup(node_data: Optional[Dict], edge_data: Optional[Dict], state: Dict, weights: Dict, analysis_data: Dict):
    if node_data and node_data.get("id"):
        node = node_by_id(state, node_data["id"])
        if not node:
            raise PreventUpdate
        nid = node["id"]
        node_meta = (analysis_data or {}).get("nodes", {}).get(nid, {})
        lines = [f"Node", f"kind: {node.get('kind')}"]
        if node.get("kind") == NodeKind.MACHINE:
            lines += [f"machine: {machine_label(node.get('machine_type'))}", f"recipe: {node.get('recipe')}"]
        else:
            lines += [f"item: {node.get('item')}", f"mode: {node.get('io_mode')}", f"rate: {float(node.get('rate', 0.0)):.3f}/s"]
        for k, v in sorted(node_meta.items()):
            lines.append(f"{k}: {v:.4f}")
        return {"type": "info", "open": True, "x": int(node.get("x", 20) + 20), "y": int(node.get("y", 20) + 20)}, "\n".join(lines)
    if edge_data and edge_data.get("id"):
        edge = next((e for e in state["edges"] if e["id"] == edge_data["id"]), None)
        if not edge:
            raise PreventUpdate
        rate = float((weights or {}).get(edge["id"], 0.0))
        lines = [f"Edge {edge['id']}", f"{edge.get('source')} -> {edge.get('target')}", f"item: {edge.get('item')}", f"share: {float(edge.get('share', 0.0)):.3f}", f"rate: {rate:.3f}/s"]
        src = node_by_id(state, edge.get("source"))
        return {"type": "info", "open": True, "x": int((src or {}).get("x", 20) + 80), "y": int((src or {}).get("y", 20) + 20)}, "\n".join(lines)
    raise PreventUpdate


@app.callback(
    Output("popup", "data", allow_duplicate=True),
    Output("state", "data", allow_duplicate=True),
    Output("selected-node", "data", allow_duplicate=True),
    Output("selected-edge", "data", allow_duplicate=True),
    Output("last-context", "data", allow_duplicate=True),
    Output("status", "children", allow_duplicate=True),
    Input("graph", "cxttapNodeData"),
    Input("graph", "cxttapEdgeData"),
    State("state", "data"),
    State("last-context", "data"),
    prevent_initial_call=True,
)
def right_click_config_or_delete(node_data: Optional[Dict], edge_data: Optional[Dict], state: Dict, last_context: Dict):
    st = deepcopy(state)
    now = time.time()
    if node_data and node_data.get("id"):
        node_id = node_data["id"]
        same = (last_context or {}).get("kind") == "node" and (last_context or {}).get("id") == node_id and (now - float((last_context or {}).get("ts", 0.0))) < 0.7
        if same:
            st["nodes"] = [n for n in st["nodes"] if n["id"] != node_id]
            st["edges"] = [e for e in st["edges"] if e["source"] != node_id and e["target"] != node_id]
            return {"type": None, "open": False, "x": 20, "y": 20}, st, None, None, {"kind": None, "id": None, "ts": 0.0}, f"Deleted node {node_id}."
        node = node_by_id(st, node_id) or {}
        return {"type": "node-config", "open": True, "x": int(node.get("x", 20) + 20), "y": int(node.get("y", 20) + 20)}, st, node_id, None, {"kind": "node", "id": node_id, "ts": now}, f"Configuring node {node_id}."
    if edge_data and edge_data.get("id"):
        edge_id = edge_data["id"]
        same = (last_context or {}).get("kind") == "edge" and (last_context or {}).get("id") == edge_id and (now - float((last_context or {}).get("ts", 0.0))) < 0.7
        if same:
            st["edges"] = [e for e in st["edges"] if e["id"] != edge_id]
            return {"type": None, "open": False, "x": 20, "y": 20}, st, None, None, {"kind": None, "id": None, "ts": 0.0}, f"Deleted edge {edge_id}."
        return {"type": None, "open": False, "x": 20, "y": 20}, st, None, edge_id, {"kind": "edge", "id": edge_id, "ts": now}, f"Right-click again quickly to delete edge {edge_id}."
    raise PreventUpdate


@app.callback(Output("selected-edge", "data"), Output("edge-rate", "children"), Input("graph", "tapEdgeData"), State("state", "data"), State("weights", "data"), prevent_initial_call=True)
def select_edge(edge_data: Dict, state: Dict, weights: Dict):
    if not edge_data:
        raise PreventUpdate
    edge = next((e for e in state["edges"] if e["id"] == edge_data["id"]), None)
    if not edge:
        raise PreventUpdate
    rate = float((weights or {}).get(edge["id"], 0.0))
    return edge["id"], f"{edge.get('item')} • quality {edge.get('quality', Quality.NORMAL.value)} • calculated rate: {rate:.3f}/s"


@app.callback(Output("selected-node-label", "children"), Input("selected-node", "data"), State("state", "data"))
def show_node(selected_node: Optional[str], state: Dict):
    if not selected_node:
        return "No node selected"
    node = node_by_id(state, selected_node)
    if not node:
        return "No node selected"
    if node["kind"] == NodeKind.MACHINE:
        return f"Machine: {node['id']}"
    return f"I/O: {node['id']}"


@app.callback(Output("state", "data", allow_duplicate=True), Output("status", "children", allow_duplicate=True), Input("save-node", "n_clicks"), State("selected-node", "data"), State("node-machine-type", "value"), State("node-recipe", "value"), State("node-io-item", "value"), State("node-io-mode", "value"), State("node-rate", "value"), State("node-quality", "value"), State("node-count", "value"), State("state", "data"), prevent_initial_call=True)
def save_node(_, selected_node: Optional[str], machine_type: Optional[str], recipe: Optional[str], io_item: Optional[str], io_mode: Optional[str], rate: Optional[float], quality: Optional[str], count: Optional[float], state: Dict):
    st = deepcopy(state)
    node = node_by_id(st, selected_node) if selected_node else None
    if not node:
        return st, "Select node first."
    if node["kind"] == NodeKind.MACHINE:
        if not machine_type or not recipe:
            return st, "Machine needs machine type + recipe."
        node["machine_type"] = machine_type
        node["recipe"] = recipe
        node["quality"] = quality or "normal"
        node["count"] = float(count) if count not in (None, "") else None
        node["label"] = recipe
        return st, "Machine updated."
    if not io_item or not io_mode:
        return st, "I/O needs item + mode."
    node["item"] = io_item
    node["io_mode"] = io_mode
    node["rate"] = float(rate or 0)
    node["quality"] = quality or "normal"
    node["label"] = f"{io_mode}\n{io_item}"
    return st, "I/O updated."






@app.callback(
    Output("node-machine-type", "value"),
    Output("node-recipe", "value"),
    Output("node-io-item", "value"),
    Output("node-io-mode", "value"),
    Output("node-rate", "value"),
    Output("node-quality", "value"),
    Output("node-count", "value"),
    Output("share-sliders", "children"),
    Input("popup", "data"),
    State("selected-node", "data"),
    State("state", "data"),
)
def populate_node_popup(popup: Dict, selected_node: Optional[str], state: Dict):
    if not popup or popup.get("type") != "node-config" or not selected_node:
        return None, None, None, None, None, None, None, []
    node = node_by_id(state, selected_node)
    if not node:
        return None, None, None, None, None, None, None, []

    sliders = []
    outgoing = [e for e in state["edges"] if e["source"] == selected_node]
    for edge in outgoing:
        sliders.append(
            html.Div([
                html.Div(f"{edge.get('item')} -> {edge.get('target')}", style={"fontSize": "11px"}),
                dcc.Slider(id={"type": "share-slider", "edge_id": edge["id"]}, min=0.0, max=1.0, step=0.01, value=float(edge.get("share", 1.0))),
            ], style={"marginBottom": "8px"})
        )

    if node["kind"] == NodeKind.MACHINE:
        return node.get("machine_type"), node.get("recipe"), None, None, None, node.get("quality", "normal"), node.get("count"), sliders
    return None, None, node.get("item"), node.get("io_mode"), node.get("rate"), node.get("quality", "normal"), None, sliders


@app.callback(
    Output("state", "data", allow_duplicate=True),
    Input({"type": "share-slider", "edge_id": ALL}, "value"),
    State({"type": "share-slider", "edge_id": ALL}, "id"),
    State("selected-node", "data"),
    State("state", "data"),
    prevent_initial_call=True,
)
def update_shares(slider_values: List[float], slider_ids: List[Dict], selected_node: Optional[str], state: Dict):
    if not selected_node or not slider_ids:
        raise PreventUpdate
    st = deepcopy(state)
    value_by_edge = {sid["edge_id"]: float(val) for sid, val in zip(slider_ids, slider_values)}

    # Apply then renormalize shares per item group to sum <= 1.
    grouped: Dict[str, List[Dict]] = {}
    for edge in st["edges"]:
        if edge["source"] == selected_node and edge["id"] in value_by_edge:
            edge["share"] = value_by_edge[edge["id"]]
            grouped.setdefault(edge.get("item", ""), []).append(edge)

    for _, edges in grouped.items():
        total = sum(float(e.get("share", 0.0)) for e in edges)
        if total > 1.0 and total > 1e-9:
            for e in edges:
                e["share"] = float(e.get("share", 0.0)) / total

    return st

@app.callback(Output("state", "data", allow_duplicate=True), Output("status", "children", allow_duplicate=True), Input("load-demo", "n_clicks"), prevent_initial_call=True)
def load_demo(_):
    state = {
        "nodes": [
            {"id": "m1", "kind": NodeKind.MACHINE, "label": "iron-gear-wheel", "recipe": "iron-gear-wheel", "machine_type": MachineType.ASSEMBLING_MACHINE_1.value, "quality": "normal", "count": None, "x": 260, "y": 180},
            {"id": "m2", "kind": NodeKind.MACHINE, "label": "automation-science-pack", "recipe": "automation-science-pack", "machine_type": MachineType.ASSEMBLING_MACHINE_1.value, "quality": "normal", "count": None, "x": 620, "y": 180},
            {"id": "io1", "kind": NodeKind.IO, "label": "input\niron-plate", "item": Item.IRON_PLATE.value, "io_mode": "input", "rate": 30.0, "quality": Quality.NORMAL.value, "x": 80, "y": 180},
            {"id": "io2", "kind": NodeKind.IO, "label": "output\nautomation-science-pack", "item": Item.AUTOMATION_SCIENCE_PACK.value, "io_mode": "output", "rate": 1.0, "quality": Quality.NORMAL.value, "x": 950, "y": 180},
        ],
        "edges": [
            {"id": "e1", "source": "io1", "target": "m1", "item": Item.IRON_PLATE.value, "share": 1.0, "quality": Quality.NORMAL.value, "label": Item.IRON_PLATE.value},
            {"id": "e2", "source": "m1", "target": "m2", "item": Item.IRON_GEAR_WHEEL.value, "share": 1.0, "quality": Quality.NORMAL.value, "label": Item.IRON_GEAR_WHEEL.value},
            {"id": "e3", "source": "m2", "target": "io2", "item": Item.AUTOMATION_SCIENCE_PACK.value, "share": 1.0, "quality": Quality.NORMAL.value, "label": Item.AUTOMATION_SCIENCE_PACK.value},
        ],
        "machine_priority": [m.value for m in MachineType],
    }
    return state, "Loaded demo graph."


@app.callback(Output("state", "data", allow_duplicate=True), Output("connect-source", "data", allow_duplicate=True), Output("selected-node", "data", allow_duplicate=True), Output("selected-edge", "data", allow_duplicate=True), Output("status", "children", allow_duplicate=True), Input("clear", "n_clicks"), prevent_initial_call=True)
def clear_all(_):
    return default_state(), None, None, None, "Cleared"


@app.callback(Output("weights", "data"), Output("analysis-data", "data"), Output("analysis", "children"), Input("run", "n_clicks"), Input("state", "data"), prevent_initial_call=True)
def run_analysis(_, state: Dict):
    try:
        graph = build_fqc_graph(state)
        graph.backcalculate_requirements(mode="fractional")
        report = MachineAnalysis().analyze(graph)
        weights: Dict[str, float] = {}
        for e in state["edges"]:
            src = node_by_id(state, e["source"])
            tgt = node_by_id(state, e["target"])
            if src and src.get("kind") == NodeKind.IO:
                weights[e["id"]] = max(0.1, float(src.get("rate", 0.0)))
                continue
            if tgt and tgt.get("kind") == NodeKind.IO:
                weights[e["id"]] = max(0.1, float(tgt.get("rate", 0.0)))
                continue
            s = report.machines.get(e["source"])
            t = report.machines.get(e["target"])
            if s and t and e.get("item"):
                try:
                    it = Item(e["item"])
                    weights[e["id"]] = max(0.1, min(float(s.production_rates.get(it, 0.0)), float(t.consumption_rates.get(it, 0.0))))
                except Exception:
                    weights[e["id"]] = max(0.1, float(e.get("share", 1.0)))
            else:
                weights[e["id"]] = max(0.1, float(e.get("share", 1.0)))

        ext_rows = [{"item": i.value, "quality": q.value, "rate": r} for (i, q), r in graph.required_external_inputs.items()]
        ui = [
            html.H4("Graph-level demand/supply"),
            html.Div(f"Machines in graph: {len(graph.machines)}"),
            html.H5("Required external inputs"),
            dash_table.DataTable(data=pd.DataFrame(ext_rows).to_dict("records"), page_size=8),
        ]
        node_meta: Dict[str, Dict[str, float]] = {}
        for mid, m in report.machines.items():
            fields: Dict[str, float] = {}
            fields["required_machine_count"] = float(getattr(m, "count", 0.0) or 0.0)
            fields["required_run_rate"] = float(getattr(m, "required_run_rate", 0.0) or 0.0)
            for item, rate in getattr(m, "consumption_rates", {}).items():
                item_name = item[0].value if isinstance(item, tuple) else item.value
                fields[f"required_input_{item_name}"] = float(rate)
            for item, rate in getattr(m, "production_rates", {}).items():
                item_name = item[0].value if isinstance(item, tuple) else item.value
                fields[f"actual_output_{item_name}"] = float(rate)
            node_meta[mid] = fields
        edge_meta = {e["id"]: {"source": e["source"], "target": e["target"], "item": e.get("item"), "share": float(e.get("share", 0.0)), "rate": float(weights.get(e["id"], 0.0))} for e in state["edges"]}
        return weights, {"nodes": node_meta, "edges": edge_meta}, ui
    except Exception as exc:
        return {}, {}, [html.Pre(str(exc))]


@app.callback(Output("hover-info", "children"), Input("graph", "mouseoverNodeData"), Input("graph", "mouseoverEdgeData"), State("analysis-data", "data"), prevent_initial_call=True)
def show_hover(node_data: Optional[Dict], edge_data: Optional[Dict], analysis_data: Dict):
    trg = ctx.triggered_id
    if trg != "graph":
        raise PreventUpdate
    if node_data and node_data.get("id"):
        nid = node_data["id"]
        fields = (analysis_data or {}).get("nodes", {}).get(nid, {})
        if not fields:
            return f"Node {nid}\n(no required_/actual_ fields yet)"
        lines = [f"Node {nid}"] + [f"{k}: {v:.4f}" for k, v in sorted(fields.items())]
        return "\n".join(lines)
    if edge_data and edge_data.get("id"):
        eid = edge_data["id"]
        e = (analysis_data or {}).get("edges", {}).get(eid, {})
        if not e:
            return ""
        return f"Edge {eid}\n{e.get('source')} -> {e.get('target')}\nitem: {e.get('item')}\nshare: {e.get('share', 0.0):.3f}\nrate: {e.get('rate', 0.0):.3f}/s"
    return ""


def apply_flow_layout(state: Dict) -> Dict:
    st = deepcopy(state)
    inputs = [n for n in st["nodes"] if n["kind"] == NodeKind.IO and n.get("io_mode") == "input"]
    machines = [n for n in st["nodes"] if n["kind"] == NodeKind.MACHINE]
    outputs = [n for n in st["nodes"] if n["kind"] == NodeKind.IO and n.get("io_mode") != "input"]
    ordered = [(inputs, 100), (machines, 520), (outputs, 940)]
    for group, x in ordered:
        for idx, n in enumerate(group):
            n["x"] = x
            n["y"] = 120 + idx * 130
    return st


@app.callback(Output("state", "data", allow_duplicate=True), Output("status", "children", allow_duplicate=True), Input("flow-layout", "n_clicks"), State("state", "data"), prevent_initial_call=True)
def flow_layout(_, state: Dict):
    return apply_flow_layout(state), "Applied flow layout."


@app.callback(Output("graph", "elements"), Input("state", "data"), Input("weights", "data"))
def render(state: Dict, weights: Dict):
    return make_cyto_elements(state, weights)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "8050")), debug=False)
