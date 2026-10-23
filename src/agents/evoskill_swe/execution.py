"""Check actual native events rather than the loop's nominal iteration count."""


def audit_execution(events, config, support_ids):
    errors, iterations, completions = [], [], []
    for event in events:
        name, data = event["event"], event["data"]
        if name == "iter_start":
            iterations.append({"iteration": data["iteration"], "samples": [],
                               "proposals": 0, "candidate_evaluations": 0})
        elif name == "loop_done":
            completions.append(data)
        elif name in ("sample", "proposal", "eval_result"):
            if not iterations:
                errors.append(f"{name} emitted before iter_start")
                continue
            current = iterations[-1]
            if name == "sample":
                current["samples"].append(data)
            elif name == "proposal":
                current["proposals"] += 1
            else:
                current["candidate_evaluations"] += 1

    expected_iterations = config.max_iterations
    expected_samples = min(config.samples_per_category, len(support_ids))
    if len(iterations) != expected_iterations:
        errors.append(f"expected {expected_iterations} executed iterations, observed {len(iterations)}")
    if len({i["iteration"] for i in iterations}) != len(iterations):
        errors.append("duplicate native iteration starts")
    if len(completions) != 1 or completions[0].get("iterations") != expected_iterations:
        errors.append("missing or inconsistent native loop_done")
    for current in iterations:
        samples = current["samples"]
        questions = [s["question"] for s in samples]
        if len(samples) != expected_samples or len(set(questions)) != expected_samples:
            errors.append(f"iteration {current['iteration']} did not execute its expected distinct samples")
        if not set(questions).issubset(support_ids):
            errors.append("native samples include a task outside this completed support window")
        if current["candidate_evaluations"]:
            current["outcome"] = "candidate_evaluated"
        elif samples and all(s["passed"] for s in samples):
            current["outcome"] = "all_samples_passed"
        else:
            # This is a real native attempt with no usable mutation, not a
            # silently skipped iteration or a successful skill improvement.
            current["outcome"] = "no_candidate"
    return {"schema": "evoskill-update-execution-v1", "passed": not errors,
            "errors": errors, "expected_iterations": expected_iterations,
            "executed_iterations": len(iterations), "expected_samples_per_iteration": expected_samples,
            "iterations": iterations}
