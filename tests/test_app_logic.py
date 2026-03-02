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
    _, _, err = app.validate_connection({"edges": []}, source_output, target_machine)
    assert "cannot have outgoing" in err


def test_input_io_can_connect_out_to_machine_when_recipe_accepts_item():
    source_input = {"id": "io1", "kind": app.NodeKind.IO, "io_mode": "input", "item": "iron-plate"}
    target_machine = {"id": "m1", "kind": app.NodeKind.MACHINE, "recipe": "iron-gear-wheel"}
    item, share, err = app.validate_connection({"edges": []}, source_input, target_machine)
    assert err == ""
    assert item == "iron-plate"
    assert share == 1.0


def test_remaining_share():
    state = {"edges": [{"source": "m1", "item": "iron-plate", "share": 0.4}, {"source": "m1", "item": "iron-plate", "share": 0.2}]}
    assert abs(app._remaining_share(state, "m1", "iron-plate") - 0.4) < 1e-9


def test_validate_connection_allows_zero_share_edge_creation():
    state = {"edges": [{"source": "m1", "target": "x", "item": "iron-gear-wheel", "share": 1.0}]}
    source_machine = {"id": "m1", "kind": app.NodeKind.MACHINE, "recipe": "iron-gear-wheel"}
    target_machine = {"id": "m2", "kind": app.NodeKind.MACHINE, "recipe": "automation-science-pack"}
    item, share, err = app.validate_connection(state, source_machine, target_machine)
    assert err == ""
    assert item == app.Item.IRON_GEAR_WHEEL.value
    assert share == 0.0


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
