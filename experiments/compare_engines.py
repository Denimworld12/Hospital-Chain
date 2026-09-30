"""Run the three MedChain engines headless on the real UCI chain and plot loss vs bits transmitted.

    python experiments/compare_engines.py            # writes experiments/out/

Everything runs in one process (no Docker, no HTTP) with the production engine code:
node/gossip.py for the naive gossip baseline and node/qgadmm/ for Q-GADMM and
full-precision GADMM, on the same three hospital slices, train/test split and
hyperparameters as config/*.yaml. The bits on the x axis are the exact packet
bytes each engine puts on the wire (the same payload_bytes the nodes log to
updates_meta), times 8.

Outputs (experiments/out/):
    compare_engines.png   loss vs cumulative bits, one curve per engine (the money shot)
    rho_sweep.png         Q-GADMM b=2 at rho in {5, 15, 24, 50}: residual and loss
    payload_ratio.png     32-bit reference / Q-GADMM packet size vs model size d
    summary.json          the numbers behind the three figures
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sklearn.linear_model import LogisticRegression  # noqa: E402

from node.config import load_config  # noqa: E402
from node.engine import NeighborUpdate  # noqa: E402
from node.gossip import GossipBaselineEngine  # noqa: E402
from node.qgadmm import quantizer as qz  # noqa: E402
from node.qgadmm.engine import QGADMMEngine  # noqa: E402
from node.qgadmm.scheduler import simulate  # noqa: E402
from node.qgadmm.state import ChainPosition  # noqa: E402
from node.trainer import LocalTrainer  # noqa: E402

CONFIGS = sorted((ROOT / "config").glob("hospital-*.yaml"))
SWEEP_RHOS = (5.0, 15.0, 24.0, 50.0)

# Pa'lais palette (design/tokens.css): indigo is the brand line; gold and orange are accents.
INDIGO, GOLD, ORANGE, SKY, SAGE, INK, CREAM = "#234386", "#ffc400", "#ed7328", "#6aa8dc", "#a2d3a6", "#000000", "#fbf9f6"


def load_chain():
    cfgs = [load_config(p) for p in CONFIGS]
    chain = cfgs[0].chain
    cfgs.sort(key=lambda c: chain.index(c.node_id))
    trainers = []
    for c in cfgs:
        data = ROOT / "data" / "slices" / Path(c.data_slice).name  # the container path, resolved on the host
        trainers.append(LocalTrainer(data, test_fraction=c.training.test_fraction, seed=c.training.seed,
                                     learning_rate=c.training.learning_rate, local_epochs=c.training.local_epochs))
    return cfgs, trainers


def admm_engines(cfgs, trainers, bits, rho=None):
    out = []
    for c, t in zip(cfgs, trainers):
        pos = ChainPosition(c.chain, c.node_id)
        out.append(QGADMMEngine(t.X_train, t.y_train, t.X_test, t.y_test, position=pos, rho=rho or c.rho,
                                alpha=c.alpha, bits=bits, local_epochs=c.local_epochs, lr=c.lr,
                                seed=c.seed + pos.position))
    return out


def global_loss(trainers, weights):
    """Mean log-loss over every hospital's training rows, each hospital evaluated at its own model."""
    total = sum(len(t.y_train) for t in trainers)
    s = 0.0
    for t, w in zip(trainers, weights):
        saved, t.weights = t.weights, w
        s += t.loss(t.X_train, t.y_train) * len(t.y_train)
        t.weights = saved
    return s / total


def run_admm(cfgs, trainers, bits, rounds, rho=None):
    engines = admm_engines(cfgs, trainers, bits, rho)
    t0 = time.time()
    res = simulate(engines, rounds, epsilon=cfgs[0].epsilon)
    rows = [{"round": r.round, "loss": r.objective, "cumulative_bits": r.cumulative_bits,
             "max_residual": r.max_residual, "payload_bytes": r.payload_bytes, "messages": r.messages}
            for r in res.rounds]
    return {"rows": rows, "converged_at": res.converged_at, "seconds": round(time.time() - t0, 2),
            "label": engines[0].label, "d": engines[0].d}


def run_gossip(cfgs, trainers, rounds):
    """The node round loop of node/runner.py, in process: train, send float64 weights, average."""
    engines = [GossipBaselineEngine(t) for t in trainers]
    ids = [c.node_id for c in cfgs]
    neighbours = {c.node_id: [n.id for n in c.neighbours] for c in cfgs}
    for e in engines:
        e.reset()
    rows, cum_bits = [], 0
    t0 = time.time()
    for r in range(1, rounds + 1):
        for e in engines:
            e.train_round(r)
        pkgs = {i: e.package_update(r) for i, e in zip(ids, engines)}
        round_bytes = messages = 0
        inbox = {i: [] for i in ids}
        for i in ids:
            for m in neighbours[i]:
                p = pkgs[i]
                inbox[m].append(NeighborUpdate(i, r, p.body, p.checksum, p.dtype))
                round_bytes += p.payload_bytes
                messages += 1
        for i, e in zip(ids, engines):
            e.apply_neighbor_update(inbox[i])
        cum_bits += round_bytes * 8
        w = {i: e.trainer.weights for i, e in zip(ids, engines)}
        gap = max(float(np.linalg.norm(w[a] - w[b])) for a, b in zip(ids, ids[1:]))
        rows.append({"round": r, "loss": global_loss(trainers, list(w.values())), "cumulative_bits": cum_bits,
                     "payload_bytes": round_bytes, "messages": messages, "max_neighbour_gap": gap})
    for t in trainers:
        t.reset()
    return {"rows": rows, "converged_at": None, "seconds": round(time.time() - t0, 2),
            "label": engines[0].label, "d": int(engines[0].trainer.weights.size)}


def centralized_loss(trainers):
    X = np.vstack([t.X_train for t in trainers])
    y = np.concatenate([t.y_train for t in trainers])
    with np.errstate(all="ignore"):
        clf = LogisticRegression(penalty=None, max_iter=10_000).fit(X, y)
        p = np.clip(clf.predict_proba(X)[:, 1], 1e-15, 1 - 1e-15)
    return float(-np.mean(y * np.log(p) + (1 - y) * np.log(1 - p)))


def payload_ratio_table(ds=(16, 64, 256, 1024, 4096, 16384, 65536)):
    """Measured: encode a real packet at each d and compare its length to the 32-bit reference 4*d."""
    rng = np.random.default_rng(0)
    table = {}
    for b in (2, 4, 8):
        pts = []
        for d in ds:
            pkt = qz.quantize(rng.normal(size=d), np.zeros(d), b, rng)
            size = len(qz.encode(pkt))
            pts.append({"d": d, "packet_bytes": size, "ref32_bytes": 4 * d, "ratio": 4 * d / size})
        table[b] = pts
    return table


# -- figures ------------------------------------------------------------------------------


def style(ax):
    ax.set_facecolor(CREAM)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    for side in ("left", "bottom"):
        ax.spines[side].set_color(INK)
        ax.spines[side].set_linewidth(1)
    ax.tick_params(colors=INK, labelsize=9)
    ax.grid(True, which="major", color="#d6d6d6", linewidth=0.6)


CURVES = [  # key, colour, line style
    ("gossip", ORANGE, "-"),
    ("gadmm_fp", INK, "--"),
    ("qgadmm_b8", SKY, "-"),
    ("qgadmm_b4", GOLD, "-"),
    ("qgadmm_b2", INDIGO, "-"),
]


def plot_money_shot(results, central, out: Path):
    fig, ax = plt.subplots(figsize=(9, 5.6), dpi=150)
    fig.patch.set_facecolor(CREAM)
    style(ax)
    for key, colour, ls in CURVES:
        r = results[key]
        x = [row["cumulative_bits"] for row in r["rows"]]
        y = [row["loss"] for row in r["rows"]]
        name = r["label"] if key != "gossip" else (
            f"naive gossip baseline (float64; neighbours still differ by {r['rows'][-1]['max_neighbour_gap']:.2f})")
        if r["converged_at"]:
            name += f" - consensus at round {r['converged_at']}"
        ax.plot(x, y, ls, color=colour, linewidth=2.2 if key == "qgadmm_b2" else 1.5, label=name)
        if r["converged_at"]:
            ax.plot(x[-1], y[-1], "o", color=colour, markersize=6, markeredgecolor=INK, markeredgewidth=0.6)
    ax.axhline(central, color=INK, linewidth=0.8, linestyle=":", label=f"centralized optimum (all data pooled): {central:.4f}")
    ax.set_xscale("log")
    ax.set_xlabel("cumulative bits transmitted, all links (log scale)", color=INK)
    ax.set_ylabel("training loss (mean log-loss, all hospitals)", color=INK)
    ax.set_title("MedChain: loss vs communication - 3-hospital chain, UCI heart disease (d = 16)", color=INDIGO,
                 fontsize=11, loc="left", fontweight="bold")
    lo = min(min(row["loss"] for row in results[k]["rows"]) for k, _, _ in CURVES)
    ax.set_ylim(lo - 0.02, min(0.75, max(results["qgadmm_b2"]["rows"][0]["loss"], 0.6) + 0.02))
    leg = ax.legend(fontsize=8, frameon=True, loc="upper right")
    leg.get_frame().set_edgecolor(INK)
    leg.get_frame().set_facecolor("#ffffff")
    fig.tight_layout()
    fig.savefig(out, facecolor=CREAM)
    plt.close(fig)


def plot_rho_sweep(sweep, central, out: Path, epsilon: float):
    fig, (a1, a2) = plt.subplots(1, 2, figsize=(12, 4.8), dpi=150)
    fig.patch.set_facecolor(CREAM)
    shades = {5.0: SKY, 15.0: GOLD, 24.0: INDIGO, 50.0: ORANGE}
    for rho, r in sweep.items():
        rounds = [row["round"] for row in r["rows"]]
        tag = f"rho = {rho:g}" + (f" (consensus at {r['converged_at']})" if r["converged_at"] else " (no consensus)")
        a1.plot(rounds, [max(row["max_residual"], 1e-9) for row in r["rows"]], color=shades[rho], linewidth=1.5, label=tag)
        a2.plot([row["cumulative_bits"] for row in r["rows"]], [row["loss"] for row in r["rows"]], color=shades[rho],
                linewidth=1.5, label=f"rho = {rho:g}")
    for ax in (a1, a2):
        style(ax)
    a1.axhline(epsilon, color=INK, linestyle=":", linewidth=0.8, label=f"epsilon = {epsilon:g}")
    a1.set_yscale("log")
    a1.set_xlabel("round", color=INK)
    a1.set_ylabel("max primal residual over edges", color=INK)
    a1.set_title("Q-GADMM b=2: disagreement between neighbours", color=INDIGO, fontsize=10, loc="left", fontweight="bold")
    a2.axhline(central, color=INK, linestyle=":", linewidth=0.8, label="centralized optimum")
    a2.set_xscale("log")
    a2.set_xlabel("cumulative bits transmitted (log scale)", color=INK)
    a2.set_ylabel("training loss", color=INK)
    a2.set_title("Q-GADMM b=2: loss vs bits", color=INDIGO, fontsize=10, loc="left", fontweight="bold")
    for ax in (a1, a2):
        leg = ax.legend(fontsize=8)
        leg.get_frame().set_edgecolor(INK)
    fig.tight_layout()
    fig.savefig(out, facecolor=CREAM)
    plt.close(fig)


def plot_payload_ratio(table, measured_d16: float, out: Path):
    fig, ax = plt.subplots(figsize=(8, 4.8), dpi=150)
    fig.patch.set_facecolor(CREAM)
    style(ax)
    colours = {2: INDIGO, 4: GOLD, 8: ORANGE}
    for b, pts in table.items():
        ax.plot([p["d"] for p in pts], [p["ratio"] for p in pts], "o-", color=colours[b], linewidth=1.5, markersize=4,
                label=f"b = {b}: packet = ceil({b}d/8) + 8 bytes")
        ax.axhline(32 / b, color=colours[b], linestyle=":", linewidth=0.8)
        ax.text(table[b][-1]["d"], 32 / b, f" {32 // b}x asymptote", va="bottom", ha="right", fontsize=8, color=INK)
    ax.plot([16], [measured_d16], "s", color=INDIGO, markersize=9, markeredgecolor=INK,
            label=f"this project's model, measured in the run: d = 16 -> {measured_d16:.1f}x")
    ax.set_xscale("log")
    ax.set_xlabel("model size d (parameters)", color=INK)
    ax.set_ylabel("32-bit reference / bytes sent per packet", color=INK)
    ax.set_title("Why 5.3x and not 16x at d = 16: the 8-byte (b, R) header dominates small models", color=INDIGO,
                 fontsize=10, loc="left", fontweight="bold")
    leg = ax.legend(fontsize=8, loc="lower right")
    leg.get_frame().set_edgecolor(INK)
    fig.tight_layout()
    fig.savefig(out, facecolor=CREAM)
    plt.close(fig)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--rounds", type=int, default=None, help="round limit (default: training.rounds from config)")
    ap.add_argument("--sweep-rounds", type=int, default=500, help="round limit for the rho sweep")
    ap.add_argument("--out", type=Path, default=ROOT / "experiments" / "out")
    ap.add_argument("--no-sweep", action="store_true", help="skip the rho sweep")
    args = ap.parse_args(argv)
    args.out.mkdir(parents=True, exist_ok=True)

    cfgs, trainers = load_chain()
    rounds = args.rounds or cfgs[0].training.rounds
    central = centralized_loss(trainers)
    print(f"chain {' - '.join(c.node_id for c in cfgs)}; rho={cfgs[0].rho} E={cfgs[0].local_epochs} "
          f"lr={cfgs[0].lr} epsilon={cfgs[0].epsilon}; centralized optimum {central:.5f}")

    results = {"gossip": run_gossip(cfgs, trainers, rounds)}
    results["gadmm_fp"] = run_admm(cfgs, trainers, qz.FULL_PRECISION_BITS, rounds)
    for b in (8, 4, 2):
        results[f"qgadmm_b{b}"] = run_admm(cfgs, trainers, b, rounds)
    for key, r in results.items():
        last = r["rows"][-1]
        agree = (f"max edge residual {last['max_residual']:.1e}" if "max_residual" in last
                 else f"max neighbour weight gap {last['max_neighbour_gap']:.2f} (no consensus mechanism)")
        print(f"  {r['label']:<40} rounds={last['round']:>4} consensus={r['converged_at']!s:>5} "
              f"loss={last['loss']:.5f} ({(last['loss'] / central - 1) * 100:+.2f}% vs centralized) "
              f"bytes={last['cumulative_bits'] // 8:>9,}  {agree}  [{r['seconds']}s]")
    plot_money_shot(results, central, args.out / "compare_engines.png")

    q2 = results["qgadmm_b2"]["rows"]
    measured = 4 * results["qgadmm_b2"]["d"] * q2[-1]["messages"] / q2[-1]["payload_bytes"]
    table = payload_ratio_table()
    plot_payload_ratio(table, measured, args.out / "payload_ratio.png")
    print(f"  per-packet ratio vs 32-bit at d=16, b=2: {measured:.2f}x (coordinates only: 16x); "
          f"at d=65536: {table[2][-1]['ratio']:.2f}x")

    sweep = {}
    if not args.no_sweep:
        for rho in SWEEP_RHOS:
            sweep[rho] = run_admm(cfgs, trainers, 2, args.sweep_rounds, rho=rho)
            last = sweep[rho]["rows"][-1]
            print(f"  rho sweep b=2 rho={rho:>4g}: consensus={sweep[rho]['converged_at']!s:>5} "
                  f"loss={last['loss']:.5f} max residual={last['max_residual']:.2e}")
        plot_rho_sweep(sweep, central, args.out / "rho_sweep.png", cfgs[0].epsilon)

    summary = {
        "config": {"chain": list(cfgs[0].chain), "rho": cfgs[0].rho, "alpha": cfgs[0].alpha,
                   "local_epochs": cfgs[0].local_epochs, "lr": cfgs[0].lr, "epsilon": cfgs[0].epsilon,
                   "rounds": rounds},
        "centralized_loss": central,
        "engines": {k: {"label": r["label"], "d": r["d"], "rounds_run": r["rows"][-1]["round"],
                        "converged_at": r["converged_at"], "final_loss": r["rows"][-1]["loss"],
                        "total_bytes": r["rows"][-1]["cumulative_bits"] // 8,
                        "final_disagreement": r["rows"][-1].get("max_residual", r["rows"][-1].get("max_neighbour_gap")),
                        "bytes_per_message": r["rows"][-1]["payload_bytes"] / max(1, r["rows"][-1]["messages"])}
                    for k, r in results.items()},
        "payload_ratio_vs_32bit": {"measured_d16_b2": measured, "by_bits": {str(b): pts for b, pts in table.items()}},
        "rho_sweep_b2": {str(rho): {"converged_at": r["converged_at"], "final_loss": r["rows"][-1]["loss"],
                                    "final_max_residual": r["rows"][-1]["max_residual"],
                                    "total_bytes": r["rows"][-1]["cumulative_bits"] // 8}
                         for rho, r in sweep.items()},
    }
    (args.out / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    print(f"wrote {', '.join(p.name for p in sorted(args.out.glob('*')))} to {args.out}")
    return summary


if __name__ == "__main__":
    main()
