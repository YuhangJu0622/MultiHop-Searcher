"""
Concurrent answer generation script for Research Agent.
Runs questions from a JSONL data file and writes answers to a results JSONL file.

Usage:
    python -m agent.scripts.run_agent --data agent/data/data_stage_1.jsonl
    python -m agent.scripts.run_agent --data agent/data/data_stage_1.jsonl --start 0 --end 50 --concurrency 3
"""
import argparse
import asyncio
import json
import os
import time
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parents[2] / ".env")

from agent.agent_loop import react_agent
from agent.logger_config import current_qid, setup_eval_logging


async def evaluate_single(qid: int, question: str) -> dict:
    """Run the agent on a single question, returning a result dict."""
    current_qid.set(qid)
    start = time.time()
    try:
        answer = await react_agent(question)
        elapsed = time.time() - start
        return {"id": qid, "answer": answer, "elapsed": round(elapsed, 1), "error": None}
    except Exception as e:
        elapsed = time.time() - start
        return {"id": qid, "answer": "", "elapsed": round(elapsed, 1), "error": str(e)}


async def main():
    parser = argparse.ArgumentParser(description="Run Research Agent on evaluation data")
    parser.add_argument("--data", required=True, help="Path to input JSONL (must have 'id' and 'question' fields)")
    parser.add_argument("--output", default=str(Path(__file__).resolve().parents[1] / "results.jsonl"), help="Path to output results JSONL")
    parser.add_argument("--start", type=int, default=0, help="Start question ID (inclusive)")
    parser.add_argument("--end", type=int, default=100, help="End question ID (exclusive)")
    parser.add_argument("--concurrency", type=int, default=1, help="Max concurrent questions")
    args = parser.parse_args()

    # Setup per-question log files
    agent_dir = Path(__file__).resolve().parents[1]
    log_dir = str(agent_dir / "logs" / datetime.now().strftime("%Y%m%d_%H%M%S"))
    per_q_handler = setup_eval_logging(log_dir)
    print(f"Logs directory: {log_dir}")

    # Load questions
    data = {}
    with open(args.data, "r", encoding="utf-8") as f:
        for line in f:
            item = json.loads(line)
            data[item["id"]] = item

    # Load existing results to support resume
    done_ids = set()
    if os.path.exists(args.output):
        with open(args.output, "r", encoding="utf-8") as f:
            for line in f:
                try:
                    done_ids.add(json.loads(line)["id"])
                except Exception:
                    pass

    ids_to_process = [i for i in range(args.start, args.end) if i in data and i not in done_ids]
    skipped = len([i for i in range(args.start, args.end) if i in data and i in done_ids])

    print(f"Processing {len(ids_to_process)} questions (IDs {args.start}-{args.end - 1})")
    if skipped:
        print(f"Skipping {skipped} already completed")

    if not ids_to_process:
        print("No new questions to process.")
        per_q_handler.close()
        return

    sem = asyncio.Semaphore(args.concurrency)
    write_lock = asyncio.Lock()
    completed = 0
    total = len(ids_to_process)

    async def run_one(qid: int) -> dict:
        nonlocal completed
        async with sem:
            result = await evaluate_single(qid, data[qid]["question"])
        async with write_lock:
            with open(args.output, "a", encoding="utf-8") as f:
                f.write(json.dumps(result, ensure_ascii=False) + "\n")
            completed += 1
            status = f"ERROR: {result['error']}" if result["error"] else result["answer"]
            print(f"[{completed}/{total}] Q{qid} ({result['elapsed']}s) -> {status}")
        return result

    tasks = [asyncio.create_task(run_one(qid)) for qid in ids_to_process]
    results = await asyncio.gather(*tasks)

    # Summary
    errors = sum(1 for r in results if r["error"])
    avg_elapsed = sum(r["elapsed"] for r in results) / len(results) if results else 0
    print(f"\nDone: {len(results)} questions, {errors} errors, avg {avg_elapsed:.1f}s per question")
    print(f"Results saved to: {args.output}")
    print(f"Logs saved to: {log_dir}/")

    per_q_handler.close()


if __name__ == "__main__":
    asyncio.run(main())
