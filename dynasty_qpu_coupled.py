import math
import time
import argparse
import json
import os
import numpy as np

from qiskit import QuantumCircuit, transpile
from qiskit_ibm_runtime import QiskitRuntimeService, Batch, SamplerV2 as Sampler
from datetime import datetime
from pathlib import Path


# -----------------------------
# Classical Dynasty-ish core
# -----------------------------
class DynastyCore:
    def __init__(self, dt=0.05, omega=0.35, beta=0.05, gamma=0.0):
        self.dt = float(dt)
        self.omega = float(omega)
        self.beta = float(beta)
        self.gamma = float(gamma)
        self.t = 0.0
        self.q = 0.0
        self.p = 0.0
        self.S4 = 0.0

    def u(self, t):
        return math.sin(self.omega * t)

    def step(self):
        dt = self.dt
        t = self.t
        q, p, S4 = self.q, self.p, self.S4
        u = self.u(t)

        p = p + dt * (-(self.omega**2) * q - self.beta * (q**3) - self.gamma * p) + self.beta * u
        q = q + dt * p
        S4 = S4 + dt * (q * q - 0.1 * S4)

        self.t = t + dt
        self.q, self.p, self.S4 = q, p, S4
        return u, q, p, S4


# -----------------------------
# Counts -> expectations
# -----------------------------
def zavg_from_counts(counts, n=5, assume_qiskit_order=True):
    """Return mean_i <Z_i> from counts."""
    shots = sum(counts.values())
    if shots <= 0:
        return 0.0

    zsum = np.zeros(n, dtype=float)
    for bitstr, c in counts.items():
        s = bitstr.strip()
        if len(s) != n:
            s = s.zfill(n)
        bits = [int(ch) for ch in s]
        if assume_qiskit_order:
            bq = [bits[n - 1 - i] for i in range(n)]
        else:
            bq = bits
        zv = np.array([1.0 if b == 0 else -1.0 for b in bq], dtype=float)
        zsum += c * zv

    z = zsum / shots
    return float(np.mean(z))


# -----------------------------
# Quantum reservoir circuit
# -----------------------------
W_ENC = np.array(
    [
        [1.20, 0.60, 0.30, 0.15],
        [-0.70, 1.00, 0.20, 0.10],
        [0.40, -0.30, 1.10, 0.20],
        [0.20, 0.25, -0.60, 1.00],
        [-0.90, 0.10, 0.35, -0.50],
    ],
    dtype=float,
)


def _tanh_norm(x, scale):
    return math.tanh(float(x) / float(scale))


def build_circuit(u, q, p, S4, delta=0.0, n=5, norm=True):
    qc = QuantumCircuit(n)

    if norm:
        u_n = float(u)
        q_n = _tanh_norm(q, 1.0)
        p_n = _tanh_norm(p, 1.0)
        s4_n = _tanh_norm(S4, 1.0)
        x = np.array([u_n, q_n, p_n, s4_n], dtype=float)
    else:
        x = np.array([float(u), float(q), float(p), float(S4)], dtype=float)

    # Encode + coupling modulation (delta)
    for i in range(n):
        a = float(W_ENC[i] @ x) + (delta if i == 0 else 0.0)
        b = float((W_ENC[(i + 1) % n] @ x) * 0.7)
        qc.ry(a, i)
        qc.rz(b, i)

    # Mix / entangle
    for i in range(n):
        qc.cx(i, (i + 1) % n)
    for i in range(n):
        qc.ry(0.35, i)

    qc.measure_all()
    return qc


# -----------------------------
# Morse decoding + coupling update
# -----------------------------

def decode_morse(series, s4_series, s4_lo, s4_hi, hi=0.6, lo=None):
    """Decode dot/dash stream from a scalar series, gated by S4 band.

    Default behavior:
    - dash if value > hi
    - dot if lo is set and value < lo
    - otherwise none

    Returns list of symbols in {'.','-'}.
    """
    symbols = []
    for v, s4 in zip(series, s4_series):
        if not (s4_lo <= s4 <= s4_hi):
            continue
        if lo is not None and v < lo:
            symbols.append('.')
        elif v > hi:
            symbols.append('-')
    return symbols


def update_delta(delta, symbols, step=0.15, decay=0.9, clamp=1.5):
    """Update coupling offset delta based on decoded symbols."""
    # decay toward 0
    delta *= decay
    for s in symbols:
        if s == '.':
            delta += step
        elif s == '-':
            delta -= step
    delta = max(-clamp, min(clamp, delta))
    return delta


def run_batch(svc, backend, circs, shots):
    circs_t = transpile(circs, backend=backend, optimization_level=1)
    with Batch(backend=backend) as batch:
        sampler = Sampler(mode=batch)
        job = sampler.run(circs_t, shots=shots)
        job_id = job.job_id()
        print("  Job ID:", job_id)
        result = job.result()

    obs_list = []
    for i in range(len(circs_t)):
        meas = result[i].data.meas
        counts = meas.get_counts() if hasattr(meas, "get_counts") else None
        if counts is None:
            raise RuntimeError(f"Unsupported measurement type: {type(meas)}")
        # Inline observables to avoid any scope/import issues
        shots = sum(counts.values())
        if shots <= 0:
            obs_list.append({"zmean": 0.0, "hw_mean": 0.0, "parity_mean": 0.0})
        else:
            zsum = np.zeros(5, dtype=float)
            hw_acc = 0.0
            parity_acc = 0.0
            for bitstr, c in counts.items():
                s = str(bitstr).strip().zfill(5)
                bits = [int(ch) for ch in s]
                bq = [bits[4 - i] for i in range(5)]
                zv = np.array([1.0 if b == 0 else -1.0 for b in bq], dtype=float)
                zsum += c * zv
                hw = float(sum(bq))
                hw_acc += c * hw
                parity_acc += c * (1.0 if (hw % 2 == 0) else -1.0)
            z = zsum / shots
            obs_list.append({
                "zmean": float(np.mean(z)),
                "hw_mean": float(hw_acc / shots),
                "parity_mean": float(parity_acc / shots),
            })

    # Convert to arrays
    zmean = np.array([o["zmean"] for o in obs_list], dtype=float)
    hw_mean = np.array([o["hw_mean"] for o in obs_list], dtype=float)
    parity_mean = np.array([o["parity_mean"] for o in obs_list], dtype=float)

    print(f"  zmean stats: mean={float(np.mean(zmean)):.4f} std={float(np.std(zmean)):.4f} min={float(np.min(zmean)):.4f} max={float(np.max(zmean)):.4f}")
    print(f"  hw_mean stats: mean={float(np.mean(hw_mean)):.4f} std={float(np.std(hw_mean)):.4f} min={float(np.min(hw_mean)):.4f} max={float(np.max(hw_mean)):.4f}")
    print(f"  parity stats: mean={float(np.mean(parity_mean)):.4f} std={float(np.std(parity_mean)):.4f} min={float(np.min(parity_mean)):.4f} max={float(np.max(parity_mean)):.4f}")

    return {
        "job_id": job_id,
        "zmean": zmean,
        "hw_mean": hw_mean,
        "parity_mean": parity_mean,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--backendA", default="", help="QPU A name (e.g. ibm_fez)")
    ap.add_argument("--backendB", default="", help="QPU B name (e.g. ibm_torino)")
    ap.add_argument("--shots", type=int, default=1024)
    ap.add_argument("--epoch", type=int, default=10, help="Circuits per epoch")
    ap.add_argument("--epochs", type=int, default=2, help="Number of epochs")
    ap.add_argument("--s4lo", type=float, default=0.85, help="S4 thin-spot low")
    ap.add_argument("--s4hi", type=float, default=0.93, help="S4 thin-spot high")
    ap.add_argument("--mhigh", type=float, default=0.60, help="Threshold for dash (applies to selected observable)")
    ap.add_argument("--observable", default="zmean", choices=["zmean","hw_mean","parity_mean"], help="Which observable drives morse decoding and coupling score")
    ap.add_argument("--save-manifest", action="store_true", help="Write a JSON manifest with job IDs, settings, and observables")
    ap.add_argument("--delta0", type=float, default=0.0)
    ap.add_argument("--deltaStep", type=float, default=0.15)
    ap.add_argument("--deltaDecay", type=float, default=0.90)
    ap.add_argument("--control", action="store_true", help="Also run a control B batch with delta=0 for comparison.")
    ap.add_argument("--norm", action="store_true")
    ap.add_argument("--no-norm", dest="norm", action="store_false")
    ap.set_defaults(norm=True)
    args = ap.parse_args()

    svc = QiskitRuntimeService(channel="ibm_quantum_platform")
    names = [b.name for b in svc.backends()]

    def pick(name_hint):
        if name_hint:
            return svc.backend(name_hint)
        # pick least-busy among visible
        candidates = [n for n in ["ibm_fez", "ibm_torino", "ibm_marrakesh"] if n in names]
        backs = [svc.backend(n) for n in candidates]

        def pending(b):
            try:
                s = b.status()
                return int(getattr(s, "pending_jobs", 999999))
            except Exception:
                return 999999

        return sorted(backs, key=pending)[0]

    backendA = pick(args.backendA)
    backendB = pick(args.backendB)
    if backendB.name == backendA.name:
        # choose a different B if possible
        alt = [n for n in names if n != backendA.name]
        if alt:
            backendB = svc.backend(alt[0])

    print("Backend A:", backendA.name)
    print("Backend B:", backendB.name)
    print(f"shots={args.shots} epoch={args.epoch} epochs={args.epochs} S4_band=[{args.s4lo},{args.s4hi}] m_high={args.mhigh}")

    core = DynastyCore()
    delta = float(args.delta0)

    manifest = {
        "ts": time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
        "backendA": backendA.name,
        "backendB": backendB.name,
        "shots": args.shots,
        "epoch": args.epoch,
        "epochs": args.epochs,
        "s4_band": [args.s4lo, args.s4hi],
        "observable": args.observable,
        "m_high": args.mhigh,
        "delta0": args.delta0,
        "deltaStep": args.deltaStep,
        "deltaDecay": args.deltaDecay,
        "control": bool(args.control),
        "epochs_log": [],
    }

    for ei in range(args.epochs):
        # Generate same classical states for both A and B in this epoch
        states = [core.step() for _ in range(args.epoch)]
        u_list = [s[0] for s in states]
        q_list = [s[1] for s in states]
        p_list = [s[2] for s in states]
        s4_list = [s[3] for s in states]

        print(f"\n=== epoch {ei+1}/{args.epochs} ===")
        print(f"delta(B)={delta:+.3f}")

        manifest_epoch = {
            "epoch": ei + 1,
            "delta_in": delta,
            "backendA": backendA.name,
            "backendB": backendB.name,
            "jobA": None,
            "jobB0": None,
            "jobB": None,
            "decoded": None,
            "delta_out": None,
        }

        circsA = [build_circuit(u, q, p, s4, delta=0.0, norm=args.norm) for (u, q, p, s4) in zip(u_list, q_list, p_list, s4_list)]
        circsB = [build_circuit(u, q, p, s4, delta=delta, norm=args.norm) for (u, q, p, s4) in zip(u_list, q_list, p_list, s4_list)]

        print("Running A...")
        outA = run_batch(svc, backendA, circsA, args.shots)
        manifest_epoch["jobA"] = outA.get("job_id")
        seriesA = outA[args.observable]
        print(f"  A[{args.observable}]:", np.array2string(seriesA, precision=3, suppress_small=True))

        symbols = decode_morse(seriesA, s4_list, args.s4lo, args.s4hi, hi=args.mhigh, lo=None)
        decoded = ("".join(symbols) if symbols else "")
        manifest_epoch["decoded"] = decoded
        print("  decoded symbols:", decoded if decoded else "<none>")

        # Update delta for next epoch based on decoded morse
        delta = update_delta(delta, symbols, step=args.deltaStep, decay=args.deltaDecay)
        manifest_epoch["delta_out"] = delta
        print(f"  updated delta(B) -> {delta:+.3f}")

        zmB0 = None
        seriesB0 = None

        if args.control:
            print("Running B (control, delta=0)...")
            circsB0 = [build_circuit(u, q, p, s4, delta=0.0, norm=args.norm) for (u, q, p, s4) in zip(u_list, q_list, p_list, s4_list)]
            outB0 = run_batch(svc, backendB, circsB0, args.shots)
            manifest_epoch["jobB0"] = outB0.get("job_id")
            seriesB0 = outB0[args.observable]
            print(f"  B0[{args.observable}]:", np.array2string(seriesB0, precision=3, suppress_small=True))

            if np.std(seriesA) > 1e-9 and np.std(seriesB0) > 1e-9:
                corr0 = float(np.corrcoef(seriesA, seriesB0)[0, 1])
            else:
                corr0 = float('nan')
            print(f"  control_score(corr A,B0)={corr0}")

        print("Running B (coupled)...")
        outB = run_batch(svc, backendB, circsB, args.shots)
        manifest_epoch["jobB"] = outB.get("job_id")
        seriesB = outB[args.observable]
        print(f"  B[{args.observable}]:", np.array2string(seriesB, precision=3, suppress_small=True))

        if np.std(seriesA) > 1e-9 and np.std(seriesB) > 1e-9:
            corr = float(np.corrcoef(seriesA, seriesB)[0, 1])
        else:
            corr = float('nan')
        print(f"  coupling_score(corr A,B)={corr}")

        if args.control and seriesB0 is not None:
            dmean = float(np.mean(seriesB) - np.mean(seriesB0))
            dstd = float(np.std(seriesB) - np.std(seriesB0))
            print(f"  delta_effect: dmean(B-B0)={dmean:+.4f} dstd={dstd:+.4f}")

    print("\ndone")

    if getattr(args, 'save_manifest', False):
        try:
            _write_manifest(manifest)
        except Exception as e:
            print('MANIFEST_WRITE_ERROR:', repr(e))






def _write_manifest(manifest: dict, out_dir: str = None) -> str:
    """Write manifest JSON to Desktop\mind with a stable name; return path."""
    import json
    base_dir = Path(__file__).resolve().parent
    if out_dir:
        base_dir = Path(out_dir).resolve()
    base_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.utcnow().strftime('%Y%m%d_%H%M%S')
    out_path = base_dir / f'coupled_manifest_{ts}.json'
    out_path.write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding='utf-8')
    print(f'WROTE_MANIFEST: {out_path}')
    return str(out_path)


if __name__ == "__main__":
    main()

