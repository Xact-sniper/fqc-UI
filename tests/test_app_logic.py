import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app


def test_create_io_without_machine_id_requirement():
    state = app.default_state()
    popup = {"x": 10, "y": 20}
    out, _, msg = app.create_io(None, app.Item.IRON_PLATE.value, "input", 1.0, app.Quality.NORMAL.value, popup, state)
    assert len(out["nodes"]) == 1
    assert out["nodes"][0]["kind"] == app.NodeKind.IO
    assert "Created input node" in msg


def test_validate_connection_direction():
    source_output = {"id": "io1", "kind": app.NodeKind.IO, "io_mode": "output", "item": "iron-plate"}
    target_machine = {"id": "m1", "kind": app.NodeKind.MACHINE, "recipe": "iron-gear-wheel"}
    _, err = app.validate_connection({"edges": []}, source_output, target_machine)
    assert "cannot have outgoing" in err


def test_input_io_can_connect_out_to_machine_when_recipe_accepts_item():
    source_input = {"id": "io1", "kind": app.NodeKind.IO, "io_mode": "input", "item": "iron-plate"}
    target_machine = {"id": "m1", "kind": app.NodeKind.MACHINE, "recipe": "iron-gear-wheel"}
    ok, err = app.validate_connection({"edges": []}, source_input, target_machine)
    assert err == ""
    assert ok is True


def test_validate_connection_allows_connection_without_edge_attributes():
    state = {"edges": [{"source": "m1", "target": "x"}]}
    source_machine = {"id": "m1", "kind": app.NodeKind.MACHINE, "recipe": "iron-gear-wheel"}
    target_machine = {"id": "m2", "kind": app.NodeKind.MACHINE, "recipe": "automation-science-pack"}
    ok, err = app.validate_connection(state, source_machine, target_machine)
    assert err == ""
    assert ok is True


def test_create_recipe_uses_selected_quality_and_click_position():
    state = app.default_state()
    popup = {"x": 123, "y": 456}
    out, _, _ = app.create_recipe(None, app.RecipeName.IRON_GEAR_WHEEL.value, app.Quality.LEGENDARY.value, popup, state)
    node = out["nodes"][0]
    assert node["quality"] == app.Quality.LEGENDARY.value
    assert node["x"] == 123
    assert node["y"] == 456


def test_machine_label_is_human_readable():
    assert app.machine_label(app.MachineType.ASSEMBLING_MACHINE_3.value) == "Assembling Machine 3"


def test_run_analysis_returns_graph_level_section():
    state, _ = app.load_demo(None)
    _, _, ui = app.run_analysis(None, state)
    texts = [getattr(c, "children", "") for c in ui if hasattr(c, "children")]
    assert any("Graph-level demand/supply" == t for t in texts)


def test_compute_edge_metrics_reports_actual_and_required_rates():
    state, _ = app.load_demo(None)
    graph = app.build_fqc_graph(state)
    graph.backcalculate_requirements(mode="fractional")
    report = app.MachineAnalysis().analyze(graph)
    metrics = app.compute_edge_metrics(state, report, True)
    assert "e2" in metrics
    assert metrics["e2"]["actual_rate"] > 0.0
    assert metrics["e2"]["required_rate"] is not None


def test_derive_edge_semantics_from_connected_nodes():
    state, _ = app.load_demo(None)
    e = next(x for x in state["edges"] if x["id"] == "e2")
    sem = app.derive_edge_semantics(state, e)
    assert sem is not None
    assert sem["item"] == app.Item.IRON_GEAR_WHEEL.value


def test_find_node_at_locates_demo_node_by_click_position():
    state, _ = app.load_demo(None)
    node = app._find_node_at(state, 260, 180)
    assert node is not None
    assert node["id"] == "m1"


def test_right_click_double_click_deletes_node_and_connected_edges():
    state, _ = app.load_demo(None)
    first = app.right_click_config_or_delete({"id": "m1"}, None, state, {"kind": None, "id": None, "ts": 0.0})
    assert first[0]["type"] == "node-config"
    second = app.right_click_config_or_delete({"id": "m1"}, None, first[1], first[4])
    assert "Deleted node" in second[5]
    assert all(n["id"] != "m1" for n in second[1]["nodes"])
