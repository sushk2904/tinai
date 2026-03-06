import os
import subprocess
import time
import json
import urllib.request
import urllib.error
import threading
import sys
from dotenv import load_dotenv

load_dotenv()

API_KEY = os.getenv("X_API_KEY_SECRET")
if not API_KEY:
    print("[ORCHESTRATOR] FATAL ERROR: X_API_KEY_SECRET not found in environment.")
    sys.exit(1)

def set_chaos(provider, mode):
    print(f"\n[ORCHESTRATOR] Chaos -> {provider}={mode}")
    req = urllib.request.Request(
        "http://localhost:8000/admin/chaos",
        method="POST",
        headers={"Content-Type": "application/json", "x-api-key": API_KEY},
        data=json.dumps({"provider": provider, "mode": mode}).encode("utf-8")
    )
    try:
        urllib.request.urlopen(req)
        print(f"[ORCHESTRATOR] Success: {provider} is now in {mode} mode.")
    except Exception as e:
        print(f"[ORCHESTRATOR] Error applying chaos: {e}")

def set_load_shedding(active):
    status = "ACTIVE" if active else "INACTIVE"
    print(f"\n[ORCHESTRATOR] Load Shedding -> {status}")
    req = urllib.request.Request(
        "http://localhost:8000/admin/load-shedding",
        method="POST",
        headers={"Content-Type": "application/json", "x-api-key": API_KEY},
        data=json.dumps({"active": active}).encode("utf-8")
    )
    try:
        urllib.request.urlopen(req)
        print(f"[ORCHESTRATOR] Load shedding updated to {status}.")
    except Exception as e:
        print(f"[ORCHESTRATOR] Error updating load shedding: {e}")

def run_k6():
    print("[ORCHESTRATOR] Launching 37-Minute Phoenix Test...")
    cmd = "docker run --rm -i --network tinai_tinai_net grafana/k6 run - < tests/k6/saturation.js"
    subprocess.run(cmd, shell=True, check=False) 
    print("\n[ORCHESTRATOR] 37-Minute k6 Test complete.")

def orchestrate():
    print("\n=======================================================")
    print(" STARTING 37-MINUTE MASTERCLASS TIMELINE")
    print("=======================================================\n")
    
    # PHASE 1: 0 to 10m (The Deceptive Calm)
    print("[PHASE 1] Healthy System. MAB optimizing Groq (10m)")
    time.sleep(600) 

    # PHASE 2: 10m to 15m (The Gray Failure)
    print("\n[PHASE 2] Injecting Gray Failure into Groq to force OpenRouter shift (5m)")
    set_chaos("groq", "slow")
    time.sleep(300) 

    # PHASE 3: 15m to 22m (The Hard Crash)
    print("\n[PHASE 3] Injecting Fatal Error into OpenRouter. Fallback incoming! (7m)")
    set_chaos("openrouter", "error")
    time.sleep(420) 

    # PHASE 4: 22m to 27m (The Meltdown & Shield)
    print("\n[PHASE 4] Fallback Timeout. Waiting 2m for peak stress...")
    set_chaos("fallback", "timeout")
    time.sleep(120) 
    
    print("[PHASE 4] SYSTEM CRITICAL. Activating 503 Load Shedding (3m)")
    set_load_shedding(True)
    time.sleep(180) 

    # PHASE 5: 27m to 37m (The Resurrection)
    print("\n[PHASE 5] Resurrection. Clearing blocks. Monitoring recovery (10m)")
    set_load_shedding(False)
    set_chaos("groq", "none")
    set_chaos("openrouter", "none")
    set_chaos("fallback", "none")
    time.sleep(600) 

    print("\n[ORCHESTRATOR] 37-Minute Timeline complete. System has risen from the ashes.")

if __name__ == "__main__":
    # Ensure a clean slate before starting
    set_chaos("groq", "none")
    set_chaos("openrouter", "none")
    set_chaos("fallback", "none")
    set_load_shedding(False)

    t_k6 = threading.Thread(target=run_k6)
    t_orch = threading.Thread(target=orchestrate)

    t_k6.start()
    t_orch.start()

    t_k6.join()
    t_orch.join()

    # Final Cleanup
    set_chaos("groq", "none")
    set_chaos("openrouter", "none")
    set_chaos("fallback", "none")
    set_load_shedding(False)
    print("[ORCHESTRATOR] All done! Go check that beautiful Streamlit Dashboard.")