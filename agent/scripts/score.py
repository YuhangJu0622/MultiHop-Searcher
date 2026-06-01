"""
Offline scoring script: compare agent results against reference answers.

Usage:
    python agent/scripts/score.py --results agent/results.jsonl --ref agent/data/data_stage_1.jsonl
    python agent/scripts/score.py --results agent/results.jsonl --ref agent/data/data_stage_2.jsonl
"""
import argparse
import json


def normalize_answer(answer: str) -> str:
    """Normalize answer for comparison: lowercase, strip, convert integer floats."""
    answer = answer.strip().lower()
    try:
        num = float(answer)
        if num == int(num):
            answer = str(int(num))
    except (ValueError, OverflowError):
        pass
    return answer


def main():
    parser = argparse.ArgumentParser(description="Score agent results against reference answers")
    parser.add_argument("--results", required=True, help="Path to results JSONL (must have 'id' and 'answer')")
    parser.add_argument("--ref", required=True, help="Path to reference JSONL (must have 'id' and 'answer')")
    args = parser.parse_args()

    # Load reference answers
    ref = {}
    with open(args.ref, "r", encoding="utf-8") as f:
        for line in f:
            item = json.loads(line)
            if "answer" in item:
                ref[item["id"]] = item["answer"]

    if not ref:
        print(f"No reference answers found in {args.ref}")
        return

    # Load results
    results = {}
    with open(args.results, "r", encoding="utf-8") as f:
        for line in f:
            try:
                item = json.loads(line)
                results[item["id"]] = item
            except Exception:
                pass

    if not results:
        print(f"No results found in {args.results}")
        return

    # Score
    correct = 0
    total = 0
    wrong = []
    errors = []
    elapsed_list = []

    for qid in sorted(results.keys()):
        r = results[qid]
        if qid not in ref:
            continue

        total += 1
        predicted = normalize_answer(r["answer"])
        expected = normalize_answer(ref[qid])
        elapsed = r.get("elapsed", 0)
        elapsed_list.append(elapsed)

        if r.get("error"):
            errors.append({"id": qid, "error": r["error"]})

        if predicted == expected:
            correct += 1
        else:
            wrong.append({"id": qid, "predicted": r["answer"], "expected": ref[qid]})

    # Print report
    print(f"{'=' * 60}")
    print(f"Results: {args.results}")
    print(f"Reference: {args.ref}")
    print(f"{'=' * 60}")

    if total > 0:
        print(f"\nAccuracy: {correct}/{total} = {correct / total:.2%}")
    else:
        print("\nNo matched questions to score.")
        return

    if elapsed_list:
        avg = sum(elapsed_list) / len(elapsed_list)
        print(f"Avg time: {avg:.1f}s  |  Total time: {sum(elapsed_list):.0f}s")

    if wrong:
        print(f"\n--- Wrong answers ({len(wrong)}) ---")
        for w in wrong:
            print(f"  Q{w['id']}: predicted={w['predicted']!r}  expected={w['expected']!r}")

    if errors:
        print(f"\n--- Errors ({len(errors)}) ---")
        for e in errors:
            print(f"  Q{e['id']}: {e['error']}")


if __name__ == "__main__":
    main()
