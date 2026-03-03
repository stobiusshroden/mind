import math
import numpy as np

from qiskit import QuantumCircuit
from qiskit.quantum_info import Statevector, Pauli

# -----------------------------
# Classical "Dynasty-ish" core
# -----------------------------
class DynastyCore:
    def __init__(self, dt=0.01, omega=0.35, beta=0.1, gamma=0.05):
        self.dt = dt
        self.omega = omega
        self.beta = beta
        self.gamma = gamma
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

        # v19-inspired update
        p = p + dt * (-(self.omega**2) * q - self.beta * (q**3) - self.gamma * p) + self.beta * u
        q = q + dt * p
        S4 = S4 + dt * (q * q - 0.1 * S4)

        self.t = t + dt
        self.q, self.p, self.S4 = q, p, S4
        return u, q, p, S4


# -----------------------------
# Quantum reservoir feature map
# -----------------------------
def build_reservoir_circuit(u, q, p, S4, n=5):
    qc = QuantumCircuit(n)

    def clip(x, m=2.0):
        x = float(x)
        return max(-m, min(m, x))

    x = np.array([u, q, p, S4], dtype=float)

    # Per-qubit encoding weights (starter)
    W = np.array(
        [
            [1.20, 0.60, 0.30, 0.15],
            [-0.70, 1.00, 0.20, 0.10],
            [0.40, -0.30, 1.10, 0.20],
            [0.20, 0.25, -0.60, 1.00],
            [-0.90, 0.10, 0.35, -0.50],
        ],
        dtype=float,
    )

    # Encode inputs via non-commuting rotations
    for i in range(n):
        a = clip(W[i] @ x)
        b = clip((W[(i + 1) % n] @ x) * 0.7)
        qc.ry(a, i)
        qc.rz(b, i)

    # Fixed reservoir mixing
    for i in range(n):
        qc.cx(i, (i + 1) % n)
    for i in range(n):
        qc.ry(0.35, i)
    for i in range(n):
        qc.cx((i + 2) % n, i)

    return qc


def features_from_statevector(qc):
    sv = Statevector.from_instruction(qc)

    feats = []

    # Singles: <Z_i>
    for i in range(5):
        z = ["I"] * 5
        z[4 - i] = "Z"  # qiskit Pauli labels are little-endian
        feats.append(float(np.real(sv.expectation_value(Pauli("".join(z))))))

    # Neighbor correlators: <Z_i Z_{i+1}>
    for i in range(5):
        z = ["I"] * 5
        z[4 - i] = "Z"
        z[4 - ((i + 1) % 5)] = "Z"
        feats.append(float(np.real(sv.expectation_value(Pauli("".join(z))))))

    return np.array(feats, dtype=float)


# -----------------------------
# Training loop (classical readout)
# -----------------------------
def run(steps=2000, delay=200, eta=0.01):
    core = DynastyCore()
    w = np.zeros(10, dtype=float)
    u_hist = []

    mse_acc = 0.0
    for k in range(steps):
        u, q, p, S4 = core.step()
        u_hist.append(u)

        qc = build_reservoir_circuit(u, q, p, S4)
        phi = features_from_statevector(qc)
        y = float(w @ phi)

        if k >= delay:
            target = u_hist[k - delay]
            err = target - y
            w += eta * err * phi
            mse_acc += err * err

        if (k + 1) % 200 == 0:
            denom = max(1, (k + 1 - delay))
            print(
                f"step={k+1:5d}  q={q:+.3f} p={p:+.3f} S4={S4:+.3f}  mse={mse_acc/denom:.6f}"
            )

    print("done")
    return w


if __name__ == "__main__":
    run()
