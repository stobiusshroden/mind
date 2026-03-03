import numpy as np
from qiskit import QuantumCircuit, transpile
from qiskit_ibm_runtime import QiskitRuntimeService, Batch, SamplerV2 as Sampler


def build_circuit(u, q, p, S4, n=5):
    qc = QuantumCircuit(n)
    x = np.array([u, q, p, S4], dtype=float)

    # Simple fixed encoder (starter)
    W = np.array([
        [ 1.20,  0.60,  0.30,  0.15],
        [-0.70,  1.00,  0.20,  0.10],
        [ 0.40, -0.30,  1.10,  0.20],
        [ 0.20,  0.25, -0.60,  1.00],
        [-0.90,  0.10,  0.35, -0.50],
    ], dtype=float)

    # Encode u,q,p,S4 into rotations
    for i in range(n):
        a = float(W[i] @ x)
        b = float((W[(i + 1) % n] @ x) * 0.7)
        qc.ry(a, i)
        qc.rz(b, i)

    # Mix / entangle
    for i in range(n):
        qc.cx(i, (i + 1) % n)
    for i in range(n):
        qc.ry(0.35, i)

    qc.measure_all()
    return qc


if __name__ == "__main__":
    svc = QiskitRuntimeService(channel="ibm_quantum_platform")

    # Choose least-busy from your available QPUs
    candidates = ["ibm_fez", "ibm_torino", "ibm_marrakesh"]
    backends = [svc.backend(name) for name in candidates]

    # status() includes pending_jobs when available; fallback to name order
    def pending(b):
        try:
            s = b.status()
            return int(getattr(s, "pending_jobs", 999999))
        except Exception:
            return 999999

    backend = sorted(backends, key=pending)[0]
    print("Using backend:", backend.name)

    # Example state roughly in your current regime
    qc = build_circuit(u=0.1, q=-0.11, p=0.005, S4=0.88)

    # Transpile to match backend ISA (required by primitives)
    qc_t = transpile(qc, backend=backend, optimization_level=1)

    # Open plan does not allow Sessions; use Batch execution mode.
    with Batch(backend=backend) as batch:
        sampler = Sampler(mode=batch)
        job = sampler.run([qc_t], shots=2048)
        print("Job ID:", job.job_id())
        result = job.result()

    # SamplerV2 returns BitArray; use get_counts() if available
    meas = result[0].data.meas
    try:
        counts = meas.get_counts()
        top = sorted(counts.items(), key=lambda kv: kv[1], reverse=True)[:10]
        print("counts (top 10):", top)
    except Exception as e:
        # Fallback: print raw object summary
        print("meas:", meas)
        print("(could not convert to counts)", repr(e))
