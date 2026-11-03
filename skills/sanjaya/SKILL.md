---
name: sanjaya
description: >-
  Opens the Sanjaya trajectory viewer side pane to watch, inspect, and steer
  Antigravity or Jetski agent trajectories. Use when the user asks to open
  Sanjaya, visualize a conversation as a DAG or timeline, inspect tool calls,
  thoughts, errors, token and context usage, compactions, or subagent trees, or
  steer or fork from an earlier step. Don't use for aggregate telemetry across
  users or for editing Sanjaya's own source.
---

# Sanjaya — live agent trajectory viewer

Sanjaya renders a conversation's transcript in the Auxiliary Pane and keeps it
updated while the agent works.

## Open the pane

When the user wants to see a trajectory, emit this pill once per response:

```markdown
[Sanjaya - Trajectory Viewer](sidecar://sanjaya/sanjaya/)
```

Clicking it toggles the Sanjaya panel. The panel follows the conversation it is
attached to; the picker switches to any recent conversation.

## What the pane shows

-   **Graph**: three zoom levels. *Phases* groups consecutive steps by work
    type (explore, edit, execute, delegate, research). *Turns* shows one node
    per user request with its subagents. *Steps* shows every model call with
    its tool calls fanned out and subagents linked.
-   **Timeline**: a latency waterfall with idle gaps compressed.
-   **Tools**: call counts, error counts, and average and total time per tool.
-   **Context**: fresh vs cache-read input tokens per model call, output
    tokens, and compaction markers.
-   **Subagents**: the recursive subagent tree with per-agent stats.
-   **Notes**: step-level and trajectory-level annotations.
-   **Inspector**: full thinking, tool arguments, results, and errors for a
    step, read from `transcript_full.jsonl`.
-   **Steer / Fork**: send guidance anchored at a step into the conversation,
    or start a new conversation from a handoff of everything up to that step.

## Filter syntax

The search box accepts free text plus `#<step>`, `is:error`, `is:tool`,
`is:model`, `is:user`, `is:checkpoint`, `is:system`, and category names such
as `is:edit`.

## Answering questions without the pane

The same data is available from the sidecar API on its local port:
`/api/conversations`, `/api/trajectory?id=`, `/api/step?id=&i=`,
`/api/subagents?id=`, and `/api/export?id=` (Markdown report).
