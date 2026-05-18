"""
main.py — Fintech SRE Simulation Environment entry point
"""

import argparse
import signal
import sys
import time
import os


from generators.metrics_generator import MetricsGenerator, start_metrics_server
from generators.log_generator import LogGenerator
from scenarios.scenario_engine import ScenarioEngine, ALL_SCENARIOS


def main():
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
    parser.add_argument("--seed", type=int, default=None, help="Random seed for deterministic replay")
    args = parser.parse_args()

    if args.seed is not None:
        import random
        random.seed(args.seed)
        print(f"[main] Using random seed: {args.seed}")

    # Start metric exposition server
    start_metrics_server(args.metrics_port)

    # Start generators
    metrics_gen = MetricsGenerator(tick_interval=0.1)

    if args.log_file and os.path.exists(args.log_file):
        os.remove(args.log_file)

    log_gen = LogGenerator(output_file=args.log_file, tick_interval=0.5)
    scenario_engine = ScenarioEngine()

    metrics_gen.start()
    log_gen.start()

    print(f"[main] Simulation running — mode: {args.mode}")
    print(f"[main] Metrics: http://localhost:{args.metrics_port}/metrics")
    print(f"[main] Logs: {'stdout' if not args.log_file else args.log_file}")

    def on_phase(scenario_name, phase_name):
        print(f"[main] Phase change → {scenario_name}:{phase_name}")

    def shutdown(sig, frame):
        print("\n[main] Shutting down...")
        scenario_engine.stop()
        metrics_gen.stop()
        log_gen.stop()
        sys.exit(0)

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    if args.mode == "steady":
        print("[main] Running in steady-state baseline mode. Ctrl+C to stop.")
        while True:
            time.sleep(1)
    elif args.mode == "scenario":
        print(f"[main] Injecting scenario: {args.scenario} in 5s...")
        time.sleep(5)
        scenario_engine.run_scenario(args.scenario, on_phase_change=on_phase)
        print("[main] Scenario complete. Continuing steady state. Ctrl+C to stop.")
        while True:
            time.sleep(1)
    elif args.mode == "training":
        print("[main] Starting full training sequence...")
        scenario_engine.run_training_sequence(interval_between=30.0)
        print("[main] Training sequence complete.")


if __name__ == "__main__":
    main()