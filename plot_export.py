import csv, pathlib, sys
import matplotlib.pyplot as plt

csv_path = sys.argv[1]
t=[]; rms=[]; erms=[]; g=[]
with open(csv_path, newline="", encoding="utf-8") as f:
    r = csv.DictReader(f)
    for row in r:
        t.append(int(float(row["t"])))
        rms.append(float(row["rms"]))
        erms.append(float(row["ema_rms"]) if row["ema_rms"] not in ("", "None") else float("nan"))
        g.append(float(row["gamma"]))

fig, ax = plt.subplots(2, 1, sharex=True, figsize=(10, 6))
ax[0].plot(t, rms, label="RMS")
ax[0].plot(t, erms, label="eRMS")
ax[0].legend(); ax[0].grid(True)
ax[1].plot(t, g, label="gamma")
ax[1].legend(); ax[1].grid(True)

plt.tight_layout()
out = pathlib.Path(csv_path).with_suffix(".png")
plt.savefig(out, dpi=150)
print("Wrote", out)