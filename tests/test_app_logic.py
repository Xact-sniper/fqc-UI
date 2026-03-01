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
    s = {"id": "io1", "kind": app.NodeKind.IO, "io_mode": "input", "item": "iron-plate"}
    t = {"id": "m1", "kind": app.NodeKind.MACHINE, "recipe": "iron-gear-wheel"}
    _, _, err = app.validate_connection({"edges": []}, s, t)
    assert "cannot have outgoing" in err


def test_remaining_share():
    state = {"edges": [{"source": "m1", "item": "iron-plate", "share": 0.4}, {"source": "m1", "item": "iron-plate", "share": 0.2}]}
    assert abs(app._remaining_share(state, "m1", "iron-plate") - 0.4) < 1e-9
