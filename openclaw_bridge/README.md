# openclaw_bridge

Local allowlisted Skill RPC layer for Hybrid.

## Run
From this folder:
```bat
python -m pip install -r requirements.txt
python -m uvicorn bridge:app --host 127.0.0.1 --port 17171
```

Health:
- http://127.0.0.1:17171/health

## Skills
Defined in `skills_manifest.json`.

Dynasty skills (internal):
- `dynasty_status`
- `dynasty_step` (n=0 continuous)
- `dynasty_cancel`
- `dynasty_mark`
- `dynasty_preset`
