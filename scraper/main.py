import os, json, pytz
from datetime import datetime
from utils import now_mt
from coloradosprings_legistar import parse_legistar
from epc_agendasuite import parse_bocc
from pueblo_civicclerk import parse_pueblo
from trinidad_regular import parse_trinidad
from alamosa_diligent import parse_alamosa
from salida_civicclerk import parse_salida

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
    # write to ../data
    os.makedirs("../data", exist_ok=True)
    with open("../data/meetings.json", "w", encoding="utf-8") as f:
        json.dump(out, f, indent=2)
    print(f"Wrote {len(meetings)} meetings to ../data/meetings.json")

if __name__ == "__main__":
    run()
