import json
import os
from pathlib import Path

from .utils import now_mt
from .coloradosprings_legistar import parse_legistar
from .epc_agendasuite import parse_bocc
from .pueblo_civicclerk import parse_pueblo
from .trinidad_regular import parse_trinidad
from .alamosa_diligent import parse_alamosa
from .salida_civicclerk import parse_salida

def run():
    meetings = []
    try:
        meetings.extend(parse_legistar())
    except Exception as e:
        print("Legistar error:", e)
    try:
        meetings.extend(parse_bocc())
    except Exception as e:
        print("BOCC error:", e)
    try:
        meetings.extend(parse_pueblo())
    except Exception as e:
        print("Pueblo error:", e)
    try:
        meetings.extend(parse_trinidad())
    except Exception as e:
        print("Trinidad error:", e)
    try:
        meetings.extend(parse_alamosa())
    except Exception as e:
        print("Alamosa error:", e)
    try:
        meetings.extend(parse_salida())
    except Exception as e:
        print("Salida error:", e)

    out = {
        "last_checked_mt": now_mt().strftime("%Y-%m-%d %H:%M"),
        "meetings": meetings
    }

    
    repo_root = Path(__file__).resolve().parents[1]
    data_dir = repo_root / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    out_path = data_dir / "meetings.json"

    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(out, f, indent=2)

    print(f"Wrote {len(meetings)} meetings to {out_path}")

if __name__ == "__main__":
    run()
