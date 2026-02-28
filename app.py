import json
from dataclasses import asdict
from uuid import uuid4

import pandas as pd
import streamlit as st
from fqc import (
    CraftingGraph,
    Edge,
    InstalledModule,
    Item,
    Machine,
    MachineAnalysis,
    MachineType,
    ModuleName,
    Quality,
    RecipeName,
)
from fqc.data import MACHINE_SUPPORTED_RECIPES
from streamlit_flow import StreamlitFlowEdge, StreamlitFlowNode, StreamlitFlowState, streamlit_flow

st.set_page_config(page_title="Factorio Quality Calculator UI", layout="wide")


def init_state() -> None:
    st.session_state.setdefault("machines", [])
    st.session_state.setdefault("edges", [])
    st.session_state.setdefault("targets", [])
    st.session_state.setdefault("throughputs", [])
    st.session_state.setdefault("external_inputs", [])


def machine_payload(machine: Machine, position: tuple[float, float]) -> dict:
    payload = asdict(machine)
    payload["machine_type"] = machine.machine_type.value
    payload["recipe"] = machine.recipe.value
    payload["machine_quality"] = machine.resolved_quality().value
    payload["position"] = {"x": float(position[0]), "y": float(position[1])}
    payload["modules"] = [
        {
            "module": m.module.value if isinstance(m.module, ModuleName) else str(m.module),
            "quality": m.resolved_quality().value,
            "count": m.count,
        }
        for m in machine.modules
    ]
    return payload


def edge_id(source: str, target: str) -> str:
    return f"edge-{source}-{target}-{uuid4().hex[:6]}"


def edge_label(edge: dict) -> str:
    quality_suffix = "" if edge["quality"] == "(any)" else f" [{edge['quality']}]"
    return f"{edge['item']} ({edge['share']}){quality_suffix}"


def flow_nodes() -> list[StreamlitFlowNode]:
    nodes: list[StreamlitFlowNode] = []
    for idx, machine in enumerate(st.session_state.machines):
        pos = machine.get("position", {"x": 120 * idx, "y": 100 * idx})
        nodes.append(
            StreamlitFlowNode(
                id=machine["id"],
                pos=(float(pos["x"]), float(pos["y"])),
                data={
                    "content": f"{machine['id']}\n{machine['machine_type']}\n{machine['recipe']}",
                },
                draggable=True,
                connectable=True,
                selectable=True,
            )
        )
    return nodes


def flow_edges() -> list[StreamlitFlowEdge]:
    rendered_edges: list[StreamlitFlowEdge] = []
    for edge in st.session_state.edges:
        rendered_edges.append(
            StreamlitFlowEdge(
                id=edge["id"],
                source=edge["source"],
                target=edge["target"],
                animated=True,
                marker_end={"type": "arrowclosed"},
                label=edge_label(edge),
            )
        )
    return rendered_edges


def sync_from_flow(flow_state: StreamlitFlowState | dict) -> None:
    state_dict = flow_state.asdict() if hasattr(flow_state, "asdict") else flow_state

    pos_by_machine = {
        node["id"]: node["position"]
        for node in state_dict.get("nodes", [])
        if node["id"] in {machine["id"] for machine in st.session_state.machines}
    }
    for machine in st.session_state.machines:
        if machine["id"] in pos_by_machine:
            machine["position"] = {
                "x": float(pos_by_machine[machine["id"]]["x"]),
                "y": float(pos_by_machine[machine["id"]]["y"]),
            }

    existing_pairs = {(edge["source"], edge["target"]): edge for edge in st.session_state.edges}
    flow_pairs = {(edge["source"], edge["target"]) for edge in state_dict.get("edges", [])}

    for source, target in flow_pairs:
        if (source, target) not in existing_pairs:
            st.session_state.edges.append(
                {
                    "id": edge_id(source, target),
                    "source": source,
                    "target": target,
                    "item": Item.IRON_PLATE.value,
                    "share": 1.0,
                    "quality": "(any)",
                }
            )

    st.session_state.edges = [
        edge
        for edge in st.session_state.edges
        if (edge["source"], edge["target"]) in flow_pairs
    ]


def build_graph() -> CraftingGraph:
    graph = CraftingGraph()

    for machine_data in st.session_state.machines:
        modules = tuple(
            InstalledModule(
                module=ModuleName(m["module"]),
                quality=Quality(m["quality"]),
                count=int(m["count"]),
            )
            for m in machine_data.get("modules", [])
        )
        graph.add_machine(
            Machine(
                id=machine_data["id"],
                machine_type=MachineType(machine_data["machine_type"]),
                recipe=RecipeName(machine_data["recipe"]),
                count=machine_data["count"],
                machine_quality=Quality(machine_data["machine_quality"]),
                modules=modules,
            )
        )

    for edge in st.session_state.edges:
        q = None if edge["quality"] == "(any)" else Quality(edge["quality"])
        graph.add_edge(
            Edge(
                source=edge["source"],
                target=edge["target"],
                item=Item(edge["item"]),
                share=float(edge["share"]),
                quality=q,
            )
        )

    for target in st.session_state.targets:
        graph.add_target_output(
            machine_id=target["machine_id"],
            item=Item(target["item"]),
            quality=Quality(target["quality"]),
            rate=float(target["rate"]),
        )

    for throughput in st.session_state.throughputs:
        graph.add_throughput_limit(
            machine_id=throughput["machine_id"],
            item=Item(throughput["item"]),
            quality=Quality(throughput["quality"]),
            rate=float(throughput["rate"]),
        )

    for ext in st.session_state.external_inputs:
        graph.add_external_input(
            machine_id=ext["machine_id"],
            item=Item(ext["item"]),
            quality=Quality(ext["quality"]),
            rate=float(ext["rate"]),
        )

    return graph


init_state()
st.title("Factorio Quality Calculator UI")
st.caption("Place machines in 2D, connect directed flows, then run fqc back-calculation and analysis.")

config_col, edge_col = st.columns([1, 1])
with config_col:
    st.subheader("Add machine node")
    with st.form("add_machine", clear_on_submit=True):
        machine_id = st.text_input("Machine ID", placeholder="e.g. quality-module-line-1")
        machine_type = st.selectbox("Machine type", [m.value for m in MachineType])
        supported = sorted(r.value for r in MACHINE_SUPPORTED_RECIPES[MachineType(machine_type)])
        recipe = st.selectbox("Recipe", supported)
        count_text = st.text_input("Count (blank = auto-populate during backcalc)", "")
        machine_quality = st.selectbox("Machine quality", [q.value for q in Quality])
        module_json = st.text_area(
            "Modules JSON (optional)",
            value="[]",
            help='Example: [{"module":"quality-module-3","quality":"normal","count":4}]',
        )
        pos_x = st.number_input("Initial X", value=float(120 * len(st.session_state.machines)))
        pos_y = st.number_input("Initial Y", value=float(80 * len(st.session_state.machines)))
        add_machine = st.form_submit_button("Add machine")

    if add_machine:
        try:
            parsed_modules = json.loads(module_json)
            module_objects = tuple(
                InstalledModule(
                    module=ModuleName(entry["module"]),
                    quality=Quality(entry.get("quality", "normal")),
                    count=int(entry.get("count", 1)),
                )
                for entry in parsed_modules
            )
            machine = Machine(
                id=machine_id or None,
                machine_type=MachineType(machine_type),
                recipe=RecipeName(recipe),
                count=None if count_text.strip() == "" else float(count_text),
                machine_quality=Quality(machine_quality),
                modules=module_objects,
            )
            if not machine.id:
                st.error("Please provide a machine ID so you can wire edges to it.")
            elif any(m["id"] == machine.id for m in st.session_state.machines):
                st.error(f"Machine '{machine.id}' already exists.")
            else:
                st.session_state.machines.append(machine_payload(machine, position=(pos_x, pos_y)))
                st.success(f"Added machine {machine.id}")
        except Exception as exc:
            st.error(f"Failed to add machine: {exc}")

with edge_col:
    st.subheader("Edge metadata")
    if st.session_state.edges:
        edge_map = {f"{e['source']} → {e['target']} ({e['id']})": e for e in st.session_state.edges}
        selected_label = st.selectbox("Select edge", list(edge_map.keys()))
        selected_edge = edge_map[selected_label]

        selected_edge["item"] = st.selectbox(
            "Item",
            [i.value for i in Item],
            index=[i.value for i in Item].index(selected_edge["item"]),
            key="edge_item_editor",
        )
        selected_edge["share"] = st.number_input(
            "Share", min_value=0.0, max_value=1.0, value=float(selected_edge["share"]), step=0.05
        )
        selected_edge["quality"] = st.selectbox(
            "Quality scope",
            ["(any)"] + [q.value for q in Quality],
            index=(["(any)"] + [q.value for q in Quality]).index(selected_edge["quality"]),
            key="edge_quality_editor",
        )

        if st.button("Delete selected edge"):
            st.session_state.edges = [e for e in st.session_state.edges if e["id"] != selected_edge["id"]]
            st.rerun()
    else:
        st.info("Create edges by dragging from a node handle to another node in the canvas below.")

st.divider()
planning_mode = st.radio("Backcalc mode", ["fractional", "integer"], horizontal=True)

st.subheader("Flow canvas")
st.caption("Drag nodes to place them. Draw directed edges directly on the canvas, then edit edge item/share in the panel above.")
canvas_state = StreamlitFlowState(nodes=flow_nodes(), edges=flow_edges())
flow_result = streamlit_flow(
    "crafting-flow",
    canvas_state,
    allow_new_edges=True,
    fit_view=True,
    show_controls=True,
    show_minimap=True,
    height=520,
)
sync_from_flow(flow_result)

st.subheader("Demand & limits")
machine_ids = [m["id"] for m in st.session_state.machines]
col_1, col_2, col_3 = st.columns(3)
with col_1:
    with st.form("add_target", clear_on_submit=True):
        t_mid = st.selectbox("Target machine", machine_ids if machine_ids else [""], key="target_mid")
        t_item = st.selectbox("Target output item", [i.value for i in Item], key="target_item")
        t_quality = st.selectbox("Target quality", [q.value for q in Quality], key="target_quality")
        t_rate = st.number_input("Target rate (items/s)", min_value=0.0, value=1.0, key="target_rate")
        add_target = st.form_submit_button("Add target output")
    if add_target and t_mid:
        st.session_state.targets.append(
            {"machine_id": t_mid, "item": t_item, "quality": t_quality, "rate": t_rate}
        )
with col_2:
    with st.form("add_limit", clear_on_submit=True):
        l_mid = st.selectbox("Limit machine", machine_ids if machine_ids else [""], key="limit_mid")
        l_item = st.selectbox("Limit item", [i.value for i in Item], key="limit_item")
        l_quality = st.selectbox("Limit quality", [q.value for q in Quality], key="limit_quality")
        l_rate = st.number_input("Throughput cap (items/s)", min_value=0.0, value=0.0, key="limit_rate")
        add_limit = st.form_submit_button("Add throughput limit")
    if add_limit and l_mid:
        st.session_state.throughputs.append(
            {"machine_id": l_mid, "item": l_item, "quality": l_quality, "rate": l_rate}
        )
with col_3:
    with st.form("add_external", clear_on_submit=True):
        e_mid = st.selectbox("External input machine", machine_ids if machine_ids else [""], key="ext_mid")
        e_item = st.selectbox("External input item", [i.value for i in Item], key="ext_item")
        e_quality = st.selectbox("External input quality", [q.value for q in Quality], key="ext_quality")
        e_rate = st.number_input("External input rate (items/s)", min_value=0.0, value=0.0, key="ext_rate")
        add_ext = st.form_submit_button("Add external input")
    if add_ext and e_mid:
        st.session_state.external_inputs.append(
            {"machine_id": e_mid, "item": e_item, "quality": e_quality, "rate": e_rate}
        )

clear_col, json_col = st.columns([1, 2])
with clear_col:
    if st.button("Clear graph"):
        for key in ("machines", "edges", "targets", "throughputs", "external_inputs"):
            st.session_state[key] = []
        st.rerun()
with json_col:
    st.json(
        {
            "machines": st.session_state.machines,
            "edges": st.session_state.edges,
            "targets": st.session_state.targets,
            "throughputs": st.session_state.throughputs,
            "external_inputs": st.session_state.external_inputs,
        }
    )

st.subheader("Run calculations")
if st.button("Run backcalculation + analysis", type="primary"):
    try:
        graph = build_graph()
        graph.backcalculate_requirements(mode=planning_mode)
        report = MachineAnalysis().analyze(graph)

        machine_rows = []
        for mid, machine in graph.machines.items():
            machine_rows.append(
                {
                    "machine_id": mid,
                    "machine_type": machine.machine_type.value,
                    "recipe": machine.recipe.value,
                    "count": machine.count,
                    "required_run_rate": machine.required_run_rate,
                }
            )
        st.markdown("#### Machine requirements")
        st.dataframe(pd.DataFrame(machine_rows), use_container_width=True)

        demand_rows = [
            {"item": item.value, "quality": quality.value, "required_external_input_rate": rate}
            for (item, quality), rate in graph.required_external_inputs.items()
        ]
        st.markdown("#### Required external inputs")
        st.dataframe(pd.DataFrame(demand_rows), use_container_width=True)

        report_rows = []
        for mid, m_report in report.machines.items():
            report_rows.append({"machine_id": mid, "utilization": m_report.utilization})
        st.markdown("#### Utilization")
        st.dataframe(pd.DataFrame(report_rows), use_container_width=True)

        detail_machine = st.selectbox(
            "Inspect machine streams",
            [m["id"] for m in st.session_state.machines],
            key="inspect_machine",
        )
        selected = report.machines[detail_machine]
        cons_rows = [
            {"item": item.value, "quality": quality.value, "rate": rate}
            for (item, quality), rate in selected.consumption_rates.items()
        ]
        prod_rows = [
            {"item": item.value, "quality": quality.value, "rate": rate}
            for (item, quality), rate in selected.production_rates.items()
        ]

        stream_left, stream_right = st.columns(2)
        with stream_left:
            st.markdown("#### Consumption rates")
            st.dataframe(pd.DataFrame(cons_rows), use_container_width=True)
        with stream_right:
            st.markdown("#### Production rates")
            st.dataframe(pd.DataFrame(prod_rows), use_container_width=True)

        leftovers = [
            {"item": item.value, "quality": quality.value, "rate": rate}
            for (item, quality), rate in report.unconsumed_output.items()
        ]
        st.markdown("#### Unconsumed output")
        st.dataframe(pd.DataFrame(leftovers), use_container_width=True)
    except Exception as exc:
        st.error(f"Calculation failed: {exc}")
