import re
from pathlib import Path

import pytest

from node.config import load_config

ROOT = Path(__file__).resolve().parents[2]
CONFIGS = sorted((ROOT / "config").glob("*.yaml"))


def test_three_hospital_configs_load():
    cfgs = [load_config(p) for p in CONFIGS]
    assert len(cfgs) == 3
    assert len({c.node_id for c in cfgs}) == 3
    assert len({c.port for c in cfgs}) == 3
    for c in cfgs:
        assert c.engine == "gossip-baseline"
        assert c.bits == 64, "the naive gossip baseline sends float64; bits must say so"
        assert c.training.rounds == 20
        assert c.timing.heartbeat_interval_s == 5


def test_neighbour_links_are_symmetric_and_resolve():
    cfgs = {c.node_id: c for c in (load_config(p) for p in CONFIGS)}
    for c in cfgs.values():
        assert c.neighbours, f"{c.node_id} has no neighbours"
        for n in c.neighbours:
            assert n.id in cfgs, f"{c.node_id} lists unknown neighbour {n.id}"
            assert cfgs[n.id].neighbour(c.node_id) is not None, f"{n.id} does not list {c.node_id} back"
            assert n.url.endswith(f":{cfgs[n.id].port}")


def test_each_config_points_at_its_own_slice():
    for c in (load_config(p) for p in CONFIGS):
        name = Path(c.data_slice).name
        assert name == f"{c.node_id}.csv"
        assert (ROOT / "data" / "slices" / name).is_file()


def test_node_code_hardcodes_no_hospital_identity():
    offenders = []
    for py in (ROOT / "node").glob("*.py"):
        for i, line in enumerate(py.read_text().splitlines(), 1):
            if re.search(r"hospital-[a-z]", line, re.I):
                offenders.append(f"{py.name}:{i}: {line.strip()}")
    assert not offenders, "\n".join(offenders)


def test_rejects_self_neighbour(tmp_path):
    p = tmp_path / "bad.yaml"
    p.write_text("node_id: x\nport: 1\ndata_slice: s.csv\nneighbours:\n  - id: x\n    url: http://x:1\n")
    with pytest.raises(ValueError):
        load_config(p)
