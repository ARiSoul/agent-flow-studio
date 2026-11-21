"""A node size set in the editor (layout w, h) is kept when the flow is saved and loaded."""
import pytest
import yaml

from agent_flow_studio.builder.spec import FlowSpec
from agent_flow_studio.editor.yaml_io import dump_flow, dump_raw


def flow(layout: dict) -> dict:
    return {"version": 1, "name": "sized", "title": "Sized", "start": "a",
            "agents": {"a": {"description": "A step.", "prompt": "Do it."}},
            "gates": {"end": {"kind": "stop", "label": "End"}},
            "edges": [{"from": "a", "to": "end"}], "layout": layout}


def test_size_round_trip():
    spec = FlowSpec.model_validate(flow({"a": {"x": 1.4, "y": 2, "w": 320.6, "h": 180},
                                         "end": {"x": 0, "y": 200}}))
    data = yaml.safe_load(dump_flow(spec))
    assert data["layout"] == {"a": {"x": 1, "y": 2, "w": 321, "h": 180}, "end": {"x": 0, "y": 200}}
    raw = yaml.safe_load(dump_raw(flow({"a": {"x": 1.4, "y": 2, "w": 320.6, "h": 180}}), "sized"))
    assert raw["layout"]["a"] == {"x": 1, "y": 2, "w": 321, "h": 180}


@pytest.mark.parametrize("size", [{"w": 10, "h": 100}, {"w": 300, "h": 5}])
def test_size_limits(size):
    with pytest.raises(ValueError):
        FlowSpec.model_validate(flow({"a": {"x": 0, "y": 0, **size}}))


def test_each_style_keeps_its_own_size():
    spec = FlowSpec.model_validate(flow({"a": {"x": 0, "y": 0, "w": 300, "h": 120,
                                               "compact": {"w": 210.4, "h": 64},
                                               "avatar": {"w": 120, "h": 150.6}}}))
    data = yaml.safe_load(dump_flow(spec))
    assert data["layout"]["a"] == {"x": 0, "y": 0, "w": 300, "h": 120,
                                   "compact": {"w": 210, "h": 64}, "avatar": {"w": 120, "h": 151}}
    with pytest.raises(ValueError):
        FlowSpec.model_validate(flow({"a": {"x": 0, "y": 0, "avatar": {"w": 10, "h": 150}}}))
