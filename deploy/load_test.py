"""
ConvexPi load test — simulates N concurrent students submitting strategies.

Usage:
    # Against local dev server (default)
    python deploy/load_test.py

    # Against a deployed instance
    BASE_URL=https://your-app.vercel.app JWT_TOKEN=eyJ... python deploy/load_test.py

    # Custom concurrency / think time
    python deploy/load_test.py --students 30 --think-time 2.0 --duration 60

Requirements:
    pip install httpx

Environment variables:
    BASE_URL    — web server base URL (default: http://localhost:3001)
    JWT_TOKEN   — Supabase access token for a test user (if not set, uses STUDENT_CREDS)
    COHORT_ID   — cohort UUID to submit to (required)

Metrics reported:
    - p50 / p95 / p99 submission latency
    - throughput (submissions/second accepted)
    - error rate and breakdown by status code
    - queue depth sampled from /api/admin/queue (if ADMIN_JWT set)
"""

from __future__ import annotations
import asyncio
import argparse
import json
import os
import random
import statistics
import time
from dataclasses import dataclass, field
from datetime import datetime

try:
    import httpx
except ImportError:
    raise SystemExit("Install httpx first:  pip install httpx")


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

BASE_URL  = os.environ.get("BASE_URL",  "http://localhost:3001")
JWT_TOKEN = os.environ.get("JWT_TOKEN", "")
COHORT_ID = os.environ.get("COHORT_ID", "")
ADMIN_JWT = os.environ.get("ADMIN_JWT", "")


# Minimal valid strategies with slight variations so deduplication doesn't block
def make_strategy(student_id: int) -> str:
    return f"""import numpy as np

class MyStrategy:
    # Student {student_id}
    FEATURE_IDX = {student_id % 5}

    def predict(self, features):
        raw = features[:, self.FEATURE_IDX]
        return (raw - raw.mean()) / (raw.std() + 1e-8)
"""


# ---------------------------------------------------------------------------
# Result tracking
# ---------------------------------------------------------------------------

@dataclass
class Result:
    student_id:  int
    status_code: int
    latency_ms:  float
    error:       str = ""
    timestamp:   float = field(default_factory=time.monotonic)


@dataclass
class Stats:
    results:     list[Result] = field(default_factory=list)
    queue_depths: list[dict]  = field(default_factory=list)

    def add(self, r: Result) -> None:
        self.results.append(r)

    def report(self) -> None:
        if not self.results:
            print("No results collected.")
            return

        total      = len(self.results)
        successes  = [r for r in self.results if r.status_code == 200]
        errors     = [r for r in self.results if r.status_code != 200]
        latencies  = sorted(r.latency_ms for r in self.results)

        def pct(lst, p):
            if not lst: return 0.0
            idx = max(0, int(len(lst) * p / 100) - 1)
            return lst[idx]

        elapsed = self.results[-1].timestamp - self.results[0].timestamp + 0.001
        throughput = len(successes) / elapsed

        print("\n" + "=" * 55)
        print("  ConvexPi Load Test Results")
        print("=" * 55)
        print(f"  Total requests   : {total}")
        print(f"  Successful (2xx) : {len(successes)}  ({100*len(successes)/total:.1f}%)")
        print(f"  Errors           : {len(errors)}  ({100*len(errors)/total:.1f}%)")
        print(f"  Throughput       : {throughput:.2f} req/s")
        print()
        print(f"  Latency (ms)")
        print(f"    p50  : {pct(latencies, 50):7.1f}")
        print(f"    p95  : {pct(latencies, 95):7.1f}")
        print(f"    p99  : {pct(latencies, 99):7.1f}")
        print(f"    max  : {max(latencies):7.1f}")
        print(f"    mean : {statistics.mean(latencies):7.1f}")

        if errors:
            print()
            print("  Error breakdown:")
            by_code: dict[int, int] = {}
            for r in errors:
                by_code[r.status_code] = by_code.get(r.status_code, 0) + 1
            for code, count in sorted(by_code.items()):
                print(f"    HTTP {code} : {count}")
            # Show first few error bodies
            for r in errors[:3]:
                if r.error:
                    print(f"    sample: {r.error[:120]}")

        if self.queue_depths:
            last = self.queue_depths[-1]
            print()
            print(f"  Final queue depth:")
            print(f"    pending  : {last.get('pending', '?')}")
            print(f"    running  : {last.get('running', '?')}")
            print(f"    failed   : {last.get('failed_today', '?')}")

        print("=" * 55)

        # Pass/fail thresholds
        p95 = pct(latencies, 95)
        error_rate = len(errors) / total
        ok = True
        if p95 > 3000:
            print(f"  ⚠ p95 latency {p95:.0f}ms exceeds 3000ms target")
            ok = False
        if error_rate > 0.05:
            print(f"  ⚠ error rate {error_rate:.1%} exceeds 5% target")
            ok = False
        if ok:
            print("  ✓ All thresholds met")
        print()


# ---------------------------------------------------------------------------
# Student simulation
# ---------------------------------------------------------------------------

async def student_loop(
    student_id: int,
    stats: Stats,
    cohort_id: str,
    jwt: str,
    n_submissions: int,
    think_time: float,
) -> None:
    headers = {
        "Authorization": f"Bearer {jwt}",
        "Content-Type": "application/json",
    }
    async with httpx.AsyncClient(base_url=BASE_URL, timeout=30.0) as client:
        for i in range(n_submissions):
            code = make_strategy(student_id * 100 + i)
            payload = {
                "cohortId":      cohort_id,
                "strategyName":  f"Student {student_id} v{i+1}",
                "code":          code,
            }
            t0 = time.monotonic()
            try:
                resp = await client.post("/api/submissions", json=payload, headers=headers)
                latency = (time.monotonic() - t0) * 1000
                body = resp.text[:200] if resp.status_code != 200 else ""
                stats.add(Result(student_id, resp.status_code, latency, body))
            except Exception as e:
                latency = (time.monotonic() - t0) * 1000
                stats.add(Result(student_id, 0, latency, str(e)[:200]))

            if i < n_submissions - 1:
                await asyncio.sleep(think_time + random.uniform(0, think_time * 0.5))


async def sample_queue(stats: Stats, interval: float, duration: float) -> None:
    if not ADMIN_JWT:
        return
    headers = {"Authorization": f"Bearer {ADMIN_JWT}"}
    async with httpx.AsyncClient(base_url=BASE_URL, timeout=10.0) as client:
        t_end = time.monotonic() + duration
        while time.monotonic() < t_end:
            try:
                resp = await client.get("/api/admin/queue", headers=headers)
                if resp.status_code == 200:
                    stats.queue_depths.append(resp.json())
            except Exception:
                pass
            await asyncio.sleep(interval)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def run_load_test(n_students: int, n_submissions: int,
                        think_time: float, cohort_id: str, jwt: str) -> Stats:
    stats = Stats()
    duration = n_submissions * (think_time * 1.5) + 10

    print(f"ConvexPi load test  {datetime.now().strftime('%H:%M:%S')}")
    print(f"  Target  : {BASE_URL}")
    print(f"  Students: {n_students}  |  Submissions each: {n_submissions}")
    print(f"  Think time: {think_time}s  |  Est. duration: {duration:.0f}s")
    print()

    tasks = [
        student_loop(i, stats, cohort_id, jwt, n_submissions, think_time)
        for i in range(n_students)
    ]
    tasks.append(sample_queue(stats, interval=5.0, duration=duration))

    await asyncio.gather(*tasks)
    return stats


def main() -> None:
    parser = argparse.ArgumentParser(description="ConvexPi load test")
    parser.add_argument("--students",     type=int,   default=30,  help="Concurrent students")
    parser.add_argument("--submissions",  type=int,   default=2,   help="Submissions per student")
    parser.add_argument("--think-time",   type=float, default=3.0, help="Seconds between submissions")
    args = parser.parse_args()

    cohort_id = COHORT_ID
    jwt       = JWT_TOKEN

    if not cohort_id:
        raise SystemExit(
            "Set COHORT_ID env var to a valid cohort UUID.\n"
            "Example:  COHORT_ID=abc-... JWT_TOKEN=eyJ... python deploy/load_test.py"
        )
    if not jwt:
        raise SystemExit(
            "Set JWT_TOKEN env var to a Supabase access token for a test user.\n"
            "Get one by signing in via the Supabase client and calling getSession()."
        )

    stats = asyncio.run(run_load_test(
        n_students=args.students,
        n_submissions=args.submissions,
        think_time=args.think_time,
        cohort_id=cohort_id,
        jwt=jwt,
    ))
    stats.report()


if __name__ == "__main__":
    main()
