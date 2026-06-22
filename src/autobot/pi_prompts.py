from __future__ import annotations

from autobot.harness import HarnessTask, HarnessTaskKind


def task_prompt(task: HarnessTask) -> str:
    feedback = "\n".join(task.review_findings) or "None"
    feedback_label = "Fix feedback"
    if task.kind == HarnessTaskKind.REVIEW_FIX:
        feedback_label = "Blocking reviewer findings"
    elif task.kind == HarnessTaskKind.VERIFICATION_FIX:
        feedback_label = "Verification failure output"
    context = "\n\n".join(f"## {item.path}\n{item.content}" for item in task.context)
    planner = task.planning_context or "None"
    return (
        "You are Autobot's implementation harness. Modify files directly in this repository. "
        "Keep changes scoped to the issue. Run only necessary local inspection commands. "
        "Do not create commits, branches, pull requests, comments, or network calls except "
        "LLM calls. "
        "When finished, respond with one JSON object and no extra prose: "
        '{"plan":["..."],"test_commands":["..."],"changed_paths":["..."]}.\n\n'
        f"Task kind: {task.kind.value}\n"
        f"Issue #{task.issue.number}: {task.issue.title}\n\n{task.issue.body}\n\n"
        f"Planner output:\n{planner}\n\n"
        f"{feedback_label}:\n{feedback}\n\n"
        f"Repo context:\n{context or 'No focused context gathered.'}"
    )


def planner_prompt(task: HarnessTask) -> str:
    context = "\n\n".join(f"## {item.path}\n{item.content}" for item in task.context)
    return (
        "You are Autobot's read-only planning agent. Inspect the repository freely using "
        "read-only tools and shell commands that do not modify files. Do not edit files, "
        "create commits, push branches, open pull requests, or leave generated artifacts. "
        "Your job is to produce an implementation strategy for a cheaper implementer. "
        "Return one JSON object and no prose matching planner contract version 1. Extra "
        "keys are invalid. Required keys: contract_version number set to 1, summary "
        "string, target_files array of strings, constraints array of strings, "
        "implementation_steps array of strings, tests_to_add array of strings, "
        "verification_commands array of strings, risks array of strings, non_goals array "
        "of strings.\n\n"
        f"Issue #{task.issue.number}: {task.issue.title}\n\n{task.issue.body}\n\n"
        f"Initial repo context:\n{context or 'No focused context gathered. Inspect the repo.'}"
    )
