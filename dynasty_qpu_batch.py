import math
import time
import argparse
import numpy as np

from qiskit import QuantumCircuit, transpile
from qiskit_ibm_runtime import QiskitRuntimeService, Batch, SamplerV2 as Sampler


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


def build_reservoir_circuit(u, q, p, S4, n=5, norm=True):
    """Build a 5q reservoir circuit. Uses measure_all for Sampler counts."""
    qc = QuantumCircuit(n)

    if norm:
        # Prevent large values from saturating angles.
        u_n = float(u)  # already bounded [-1,1]
        q_n = _tanh_norm(q, 1.0)
        p_n = _tanh_norm(p, 1.0)
        s4_n = _tanh_norm(S4, 1.0)
        x = np.array([u_n, q_n, p_n, s4_n], dtype=float)
    else:
        x = np.array([float(u), float(q), float(p), float(S4)], dtype=float)

    # Encode inputs
    for i in range(n):
        a = float(W_ENC[i] @ x)
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
# Features from counts
# -----------------------------

def z_expectations_from_counts(counts, n=5, assume_qiskit_order=True):
    """Return features: [<Z_i> for i] + [<Z_i Z_{i+1}> for i] for a bitstring distribution.

    assume_qiskit_order=True interprets bitstrings as q_{n-1}...q_0 (Qiskit print order).
    """
    shots = sum(counts.values())
    if shots <= 0:
        return np.zeros(2 * n, dtype=float)

    # Accumulators
    z = np.zeros(n, dtype=float)
    zz = np.zeros(n, dtype=float)

    for bitstr, c in counts.items():
        # Normalize key to length n
        s = bitstr.strip()
        if len(s) != n:
            s = s.zfill(n)

        # Map to bits per qubit index
        # If assume_qiskit_order, leftmost is qubit n-1.
        bits = [int(ch) for ch in s]
        if assume_qiskit_order:
            # qubit i corresponds to bits[n-1-i]
            bq = [bits[n - 1 - i] for i in range(n)]
        else:
            # qubit i corresponds to bits[i]
            bq = bits

        # Z eigenvalue: 0 -> +1, 1 -> -1
        zv = np.array([1.0 if b == 0 else -1.0 for b in bq], dtype=float)

        z += c * zv
        for i in range(n):
            zz[i] += c * (zv[i] * zv[(i + 1) % n])

    z /= shots
    zz /= shots
    return np.concatenate([z, zz]).astype(float)


# -----------------------------
# Batch runner
# -----------------------------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--backend", default="", help="Backend name (ibm_fez/ibm_torino/ibm_marrakesh). Default: least busy of available.")
    ap.add_argument("--shots", type=int, default=2048)
    ap.add_argument("--batch", type=int, default=20, help="Circuits per QPU batch job.")
    ap.add_argument("--batches", type=int, default=5, help="Number of batch jobs to run.")
    ap.add_argument("--delay", type=int, default=10, help="Target delay in steps (predict u(t-delay)).")
    ap.add_argument("--eta", type=float, default=0.05, help="LMS learning rate for readout.")
    ap.add_argument("--norm", action="store_true", help="Use tanh normalization for q,p,S4 before encoding.")
    ap.add_argument("--no-norm", dest="norm", action="store_false")
    ap.set_defaults(norm=True)
    args = ap.parse_args()

    svc = QiskitRuntimeService(channel="ibm_quantum_platform")

    if args.backend:
        backend = svc.backend(args.backend)
    else:
        # Choose least-busy from available backends
        names = [b.name for b in svc.backends()]
        candidates = [n for n in ["ibm_fez", "ibm_torino", "ibm_marrakesh"] if n in names]
        if not candidates:
            raise RuntimeError(f"No expected QPUs visible. Available: {names}")
        backends = [svc.backend(n) for n in candidates]

        def pending(b):
            try:
                s = b.status()
                return int(getattr(s, "pending_jobs", 999999))
            except Exception:
                return 999999

        backend = sorted(backends, key=pending)[0]

    print("Using backend:", backend.name)
    print(f"shots={args.shots} batch={args.batch} batches={args.batches} delay={args.delay} eta={args.eta} norm={args.norm}")

    core = DynastyCore()

    # Readout weights (10 features: 5 Z + 5 ZZ)
    w = np.zeros(10, dtype=float)
    u_hist = []

    global_step = 0
    for bi in range(args.batches):
        # Generate classical states for this batch
        states = []
        for _ in range(args.batch):
            u, q, p, S4 = core.step()
            u_hist.append(u)
            states.append((u, q, p, S4))
            global_step += 1

        # Build circuits
        circs = [build_reservoir_circuit(u, q, p, S4, norm=args.norm) for (u, q, p, S4) in states]

        # Transpile batch to backend ISA
        t0 = time.time()
        circs_t = transpile(circs, backend=backend, optimization_level=1)
        t_tr = time.time() - t0
        print(f"batch {bi+1}/{args.batches}: transpiled {len(circs_t)} circuits in {t_tr:.2f}s")

        # Submit as one Batch job (open plan compatible)
        with Batch(backend=backend) as batch:
            sampler = Sampler(mode=batch)
            job = sampler.run(circs_t, shots=args.shots)
            print("  Job ID:", job.job_id())
            result = job.result()

        # Train readout across batch
        mse_acc = 0.0
        mse_n = 0
        gammas = []
        for i in range(args.batch):
            meas = result[i].data.meas
            # Prefer get_counts(); fallback to quasi distribution if needed
            if hasattr(meas, "get_counts"):
                counts = meas.get_counts()
            else:
                # If BitArray-like without counts helper, raise for now
                raise RuntimeError(f"Unsupported measurement type: {type(meas)}")

            phi = z_expectations_from_counts(counts, n=5, assume_qiskit_order=True)
            y = float(w @ phi)

            k = (global_step - args.batch) + i  # absolute step index for this item
            if k >= args.delay:
                target = u_hist[k - args.delay]
                err = target - y
                w += args.eta * err * phi
                mse_acc += err * err
                mse_n += 1

        mse = (mse_acc / max(1, mse_n))
        print(f"  batch_mse={mse:.6f}  w_norm={float(np.linalg.norm(w)):.4f}  step={global_step}")

    print("done")


if __name__ == "__main__":
    main()
