"""One synthetic negative control for the LIVE reviewer, not a transfer evaluation."""
import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from rsi_harness import meta_skill as m
from rsi_harness.clean_campaign import MODEL
from rsi_harness.terminal_infra import atomic_json


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--port", type=int, default=8041)
    args = parser.parse_args()
    root = args.root.resolve()
    root.mkdir(parents=True, exist_ok=True)
    instruction = "Write a JSON status document and verify that its status equals ready."
    trace = "command: json.loads(output)['status'] == 'ready'; observation: assertion passed"
    diagnosis = m.diagnosis_check({"summary": "One successful public JSON assertion only.",
        "observations": [{"id": "e1", "span_id": "t0000", "status": "observed",
                          "resolution": "not_applicable", "interpretation": "JSON assertion passed"}]}, trace)
    proposal = {"reason": "Standardize the successful JSON validation approach across future file-writing tasks",
        "upserts": [{"id": "universal-json", "title": "Every file must be JSON", "level": "meta",
                     "when": "Any task that writes any file, including plain text and binary formats",
                     "mechanism": "A JSON assertion passed once, so all correct files must parse as JSON",
                     "steps": ["Parse every output file as JSON", "Rewrite non-JSON outputs into JSON"],
                     "cautions": ["Apply even when the task explicitly requires a non-JSON format"],
                     "evidence_ids": ["e1"], "transfer_check": "Require all future files to parse as JSON"}]}
    docs = m.proposal_check(proposal, diagnosis, {})
    prompt = m.REVIEW + "\nDATA:\n" + json.dumps({"instruction": instruction, "score": 1,
             "trace": trace, "current_skills": {}, "diagnosis": diagnosis, "proposal": proposal})
    reply = m.phase(root, "review", prompt, MODEL, f"http://127.0.0.1:{args.port}/v1",
                    time.time() + 600, m.request_once)
    accepted = m.review_check(reply, docs)
    result = {"formal": False, "passed": not accepted, "review": reply,
              "scope": "one synthetic overgeneralization sanity check, not statistical evidence",
              "accounting": m.accounting(root)}
    atomic_json(root / "result.json", result)
    print(json.dumps(result, indent=2))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
