import json
import os
import re
import sys
from dataclasses import asdict
from pathlib import Path
from uuid import uuid4

import pandas as pd
import streamlit as st
from streamlit_flow import StreamlitFlowEdge, StreamlitFlowNode, StreamlitFlowState, streamlit_flow


def _configure_local_fqc_import_path() -> None:
    env_path = os.getenv("FQC_PATH")
    candidates = [
        Path(env_path) if env_path else None,
        Path(__file__).resolve().parent.parent / "fqc-lib",
        Path("/workspace/fqc-lib"),
    ]

    for base in [candidate for candidate in candidates if candidate]:
        src_path = base / "src"
        if (src_path / "fqc").exists():
            src_path_str = str(src_path)
            if src_path_str not in sys.path:
                sys.path.insert(0, src_path_str)
            return


_configure_local_fqc_import_path()

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

st.set_page_config(page_title="Factorio Quality Calculator UI", layout="wide")

MACHINE_TYPE_ICON = {
    "assembling-machine": "🏭",
    "electromagnetic": "🧲",
    "foundry": "🔥",
    "cryogenic": "❄️",
    "recycler": "♻️",
}
MODULE_ICON = {
    "quality-module": "✨",
    "productivity-module": "📈",
    "speed-module": "⚡",
    "efficiency-module": "🔋",
}
DEFAULT_SLOTS = {
    MachineType.ASSEMBLING_MACHINE_1.value: 0,
    MachineType.ASSEMBLING_MACHINE_2.value: 2,
    MachineType.ASSEMBLING_MACHINE_3.value: 4,
    MachineType.ELECTROMAGNETIC_PLANT.value: 5,
    MachineType.FOUNDRY.value: 4,
    MachineType.CRYOGENIC_PLANT.value: 8,
    MachineType.RECYCLER.value: 4,
}


@st.cache_data
def load_item_icons(item_dump_path: str) -> dict[str, str]:
    path = Path(item_dump_path)
    if not path.exists():
        return {}

    icon_map: dict[str, str] = {}
    current_name: str | None = None
    name_re = re.compile(r'"name":\s*"([^"]+)"')
    icon_re = re.compile(r'"icon":\s*"([^"]+)"')

    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        name_match = name_re.search(line)
        if name_match:
            current_name = name_match.group(1)

        icon_match = icon_re.search(line)
        if icon_match and current_name:
            icon_map[current_name] = icon_match.group(1)
            current_name = None

    return icon_map


def resolve_icon_path(icon_ref: str | None, factorio_install_path: str) -> str | None:
    if not icon_ref:
        return None

    factorio_root = Path(factorio_install_path).expanduser()
    if not factorio_root.exists():
        return None

    if icon_ref.startswith("__base__/"):
        candidate = factorio_root / "base" / icon_ref.replace("__base__/", "", 1)
    elif icon_ref.startswith("__core__/"):
        candidate = factorio_root / "core" / icon_ref.replace("__core__/", "", 1)
    else:
        candidate = Path(icon_ref)

    return str(candidate) if candidate.exists() else None


def module_pretty_name(module_value: str) -> str:
    prefix = next((k for k in MODULE_ICON if module_value.startswith(k)), "")
    return f"{MODULE_ICON.get(prefix, '🧩')} {module_value}"


def init_state() -> None:
    st.session_state.setdefault("machines", [])
    st.session_state.setdefault("edges", [])
    st.session_state.setdefault("targets", [])
    st.session_state.setdefault("throughputs", [])
    st.session_state.setdefault("external_inputs", [])
    st.session_state.setdefault("selected_machine_id", None)
    st.session_state.setdefault("module_slot_to_edit", None)


def machine_payload(machine: Machine, position: tuple[float, float], module_slots: int) -> dict:
    payload = asdict(machine)
    payload["machine_type"] = machine.machine_type.value
    payload["recipe"] = machine.recipe.value
    payload["machine_quality"] = machine.resolved_quality().value
    payload["position"] = {"x": float(position[0]), "y": float(position[1])}
    payload["module_slots"] = module_slots
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


def selected_flow_machine(flow_state: StreamlitFlowState | dict) -> str | None:
    state_dict = flow_state.asdict() if hasattr(flow_state, "asdict") else flow_state
    for node in state_dict.get("nodes", []):
        if node.get("selected"):
            return node["id"]
    return None


def flow_nodes() -> list[StreamlitFlowNode]:
    nodes: list[StreamlitFlowNode] = []
    for idx, machine in enumerate(st.session_state.machines):
        pos = machine.get("position", {"x": 120 * idx, "y": 100 * idx})
        icon_key = next((k for k in MACHINE_TYPE_ICON if machine["machine_type"].startswith(k)), "")
        icon = MACHINE_TYPE_ICON.get(icon_key, "🏭")
        nodes.append(
            StreamlitFlowNode(
                id=machine["id"],
                pos=(float(pos["x"]), float(pos["y"])),
                data={
                    "content": f"{icon} {machine['id']}\n{machine['machine_type']}\n{machine['recipe']}",
                },
                draggable=True,
                connectable=True,
                selectable=True,
            )
        )
    return nodes


def flow_edges() -> list[StreamlitFlowEdge]:
    return [
        StreamlitFlowEdge(
            id=edge["id"],
            source=edge["source"],
            target=edge["target"],
            animated=True,
            marker_end={"type": "arrowclosed"},
            label=edge_label(edge),
        )
        for edge in st.session_state.edges
    ]


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
        edge for edge in st.session_state.edges if (edge["source"], edge["target"]) in flow_pairs
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


def render_machine_module_editor(selected_machine: dict) -> None:
    st.markdown(f"### Modules · `{selected_machine['id']}`")
    slots = int(selected_machine.get("module_slots", DEFAULT_SLOTS.get(selected_machine["machine_type"], 4)))

    expanded_modules: list[dict | None] = [None] * slots
    idx = 0
    for mod in selected_machine.get("modules", []):
        for _ in range(int(mod["count"])):
            if idx < slots:
                expanded_modules[idx] = {"module": mod["module"], "quality": mod["quality"]}
            idx += 1

    cols = st.columns(min(slots, 6))
    for i in range(slots):
        col = cols[i % len(cols)]
        current = expanded_modules[i]
        label = "[ Empty ]" if current is None else module_pretty_name(current["module"])
        if col.button(label, key=f"slot_{selected_machine['id']}_{i}", use_container_width=True):
            st.session_state.module_slot_to_edit = i

    if st.session_state.module_slot_to_edit is not None:
        slot_index = st.session_state.module_slot_to_edit
        st.markdown(f"Edit slot {slot_index + 1}")

        module_options = [m.value for m in ModuleName]
        quality_options = [q.value for q in Quality]

        picker_cols = st.columns(3)
        for i, module_value in enumerate(module_options):
            with picker_cols[i % 3]:
                if st.button(module_pretty_name(module_value), key=f"module_pick_{module_value}"):
                    expanded_modules[slot_index] = {"module": module_value, "quality": "normal"}

        edit_cols = st.columns([2, 1, 1])
        with edit_cols[0]:
            chosen = expanded_modules[slot_index]
            if chosen:
                new_quality = st.selectbox(
                    "Quality", quality_options, index=quality_options.index(chosen["quality"]), key="slot_quality"
                )
                chosen["quality"] = new_quality
        with edit_cols[1]:
            if st.button("Clear slot"):
                expanded_modules[slot_index] = None
        with edit_cols[2]:
            if st.button("Done"):
                st.session_state.module_slot_to_edit = None

    grouped: dict[tuple[str, str], int] = {}
    for mod in expanded_modules:
        if mod is None:
            continue
        key = (mod["module"], mod["quality"])
        grouped[key] = grouped.get(key, 0) + 1

    selected_machine["modules"] = [
        {"module": module_name, "quality": quality, "count": count}
        for (module_name, quality), count in grouped.items()
    ]


init_state()
st.title("Factorio Quality Calculator UI")
st.caption("Graph-first layout: canvas in main view, controls in sidebar.")

with st.sidebar:
    st.subheader("Canvas controls")
    st.caption("Double-click add-node popups are not available in streamlit-flow; use the quick add menu below.")

    with st.popover("➕ Add machine", use_container_width=True):
        machine_id = st.text_input("Machine ID", placeholder="e.g. smelter-1")
        machine_type = st.selectbox("Machine type", [m.value for m in MachineType])
        supported = sorted(r.value for r in MACHINE_SUPPORTED_RECIPES[MachineType(machine_type)])
        recipe = st.selectbox("Recipe", supported)
        machine_quality = st.selectbox("Machine quality", [q.value for q in Quality])
        module_slots = st.number_input(
            "Module slots",
            min_value=0,
            max_value=12,
            value=DEFAULT_SLOTS.get(machine_type, 4),
            step=1,
        )
        count_text = st.text_input("Count (blank = auto)", "")
        pos_x = st.number_input("X", value=float(120 * (len(st.session_state.machines) + 1)))
        pos_y = st.number_input("Y", value=float(80 * (len(st.session_state.machines) + 1)))
        if st.button("Add machine node", use_container_width=True):
            if not machine_id:
                st.error("Machine ID is required.")
            elif any(m["id"] == machine_id for m in st.session_state.machines):
                st.error("Machine ID already exists.")
            else:
                machine = Machine(
                    id=machine_id,
                    machine_type=MachineType(machine_type),
                    recipe=RecipeName(recipe),
                    count=None if count_text.strip() == "" else float(count_text),
                    machine_quality=Quality(machine_quality),
                    modules=(),
                )
                st.session_state.machines.append(machine_payload(machine, (pos_x, pos_y), int(module_slots)))
                st.success(f"Added {machine_id}")

    if st.button("🧹 Clear graph", use_container_width=True):
        for key in ("machines", "edges", "targets", "throughputs", "external_inputs"):
            st.session_state[key] = []
        st.session_state.selected_machine_id = None
        st.session_state.module_slot_to_edit = None
        st.rerun()

    st.divider()
    st.subheader("Factorio icon settings")
    factorio_install_path = st.text_input("Factorio install path", value=os.getenv("FACTORIO_INSTALL_PATH", ""))
    item_dump_path = st.text_input("item_dump.lua path", value="item_dump.lua")
    item_icon_map = load_item_icons(item_dump_path)
    st.caption(f"Loaded {len(item_icon_map)} icon refs")

canvas_state = StreamlitFlowState(nodes=flow_nodes(), edges=flow_edges())
flow_result = streamlit_flow(
    "crafting-flow",
    canvas_state,
    allow_new_edges=True,
    fit_view=True,
    show_controls=True,
    show_minimap=True,
    height=700,
)
sync_from_flow(flow_result)

selected_id = selected_flow_machine(flow_result)
if selected_id:
    st.session_state.selected_machine_id = selected_id

with st.sidebar:
    if st.session_state.selected_machine_id:
        selected_machine = next(
            (m for m in st.session_state.machines if m["id"] == st.session_state.selected_machine_id), None
        )
        if selected_machine:
            render_machine_module_editor(selected_machine)

    st.divider()
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
        icon_ref = item_icon_map.get(selected_edge["item"])
        icon_path = resolve_icon_path(icon_ref, factorio_install_path)
        if icon_path:
            st.image(icon_path, width=34)
        selected_edge["share"] = st.number_input("Share", min_value=0.0, max_value=1.0, value=float(selected_edge["share"]))
        selected_edge["quality"] = st.selectbox(
            "Quality scope",
            ["(any)"] + [q.value for q in Quality],
            index=(["(any)"] + [q.value for q in Quality]).index(selected_edge["quality"]),
        )

        if st.button("Delete edge", use_container_width=True):
            st.session_state.edges = [e for e in st.session_state.edges if e["id"] != selected_edge["id"]]
            st.rerun()

st.subheader("Run calculations")
planning_mode = st.radio("Backcalc mode", ["fractional", "integer"], horizontal=True)

if st.button("Run backcalculation + analysis", type="primary"):
    try:
        graph = build_graph()
        graph.backcalculate_requirements(mode=planning_mode)
        report = MachineAnalysis().analyze(graph)

        machine_rows = [
            {
                "machine_id": mid,
                "machine_type": machine.machine_type.value,
                "recipe": machine.recipe.value,
                "count": machine.count,
                "required_run_rate": machine.required_run_rate,
            }
            for mid, machine in graph.machines.items()
        ]
        st.markdown("#### Machine requirements")
        st.dataframe(pd.DataFrame(machine_rows), use_container_width=True)

        demand_rows = [
            {"item": item.value, "quality": quality.value, "required_external_input_rate": rate}
            for (item, quality), rate in graph.required_external_inputs.items()
        ]
        st.markdown("#### Required external inputs")
        st.dataframe(pd.DataFrame(demand_rows), use_container_width=True)

        report_rows = [{"machine_id": mid, "utilization": m_report.utilization} for mid, m_report in report.machines.items()]
        st.markdown("#### Utilization")
        st.dataframe(pd.DataFrame(report_rows), use_container_width=True)
    except Exception as exc:
        st.error(f"Calculation failed: {exc}")
