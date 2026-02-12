import shutil
import time
import traceback
from datetime import date, datetime
from pathlib import Path

LLM_NAME = "hosted_vllm/gpt-oss-120b"
LLM_USER = "hosted_vllm/gpt-oss-120b"

AGENTS = [
    "llm_agent",
    "mav_soft",
    "mav_hard",
    "map_static",
    "map_adaptive",
    "mas_2",
    "mas_3",
]

DOMAIN = "telecom"
NUM_RUNS = 3  # runs per agent
NUM_TRIALS = 1  # trials per task per run
MAX_RETRIES = 5  # max retries per run on crash
NUM_TASKS = None  # None to run on full dataset
MAX_STEPS = 200
MAX_ERRORS = 10
MAX_CONCURRENCY = 10
SEED = 300

# LLM args
LLM_ARGS_AGENT = {"temperature": 0.0}
LLM_ARGS_USER = {"temperature": 0.0}

# Submission defaults
SUBMISSION_ORG = "research"
SUBMISSION_EMAIL = "research@example.com"


def make_save_name(llm_name: str, agent: str, run_idx: int) -> str:
    """e.g. gpt-oss-120b_mav_soft_1_<timestamp>"""
    short_llm = llm_name.split("/")[-1]
    return f"{short_llm}_{agent}_{run_idx}_{datetime.now().strftime('%Y%m%d_%H%M%S')}"


def run_single(agent: str, save_name: str):
    """Run a single experiment. Raises on failure."""
    from tau2.data_model.simulation import RunConfig
    from tau2.run import run_domain

    config = RunConfig(
        domain=DOMAIN,
        agent=agent,
        llm_agent=LLM_NAME,
        llm_args_agent=LLM_ARGS_AGENT,
        user="user_simulator",
        llm_user=LLM_USER,
        llm_args_user=LLM_ARGS_USER,
        num_tasks=NUM_TASKS,
        num_trials=NUM_TRIALS,
        max_steps=MAX_STEPS,
        max_errors=MAX_ERRORS,
        max_concurrency=MAX_CONCURRENCY,
        seed=SEED,
        save_to=save_name,
        log_level="WARNING",
    )
    return run_domain(config)


def prepare_submission_auto(sim_path: Path, output_dir: Path):
    """
    Non-interactive submission preparation.
    Bypasses all prompts from prepare_submission.
    """
    from tau2.data_model.simulation import Results as TrajectoryResults
    from tau2.metrics.agent_metrics import compute_metrics
    from tau2.scripts.leaderboard.submission import (
        SUBMISSION_FILE_NAME,
        TRAJECTORY_FILES_DIR_NAME,
        ContactInfo,
        DomainResults,
        Methodology,
        Submission,
    )
    from tau2.scripts.leaderboard.submission import (
        Results as SubmResults,
    )

    def safe_pct(val):
        return round(val * 100, 2) if val is not None else None

    output_dir.mkdir(parents=True, exist_ok=True)
    traj_dir = output_dir / TRAJECTORY_FILES_DIR_NAME
    traj_dir.mkdir(exist_ok=True)

    # Copy trajectory
    dest = traj_dir / sim_path.name
    shutil.copy2(sim_path, dest)

    # Load & compute
    results = TrajectoryResults.load(dest)
    metrics = compute_metrics(results)
    domain = results.info.environment_info.domain_name

    domain_results = DomainResults(
        pass_1=safe_pct(metrics.pass_hat_ks.get(1)),
        pass_2=safe_pct(metrics.pass_hat_ks.get(2)),
        pass_3=safe_pct(metrics.pass_hat_ks.get(3)),
        pass_4=safe_pct(metrics.pass_hat_ks.get(4)),
        cost=metrics.avg_agent_cost,
    )

    sub = Submission(
        model_name=results.info.agent_info.llm or LLM_NAME,
        organization=SUBMISSION_ORG,
        submission_date=date.today(),
        contact_info=ContactInfo(email=SUBMISSION_EMAIL),
        results=SubmResults(**{domain: domain_results}),
        is_new=False,
        methodology=Methodology(
            evaluation_date=date.today(),
            user_simulator=results.info.user_info.llm,
            notes=f"agent={results.info.agent_info.implementation}",
        ),
    )

    sub_file = output_dir / SUBMISSION_FILE_NAME
    with open(sub_file, "w") as f:
        f.write(sub.model_dump_json(indent=2, exclude_none=True))

    return metrics


class Progress:
    def __init__(self, total: int):
        self.total = total
        self.done = 0
        self.failed = 0
        self.start = time.time()

    def tick(self, label: str, ok: bool):
        self.done += 1
        if not ok:
            self.failed += 1
        elapsed = time.time() - self.start
        bar_len = 30
        filled = int(bar_len * self.done / self.total)
        bar = "█" * filled + "░" * (bar_len - filled)
        status = "✓" if ok else "✗"
        print(
            f"\r  [{bar}] {self.done}/{self.total}  "
            f"{status} {label}  "
            f"({elapsed:.0f}s elapsed, {self.failed} failed)    ",
            end="\n",
            flush=True,
        )


def main():
    from tau2.utils.utils import DATA_DIR

    sim_dir = DATA_DIR / "simulations"
    sub_base = DATA_DIR / "submissions"

    # Build job list
    jobs = []
    for agent in AGENTS:
        for run_idx in range(1, NUM_RUNS + 1):
            save_name = make_save_name(LLM_NAME, agent, run_idx)
            jobs.append((agent, save_name, run_idx))

    total = len(jobs)
    print(f"\n  tau2 experiment runner")
    print(f"  LLM:    {LLM_NAME}")
    print(f"  Domain: {DOMAIN}")
    print(f"  Agents: {', '.join(AGENTS)}")
    print(f"  Runs:   {NUM_RUNS} per agent")
    print(f"  Total:  {total} experiments\n")

    progress = Progress(total)
    results_log = []

    for agent, save_name, run_idx in jobs:
        label = f"{agent} run {run_idx}"
        sim_path = sim_dir / f"{save_name}.json"
        ok = False

        for attempt in range(1, MAX_RETRIES + 1):
            try:
                results = run_single(agent, save_name)
                ok = True
                break
            except Exception as e:
                short_err = str(e)[:120]
                if attempt < MAX_RETRIES:
                    print(
                        f"    ⚠ {label} attempt {attempt}/{MAX_RETRIES} failed: {short_err}"
                    )
                    print(f"    ↻ retrying (resume from checkpoint)...")
                else:
                    print(
                        f"    ✗ {label} FAILED after {MAX_RETRIES} attempts: {short_err}"
                    )
                    traceback.print_exc(limit=3)

        # Prepare submission if run succeeded
        if ok and sim_path.exists():
            try:
                sub_dir = sub_base / save_name
                metrics = prepare_submission_auto(sim_path, sub_dir)
                results_log.append(
                    {
                        "agent": agent,
                        "run": run_idx,
                        "save_name": save_name,
                        "status": "ok",
                        "pass_1": metrics.pass_hat_ks.get(1),
                        "cost": metrics.avg_agent_cost,
                    }
                )
            except Exception as e:
                print(f"    ⚠ submission prep failed for {label}: {e}")
                results_log.append(
                    {
                        "agent": agent,
                        "run": run_idx,
                        "save_name": save_name,
                        "status": "submit_err",
                        "error": str(e)[:100],
                    }
                )
        else:
            results_log.append(
                {
                    "agent": agent,
                    "run": run_idx,
                    "save_name": save_name,
                    "status": "run_failed",
                }
            )

        progress.tick(label, ok)

    # Summary
    print(f"\n{'=' * 70}")
    print(f"  RESULTS SUMMARY")
    print(f"{'=' * 70}")
    print(f"  {'Agent':<20s} {'Run':>4s} {'Status':>12s} {'Pass^1':>8s} {'Cost':>8s}")
    print(f"  {'-' * 20} {'-' * 4} {'-' * 12} {'-' * 8} {'-' * 8}")
    for r in results_log:
        p1 = f"{r['pass_1'] * 100:.1f}%" if r.get("pass_1") is not None else "—"
        cost = f"${r['cost']:.4f}" if r.get("cost") is not None else "—"
        print(
            f"  {r['agent']:<20s} {r['run']:>4d} {r['status']:>12s} {p1:>8s} {cost:>8s}"
        )

    n_ok = sum(1 for r in results_log if r["status"] == "ok")
    print(f"\n  Done: {n_ok}/{total} succeeded\n")


if __name__ == "__main__":
    main()
