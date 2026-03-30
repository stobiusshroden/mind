"""Build a Wolfram-style multiway graph from *actual* OpenClaw relay events.

We treat each relay line as an "event" and build a dependency DAG:
- replies depend on their in_reply_to parent (if we can match it)
- within each actor (Tachikoma vs Major-daemon), events are totally ordered (optional; enabled by default)

Then we construct a multiway graph where each node is a *set of completed events*.
An edge adds one currently-available event (all deps satisfied).
This yields exponential growth when there are many independent events.

Outputs (in ./out):
- openclaw_multiway_events.json
- openclaw_multiway_events.dot

Usage examples:
  python openclaw_events_multiway.py --session 51d9... --limit 20 --max-nodes 800
  python openclaw_events_multiway.py --session debug --limit 12 --no-actor-order

"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from datetime import datetime
from hashlib import blake2b
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple


def _parse_ts(s: str) -> float:
    # handles 'Z' and '+00:00'
    s = s.strip()
    if s.endswith('Z'):
        s = s[:-1] + '+00:00'
    return datetime.fromisoformat(s).timestamp()


@dataclass
class Ev:
    idx: int
    ts: str
    t: float
    sessionId: str
    actor: str
    text: str
    in_reply_to_ts: Optional[str] = None
    in_reply_to_text: Optional[str] = None


def _read_jsonl(path: Path) -> List[dict]:
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding='utf-8', errors='ignore').splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except Exception:
            pass
    return out


def load_events(relay_dir: Path, session: str) -> List[Ev]:
    files = [relay_dir / 'to_major.jsonl', relay_dir / 'to_tachikoma.jsonl']
    raw: List[dict] = []
    for f in files:
        raw.extend(_read_jsonl(f))

    evs: List[Ev] = []
    for r in raw:
        if str(r.get('sessionId', '')).strip() != session:
            continue
        ts = str(r.get('ts') or '')
        if not ts:
            continue
        actor = str(r.get('from') or 'unknown')
        text = str(r.get('text') or '')
        irt = r.get('in_reply_to') or {}
        in_ts = irt.get('ts') if isinstance(irt, dict) else None
        in_txt = irt.get('text') if isinstance(irt, dict) else None
        try:
            t = _parse_ts(ts)
        except Exception:
            continue
        evs.append(Ev(idx=len(evs), ts=ts, t=t, sessionId=session, actor=actor, text=text, in_reply_to_ts=in_ts, in_reply_to_text=in_txt))

    evs.sort(key=lambda e: (e.t, e.actor, e.idx))
    # reindex in sorted order
    for i,e in enumerate(evs):
        e.idx = i
    return evs


def build_deps(evs: List[Ev], actor_order: bool = True) -> Dict[int, Set[int]]:
    """deps[i] = set of event indices that must happen before event i."""
    deps: Dict[int, Set[int]] = {e.idx: set() for e in evs}

    # Map by ts (best) and also fallback by (ts,text)
    by_ts: Dict[str, int] = {}
    by_key: Dict[Tuple[str, str], int] = {}
    for e in evs:
        by_ts[e.ts] = e.idx
        by_key[(e.ts, e.text)] = e.idx

    # reply dependencies
    for e in evs:
        if e.in_reply_to_ts:
            parent = by_ts.get(str(e.in_reply_to_ts))
            if parent is None and e.in_reply_to_text:
                # try match by text only (earliest)
                # expensive to search; do simple scan
                for p in evs:
                    if p.text == e.in_reply_to_text:
                        parent = p.idx
                        break
            if parent is not None and parent != e.idx:
                deps[e.idx].add(parent)

    if actor_order:
        # enforce per-actor time order as dependencies
        last_by_actor: Dict[str, int] = {}
        for e in evs:
            if e.actor in last_by_actor:
                deps[e.idx].add(last_by_actor[e.actor])
            last_by_actor[e.actor] = e.idx

    return deps


def _state_id(done: Set[int]) -> str:
    # stable id from sorted indices
    s = ','.join(map(str, sorted(done)))
    h = blake2b(s.encode('utf-8'), digest_size=8).hexdigest()
    return f"s{h}"


def build_multiway(evs: List[Ev], deps: Dict[int, Set[int]], limit_events: int, max_nodes: int) -> Dict:
    # consider only first N events (chronological)
    evs = evs[:limit_events]
    keep = {e.idx for e in evs}
    deps2 = {i: {d for d in deps.get(i, set()) if d in keep} for i in keep}

    # initial done set is empty
    start: Set[int] = set()
    nodes: Dict[str, Dict] = {}
    edges: List[Dict] = []

    sid0 = _state_id(start)
    nodes[sid0] = {"id": sid0, "done": [], "k": 0}

    frontier: List[Set[int]] = [set()]
    seen: Set[str] = {sid0}

    while frontier and len(nodes) < max_nodes:
        done = frontier.pop(0)
        sid = _state_id(done)

        # available = events not done, deps satisfied
        avail = []
        for e in evs:
            if e.idx in done:
                continue
            if deps2.get(e.idx, set()).issubset(done):
                avail.append(e.idx)

        # branch on each available event
        for ei in avail:
            nd = set(done)
            nd.add(ei)
            nsid = _state_id(nd)
            if nsid not in nodes:
                nodes[nsid] = {"id": nsid, "done": sorted(nd), "k": len(nd)}
            edges.append({"src": sid, "dst": nsid, "add": ei})
            if nsid not in seen and len(nodes) < max_nodes:
                seen.add(nsid)
                frontier.append(nd)

    return {
        "sessionId": evs[0].sessionId if evs else "",
        "limit_events": limit_events,
        "max_nodes": max_nodes,
        "events": [e.__dict__ for e in evs],
        "deps": {str(k): sorted(list(v)) for k,v in deps2.items()},
        "nodes": list(nodes.values()),
        "edges": edges,
    }


def to_dot(g: Dict) -> str:
    evs = g.get('events') or []
    def ev_label(i: int) -> str:
        if i < 0 or i >= len(evs):
            return str(i)
        e = evs[i]
        actor = e.get('actor','?')
        txt = (e.get('text','') or '').replace('"','\\"')
        if len(txt) > 40:
            txt = txt[:37] + '...'
        return f"{actor}:{txt}"

    lines = ["digraph multiway_events {", "  rankdir=LR;", "  node [shape=box,fontname=Consolas,fontsize=10];"]

    for n in g.get('nodes', []):
        k = n.get('k', 0)
        # color by size of done-set
        shade = max(40, min(220, 40 + int(180 * (k / max(1, g.get('limit_events', 1))))))
        color = f"#{shade:02x}{(shade//2):02x}82"
        label = f"k={k}"
        lines.append(f"  {n['id']} [label=\"{label}\", style=filled, fillcolor=\"{color}\"]; ")

    for e in g.get('edges', []):
        add = e.get('add')
        lines.append(f"  {e['src']} -> {e['dst']} [label=\"+ {ev_label(add)}\"]; ")

    lines.append("}")
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--session', required=True, help='sessionId to extract (e.g., current UI sessionId)')
    ap.add_argument('--limit', type=int, default=12, help='max events to include (smaller => manageable)')
    ap.add_argument('--max-nodes', type=int, default=800)
    ap.add_argument('--no-actor-order', action='store_true', help='do not impose per-actor sequential ordering (more branching)')
    args = ap.parse_args()

    relay_dir = Path(__file__).resolve().parent / 'relay'
    evs = load_events(relay_dir, args.session)
    if not evs:
        raise SystemExit(f'No events found for sessionId={args.session!r} in {relay_dir}')

    deps = build_deps(evs, actor_order=(not args.no_actor_order))
    g = build_multiway(evs, deps, limit_events=args.limit, max_nodes=args.max_nodes)

    outdir = Path(__file__).resolve().parent / 'out'
    outdir.mkdir(parents=True, exist_ok=True)
    (outdir / 'openclaw_multiway_events.json').write_text(json.dumps(g, indent=2), encoding='utf-8')
    (outdir / 'openclaw_multiway_events.dot').write_text(to_dot(g), encoding='utf-8')

    print('wrote', outdir / 'openclaw_multiway_events.dot')
    print('wrote', outdir / 'openclaw_multiway_events.json')
    print('events', len(g['events']), 'nodes', len(g['nodes']), 'edges', len(g['edges']))


if __name__ == '__main__':
    main()
