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
import dash_ag_grid as dag
import pandas as pd
from dash import ALL, Dash, Input, Output, State, ctx, dcc, html, dash_table
from dash.exceptions import PreventUpdate
from dash_extensions import EventListener


def _configure_local_fqc_import_path() -> None:
    env_path = os.getenv("FQC_PATH")
    candidates = [
        Path(env_path) if env_path else None,
        Path(__file__).resolve().parent.parent / "fqc",
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


def make_cyto_elements(state: Dict, weights: Optional[Dict[str, float]] = None, edge_metrics: Optional[Dict[str, Dict]] = None) -> List[Dict]:
    els: List[Dict] = []
    for node in state["nodes"]:
        color = "#2563eb" if node["kind"] == NodeKind.MACHINE else {"input": "#10b981", "output": "#f59e0b", "throughput": "#8b5cf6"}.get(node.get("io_mode"), "#16a34a")
        els.append({"data": {"id": node["id"], "label": node["label"]}, "position": {"x": node["x"], "y": node["y"]}, "style": {"background-color": color}})
    for edge in state["edges"]:
        eid = edge["id"]
        w = float((weights or {}).get(eid, max(float(edge.get("share", 0.1)), 0.05)))
        label_item = edge.get("item", "flow")
        meta = (edge_metrics or {}).get(eid, {})
        actual = meta.get("actual_rate")
        req = meta.get("required_rate")
        req_txt = "none" if req is None else f"{req:.2f}/s"
        act_txt = f"{(actual if actual is not None else w):.2f}/s"
        label = f"{label_item} | actual {act_txt} | required {req_txt}"
        els.append({"data": {"id": eid, "source": edge["source"], "target": edge["target"], "label": label, "width": 2 + min(20.0, (w ** 0.5) * 3.0)}})
    return els


def _lookup_rate(rate_map: Dict, item_name: str) -> float:
    for k, v in rate_map.items():
        if isinstance(k, tuple):
            if len(k) > 0 and getattr(k[0], "value", None) == item_name:
                return float(v)
        elif getattr(k, "value", None) == item_name:
            return float(v)
    return 0.0


def compute_edge_metrics(state: Dict, report: MachineAnalysis, has_demands: bool) -> Dict[str, Dict]:
    metrics: Dict[str, Dict] = {}
    for e in state["edges"]:
        src = node_by_id(state, e["source"])
        tgt = node_by_id(state, e["target"])
        sem = derive_edge_semantics(state, e) or {}
        item_name = sem.get("item")
        actual_rate = 0.0
        required_rate: Optional[float] = None
        if src and src.get("kind") == NodeKind.IO:
            actual_rate = float(src.get("rate", 0.0))
            required_rate = actual_rate if has_demands else None
        elif tgt and tgt.get("kind") == NodeKind.IO:
            actual_rate = float(tgt.get("rate", 0.0))
            required_rate = actual_rate if has_demands else None
        else:
            s = report.machines.get(e["source"])
            t = report.machines.get(e["target"])
            if s and item_name:
                actual_rate = _lookup_rate(getattr(s, "production_rates", {}), item_name)
            if has_demands and t and item_name:
                required_rate = _lookup_rate(getattr(t, "consumption_rates", {}), item_name)
        metrics[e["id"]] = {
            "source": e.get("source"),
            "target": e.get("target"),
            "item": item_name,
            "share": float(sem.get("share", 1.0)),
            "actual_rate": float(actual_rate),
            "required_rate": None if required_rate is None else float(required_rate),
        }
    return metrics


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
            sem = derive_edge_semantics(state, edge)
            if not sem or not sem.get("item"):
                continue
            q = Quality(sem.get("quality", Quality.NORMAL.value))
            graph.add_edge(Edge(source=edge["source"], target=edge["target"], item=Item(sem["item"]), share=float(sem.get("share", 1.0)), quality=q))
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


def _can_source(node: Dict) -> bool:
    # Input nodes only source flow; output nodes only sink flow.
    return node["kind"] == NodeKind.MACHINE or node.get("io_mode") != "output"


def _can_target(node: Dict) -> bool:
    return node["kind"] == NodeKind.MACHINE or node.get("io_mode") != "input"


def validate_connection(state: Dict, source: Dict, target: Dict) -> Tuple[bool, str]:
    if not _can_source(source):
        return False, "Output I/O nodes cannot have outgoing connections."
    if not _can_target(target):
        return False, "Input I/O nodes cannot have incoming connections."

    if source["kind"] == NodeKind.IO:
        item = source.get("item")
        if target["kind"] == NodeKind.MACHINE and item not in RECIPE_INPUTS.get(target.get("recipe"), set()):
            return False, f"{item} is not an input for {target.get('recipe')}."
        return True, ""

    src_outputs = RECIPE_OUTPUTS.get(source.get("recipe"), [])
    if not src_outputs:
        return False, f"No outputs found for {source.get('recipe')}"

    if target["kind"] == NodeKind.IO:
        item = target.get("item")
        if item not in src_outputs:
            return False, f"{item} is not produced by {source.get('recipe')}."
        return True, ""

    tgt_inputs = RECIPE_INPUTS.get(target.get("recipe"), set())
    for item in src_outputs:
        if item in tgt_inputs:
            return True, ""
    return False, "No valid source->target item match found."


def derive_edge_semantics(state: Dict, edge: Dict) -> Optional[Dict]:
    src = node_by_id(state, edge.get("source"))
    tgt = node_by_id(state, edge.get("target"))
    if not src or not tgt:
        return None
    if src.get("kind") == NodeKind.IO:
        return {"item": src.get("item"), "quality": src.get("quality", Quality.NORMAL.value), "share": 1.0}
    if tgt.get("kind") == NodeKind.IO:
        return {"item": tgt.get("item"), "quality": tgt.get("quality", Quality.NORMAL.value), "share": 1.0}
    outs = RECIPE_OUTPUTS.get(src.get("recipe"), [])
    ins = RECIPE_INPUTS.get(tgt.get("recipe"), set())
    for item in outs:
        if item in ins:
            return {"item": item, "quality": src.get("quality", Quality.NORMAL.value), "share": 1.0}
    return None


def machine_label(machine_type: Optional[str]) -> str:
    if not machine_type:
        return "Machine"
    return machine_type.replace("-", " ").title()


def _find_node_at(state: Dict, x: float, y: float, radius: float = 55.0) -> Optional[Dict]:
    best = None
    best_d2 = radius * radius
    for n in state.get("nodes", []):
        dx = float(n.get("x", 0.0)) - x
        dy = float(n.get("y", 0.0)) - y
        d2 = dx * dx + dy * dy
        if d2 <= best_d2:
            best = n
            best_d2 = d2
    return best


def _point_segment_distance(px: float, py: float, ax: float, ay: float, bx: float, by: float) -> float:
    abx = bx - ax
    aby = by - ay
    apx = px - ax
    apy = py - ay
    ab2 = abx * abx + aby * aby
    t = 0.0 if ab2 == 0 else max(0.0, min(1.0, (apx * abx + apy * aby) / ab2))
    cx = ax + t * abx
    cy = ay + t * aby
    dx = px - cx
    dy = py - cy
    return (dx * dx + dy * dy) ** 0.5


def _find_edge_at(state: Dict, x: float, y: float, threshold: float = 12.0) -> Optional[Dict]:
    for e in state.get("edges", []):
        s = node_by_id(state, e.get("source"))
        t = node_by_id(state, e.get("target"))
        if not s or not t:
            continue
        d = _point_segment_distance(x, y, float(s.get("x", 0.0)), float(s.get("y", 0.0)), float(t.get("x", 0.0)), float(t.get("y", 0.0)))
        if d <= threshold:
            return e
    return None


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
            dag.AgGrid(
                id="priority-table",
                columnDefs=[{"field": "machine", "rowDrag": True}],
                rowData=[],
                defaultColDef={"resizable": True},
                dashGridOptions={"animateRows": True, "rowDragManaged": True},
                style={"height": "260px"},
            ),
            html.Button("Load demo graph", id="load-demo", style={"width": "100%", "marginTop": "8px"}),
            html.Button("Auto flow layout", id="flow-layout", style={"width": "100%", "marginTop": "6px"}),
            html.Button("Clear", id="clear", style={"width": "100%", "marginTop": "6px"}),
            html.Pre(id="status", style={"fontSize": "12px", "whiteSpace": "pre-wrap", "marginTop": "8px"}),
        ]),
        html.Div(style={"flex": "1", "position": "relative", "display": "flex", "flexDirection": "column"}, children=[
            html.Div(style={"display": "none"}, children=[
                html.Div(id="selected-node-label"),
                html.Div(id="edge-rate"),
                html.Div(id="hover-info"),
                html.Button("Run analysis", id="run"),
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


@app.callback(Output("priority-table", "rowData"), Input("state", "data"))
def show_priority(state: Dict):
    return [{"machine": x} for x in state["machine_priority"]]


@app.callback(Output("state", "data", allow_duplicate=True), Input("priority-table", "rowData"), State("state", "data"), prevent_initial_call=True)
def update_priority_from_table(rows: List[Dict], state: Dict):
    st = deepcopy(state)
    if not rows:
        return st
    ordered = [r.get("machine") for r in rows if r.get("machine") in [m.value for m in MachineType]]
    if ordered:
        st["machine_priority"] = ordered
    return st


@app.callback(Output("popup", "data", allow_duplicate=True), Output("last-context", "data", allow_duplicate=True), Input("canvas-events", "event"), State("selected-node", "data"), State("selected-edge", "data"), State("state", "data"), State("last-context", "data"), prevent_initial_call=True)
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
        ok, err = validate_connection(st, src, tgt)
        if err:
            return st, None, node_id, None, {"node": node_id, "ts": now}, err
        if not ok:
            return st, None, node_id, None, {"node": node_id, "ts": now}, "Connection rejected."
        st["edges"].append({"id": f"e-{uuid4().hex[:6]}", "source": source, "target": node_id})
        return st, None, node_id, None, {"node": node_id, "ts": now}, f"Connected {source} -> {node_id}"

    if is_double:
        return st, node_id, node_id, None, {"node": node_id, "ts": now}, f"Connection mode started from {node_id}. Click target node."
    return st, None, node_id, None, {"node": node_id, "ts": now}, f"Selected node {node_id}. Double-click to start connection."


@app.callback(Output("popup", "data", allow_duplicate=True), Output("info-popup", "children"), Input("graph", "tapNodeData"), Input("graph", "tapEdgeData"), State("state", "data"), State("analysis-data", "data"), prevent_initial_call=True)
def open_info_popup(node_data: Optional[Dict], edge_data: Optional[Dict], state: Dict, analysis_data: Dict):
    if node_data and node_data.get("id"):
        node = node_by_id(state, node_data["id"])
        if not node:
            raise PreventUpdate
        nid = node["id"]
        node_meta = (analysis_data or {}).get("nodes", {}).get(nid, {})
        li = []
        if node.get("kind") == NodeKind.MACHINE:
            li += [html.Li(f"machine: {machine_label(node.get('machine_type'))}"), html.Li(f"recipe: {node.get('recipe')}"), html.Li(f"quality: {node.get('quality', Quality.NORMAL.value)}")]
        else:
            li += [html.Li(f"item: {node.get('item')}"), html.Li(f"mode: {node.get('io_mode')}"), html.Li(f"rate: {float(node.get('rate', 0.0)):.3f}/s"), html.Li(f"quality: {node.get('quality', Quality.NORMAL.value)}")]
        for k, v in sorted(node_meta.items()):
            li.append(html.Li(f"{k}: {v:.4f}"))
        content = html.Div([html.H5("Node details", style={"margin": "0 0 6px 0"}), html.Ul(li, style={"margin": 0, "paddingLeft": "18px"})])
        return {"type": "info", "open": True, "x": int(node.get("x", 20) + 20), "y": int(node.get("y", 20) + 20)}, content
    if edge_data and edge_data.get("id"):
        eid = edge_data["id"]
        edge = next((e for e in state["edges"] if e["id"] == eid), None)
        em = (analysis_data or {}).get("edges", {}).get(eid, {})
        if not edge:
            raise PreventUpdate
        req = em.get("required_rate")
        req_txt = "none" if req is None else f"{float(req):.3f}/s"
        li = [
            html.Li(f"source: {edge.get('source')}"),
            html.Li(f"target: {edge.get('target')}"),
            html.Li(f"item: {em.get('item')}"),
            html.Li(f"share: {float(em.get('share', 1.0)):.3f}"),
            html.Li(f"actual rate: {float(em.get('actual_rate', 0.0)):.3f}/s"),
            html.Li(f"required rate: {req_txt}"),
        ]
        src = node_by_id(state, edge.get("source"))
        content = html.Div([html.H5("Edge details", style={"margin": "0 0 6px 0"}), html.Ul(li, style={"margin": 0, "paddingLeft": "18px"})])
        return {"type": "info", "open": True, "x": int((src or {}).get("x", 20) + 80), "y": int((src or {}).get("y", 20) + 20)}, content
    raise PreventUpdate


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
    sem = derive_edge_semantics(state, edge) or {}
    rate = float((weights or {}).get(edge["id"], 0.0))
    return edge["id"], f"{sem.get('item')} • quality {sem.get('quality', Quality.NORMAL.value)} • calculated rate: {rate:.3f}/s"


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

    sliders = [html.Div("Edge attributes are derived from connected nodes.", style={"fontSize": "11px", "color": "#64748b"})]

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
    raise PreventUpdate

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
        has_demands = len(getattr(graph, "target_outputs", {})) > 0
        edge_meta = compute_edge_metrics(state, report, has_demands)
        weights: Dict[str, float] = {eid: max(0.1, float(meta.get("actual_rate", 0.0))) for eid, meta in edge_meta.items()}

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


@app.callback(Output("graph", "elements"), Input("state", "data"), Input("weights", "data"), Input("analysis-data", "data"))
def render(state: Dict, weights: Dict, analysis_data: Dict):
    return make_cyto_elements(state, weights, (analysis_data or {}).get("edges", {}))


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "8050")), debug=False)
