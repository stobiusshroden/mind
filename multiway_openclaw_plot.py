"""Multiway plotter (Wolfram-style) for an OpenClaw-ish multi-agent A/B toy.

Goal: generate a multiway states-graph from rewrite rules (string substitution)
that mirrors the chapter's A/B examples, but interpret A and B as agent roles.

Outputs:
- out/multiway_ab.dot  (Graphviz DOT; view with any DOT viewer)
- out/multiway_ab.json (nodes/edges)

No Graphviz install required to generate DOT.
"""

from __future__ import annotations

import argparse
import json
import os
from dataclasses import dataclass
from hashlib import blake2b
from pathlib import Path
from typing import Dict, List, Tuple


@dataclass(frozen=True)
class Rule:
    lhs: str
    rhs: str


def apply_all(s: str, rules: List[Rule]) -> List[Tuple[str, Dict]]:
    """Return all one-step rewrites (multiway). Overlapping matches are allowed.

    Each result is (new_string, meta) where meta contains rule+pos.
    """
    out: List[Tuple[str, Dict]] = []
    for ridx, r in enumerate(rules):
        start = 0
        while True:
            pos = s.find(r.lhs, start)
            if pos < 0:
                break
            ns = s[:pos] + r.rhs + s[pos + len(r.lhs) :]
            out.append(
                (
                    ns,
                    {
                        "rule": f"{r.lhs}->{r.rhs}",
                        "ruleIndex": ridx,
                        "pos": pos,
                        "before": s,
                    },
                )
            )
            start = pos + 1  # allow overlaps
    return out


def node_id(s: str) -> str:
    h = blake2b(s.encode("utf-8"), digest_size=8).hexdigest()
    return f"n{h}"


def build_multiway(initial: str, rules: List[Rule], max_depth: int, max_nodes: int) -> Dict:
    """Build a states-graph (merge identical states)."""
    nodes: Dict[str, Dict] = {}
    edges: List[Dict] = []

    init_id = node_id(initial)
    nodes[init_id] = {"id": init_id, "state": initial, "depth": 0}

    frontier = [(initial, 0)]
    seen_depth: Dict[str, int] = {initial: 0}

    while frontier:
        s, d = frontier.pop(0)
        if d >= max_depth:
            continue
        if len(nodes) >= max_nodes:
            break

        src = node_id(s)
        for ns, meta in apply_all(s, rules):
            dst = node_id(ns)
            if dst not in nodes:
                nodes[dst] = {"id": dst, "state": ns, "depth": d + 1}

            edges.append(
                {
                    "src": src,
                    "dst": dst,
                    "meta": meta,
                    "depth": d,
                }
            )

            # BFS by depth on unique states
            if ns not in seen_depth:
                seen_depth[ns] = d + 1
                frontier.append((ns, d + 1))

            if len(nodes) >= max_nodes:
                break

    return {"initial": initial, "rules": [r.__dict__ for r in rules], "nodes": list(nodes.values()), "edges": edges}


def to_dot(g: Dict) -> str:
    """Render DOT with simple styling. A and B are color-coded."""
    def label_for(state: str) -> str:
        return state.replace('"', '\\"')

    lines = ["digraph multiway {", "  rankdir=LR;", "  node [shape=box,fontname=Consolas];"]

    for n in g["nodes"]:
        st = n["state"]
        # heuristic: color by dominant symbol
        a = st.count("A")
        b = st.count("B")
        if a > b:
            color = "#2c4db0"  # Tachikoma blue-ish
        elif b > a:
            color = "#203a82"  # deeper blue
        else:
            color = "#1f2f4a"  # neutral
        lines.append(f"  {n['id']} [label=\"{label_for(st)}\", style=filled, fillcolor=\"{color}\"]; ")

    for e in g["edges"]:
        r = e["meta"].get("rule", "")
        pos = e["meta"].get("pos", 0)
        lines.append(f"  {e['src']} -> {e['dst']} [label=\"{r}@{pos}\", fontname=Consolas, fontsize=10];")

    lines.append("}")
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--initial", default="A", help="initial string/state")
    ap.add_argument("--depth", type=int, default=6)
    ap.add_argument("--max-nodes", type=int, default=400)
    ap.add_argument(
        "--preset",
        default="ab-confluence",
        choices=["ab-confluence", "ab-branchy", "ba-sort"],
        help="rule preset mirroring Wolfram chapter examples",
    )
    args = ap.parse_args()

    # Presets:
    # - ab-confluence: has branching but reconverges quickly (diamond-ish)
    # - ab-branchy: tends to keep branching without immediate reconvergence
    # - ba-sort: BA->AB sorting (confluent)
    if args.preset == "ab-confluence":
        rules = [Rule("A", "BBB"), Rule("BB", "A")]
    elif args.preset == "ab-branchy":
        rules = [Rule("A", "AA"), Rule("AA", "AB")]
    else:  # ba-sort
        rules = [Rule("BA", "AB")]

    g = build_multiway(args.initial, rules, max_depth=args.depth, max_nodes=args.max_nodes)

    outdir = Path(__file__).resolve().parent / "out"
    outdir.mkdir(parents=True, exist_ok=True)

    (outdir / "multiway_ab.json").write_text(json.dumps(g, indent=2), encoding="utf-8")
    (outdir / "multiway_ab.dot").write_text(to_dot(g), encoding="utf-8")

    print("wrote", outdir / "multiway_ab.dot")
    print("wrote", outdir / "multiway_ab.json")
    print("nodes", len(g["nodes"]), "edges", len(g["edges"]))


if __name__ == "__main__":
    main()
