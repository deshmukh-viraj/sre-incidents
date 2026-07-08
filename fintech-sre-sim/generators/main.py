"""
main.py — Fintech SRE Simulation Environment entry point
"""

import argparse
import uvicorn
import signal
import threading
from fastapi import FastAPI
import sys
import time
import os


from generators.metrics_generator import MetricsGenerator, start_metrics_server
from generators.log_generator import LogGenerator
from scenarios.scenario_engine import ScenarioEngine, ALL_SCENARIOS


engine = ScenarioEngine()
control_api = FastAPI()

@control_api.post("/control/resolve")
def resolve_scenario():
    engine.resolve()
    return {"status": "resolving"}

def start_control_api():
    """runs in background"""
    uvicorn.run(control_api, host="0.0.0.0", port=8001, log_level="error")

def main():

    api_thread = threading.Thread(target=start_control_api, daemon=True)
    api_thread.start()
    print("[control] simulator control API started on port:8001")


    parser = argparse.ArgumentParser(description="Fintech SRE Simulation Environment")
    parser.add_argument(
        "--mode",
        choices=["steady", "scenario", "training"],
        default="steady",
        help="steady=baseline only; scenario=inject specific scenario; training=all scenarios in sequence",
    )
    parser.add_argument(
        "--scenario",
        choices=list(ALL_SCENARIOS.keys()),
        default="payment_latency_spike",
        help="Scenario to inject (used with --mode scenario)",
    )
    parser.add_argument("--metrics-port", type=int, default=8000)
    parser.add_argument("--log-file", type=str, default=None, help="Write logs to file (default: stdout)")
    parser.add_argument(
        "--duration-minutes",
        type=float,
        default=None,
        help="Stop after N minutes (default: 15 when --log-file is set; use 0 for no limit)",
    )
    parser.add_argument("--seed", type=int, default=None, help="Random seed for deterministic replay")
    args = parser.parse_args()

    duration_minutes = args.duration_minutes
    if duration_minutes is None and args.log_file:
        duration_minutes = 15.0
    elif duration_minutes is not None and duration_minutes <= 0:
        duration_minutes = None

    if args.seed is not None:
        import random
        random.seed(args.seed)
        print(f"[main] Using random seed: {args.seed}")

    start_metrics_server(args.metrics_port)

    metrics_gen = MetricsGenerator(tick_interval=0.1)

    if args.log_file and os.path.exists(args.log_file):
        os.remove(args.log_file)

    log_gen = LogGenerator(output_file=args.log_file, tick_interval=0.5)

    metrics_gen.start()
    log_gen.start()

    deadline = None
    if duration_minutes is not None:
        deadline = time.time() + duration_minutes * 60

    print(f"[main] Simulation running — mode: {args.mode}")
    print(f"[main] Metrics: http://localhost:{args.metrics_port}/metrics")
    print(f"[main] Logs: {'stdout' if not args.log_file else args.log_file}")
    if deadline:
        print(f"[main] Will stop automatically after {duration_minutes} minutes")

    def on_phase(scenario_name, phase_name):
        print(f"[main] Phase change -> {scenario_name}:{phase_name}")

    def shutdown(sig=None, frame=None):
        print("\n[main] Shutting down...")
        engine.stop()
        metrics_gen.stop()
        log_gen.stop()
        sys.exit(0)

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    def timed_out() -> bool:
        return deadline is not None and time.time() >= deadline

    def wait_until_deadline():
        while not timed_out():
            time.sleep(1)
        if timed_out():
            print("[main] Duration reached -> stopping.")
            shutdown()

    if args.mode == "steady":
        if deadline:
            print(f"[main] Steady-state mode ({duration_minutes} min). Ctrl+C to stop early.")
        else:
            print("[main] Running in steady-state baseline mode. Ctrl+C to stop.")
        wait_until_deadline()

    elif args.mode == "scenario":
        print(f"[main] Injecting scenario: {args.scenario}.")
    
        if not timed_out():
            engine.run_scenario(args.scenario, on_phase_change=on_phase)
        print("[main] Scenario complete. Continuing steady state.")
        wait_until_deadline()
    elif args.mode == "training":
        print("[main] Starting full training sequence...")
        if not timed_out():
            engine.run_training_sequence(interval_between=30.0)
        print("[main] Training sequence complete.")
        if timed_out():
            shutdown()


if __name__ == "__main__":
    main()
