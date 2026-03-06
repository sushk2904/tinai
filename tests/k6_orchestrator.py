import subprocess
import time
import json
import urllib.request
import urllib.error
import threading
import sys

API_KEY = "5Yp3dNkVwY47Qq8BCsmv1KlNep7VguL0Qs375l4a1QE"

def set_chaos(provider, mode):
    print(f"\n[ORCHESTRATOR] Submitting Chaos -> provider={provider}, mode={mode}")
    req = urllib.request.Request(
        "http://localhost:8000/admin/chaos",
        method="POST",
        headers={"Content-Type": "application/json", "x-api-key": API_KEY},
        data=json.dumps({"provider": provider, "mode": mode}).encode("utf-8")
    )
    try:
        urllib.request.urlopen(req)
        print("[ORCHESTRATOR] Chaos applied successfully.")
    except Exception as e:
        print(f"[ORCHESTRATOR] Failed to apply Chaos: {e}")

def set_load_shedding(active):
    print(f"\n[ORCHESTRATOR] Submitting Load Shedding -> active={active}")
    req = urllib.request.Request(
        "http://localhost:8000/admin/load-shedding",
        method="POST",
        headers={"Content-Type": "application/json", "x-api-key": API_KEY},
        data=json.dumps({"active": active}).encode("utf-8")
    )
    try:
        urllib.request.urlopen(req)
        print("[ORCHESTRATOR] Load shedding applied successfully.")
    except Exception as e:
        print(f"[ORCHESTRATOR] Failed to apply Load Shedding: {e}")

def run_k6():
    print("[ORCHESTRATOR] Starting k6 load test (10m duration)...")
    cmd = "cmd.exe /c \"docker run --rm -i --network tinai_tinai_net grafana/k6 run - < tests/k6/primer.js\""
    subprocess.run(cmd, shell=True)
    print("\n[ORCHESTRATOR] k6 load test finished.")

def orchestrate():
    # Minute 1.5 (90s): Gray failure on groq
    print("[ORCHESTRATOR] Waiting 90s for Gray Failure injection (Groq: slow)")
    time.sleep(90)
    set_chaos("groq", "slow")

    # Minute 3 (180s total, wait 90s more): Outage on OpenRouter
    print("[ORCHESTRATOR] Waiting 90s for Outage injection (OpenRouter: error)")
    time.sleep(90)
    set_chaos("openrouter", "error")

    # Minute 6 (360s total, wait 180s more): Outage on fallback
    print("[ORCHESTRATOR] Waiting 180s for Outage injection (Fallback: timeout)")
    time.sleep(180)
    set_chaos("fallback", "timeout")

    # Minute 8 (480s total, wait 120s more): DDoS load shedding
    print("[ORCHESTRATOR] Waiting 120s for DDoS simulation (Load Shedding: active)")
    time.sleep(120)
    set_load_shedding(True)

    # Minute 8:30 (510s total, wait 30s more): Recover load shedding
    print("[ORCHESTRATOR] Waiting 30s for Recovery (Load Shedding: inactive)")
    time.sleep(30)
    set_load_shedding(False)

    print("[ORCHESTRATOR] Timeline events concluded. Awaiting k6 finish at minute 10.")

if __name__ == "__main__":
    t_k6 = threading.Thread(target=run_k6)
    t_orch = threading.Thread(target=orchestrate)

    t_k6.start()
    t_orch.start()

    t_k6.join()
    t_orch.join()

    # Clean up chaos
    set_chaos("groq", "none")
    set_chaos("openrouter", "none")
    set_chaos("fallback", "none")
    print("[ORCHESTRATOR] All done!")
