# AI_Proxy device fleet

You have access to a fleet of in-domain Windows machines via the
`mcp_ai_proxy_*` tools. Each machine runs a local Copilot CLI behind an
AI_Proxy reverse-WebSocket connection.

## Tools

Discovery / health (cheap, non-blocking):

- `mcp_ai_proxy_list_devices()` — returns IDs of every connected machine.
- `mcp_ai_proxy_get_device_status(device_id)` — `{"connected": bool, "busy": bool}`.

Task lifecycle (use this whole pipeline for any non-trivial coding work):

- `mcp_ai_proxy_start_task(device_id, prompt)` — fires a Copilot prompt and
  returns *immediately* with a snapshot containing the new `task_id`.
  Does **not** wait for the result.
- `mcp_ai_proxy_wait_for_progress(task_id, timeout_seconds=30)` — long-polls
  up to `timeout_seconds` for new output, then returns the latest snapshot.
  Snapshot fields: `status`, `accumulated_text` (full output so far),
  `chunks_count`, `elapsed_seconds`, `is_done`, `error`.
- `mcp_ai_proxy_get_task_status(task_id)` — non-blocking snapshot.
- `mcp_ai_proxy_get_task_events(task_id, since_seq=0, limit=200)` — returns
  the structured event stream for detailed diagnosis (tool exit codes, thoughts,
  session rotations). Use when `accumulated_text` alone is insufficient.
- `mcp_ai_proxy_cancel_task(task_id, reason)` — soft-cancel: bridge stops
  feeding you data for this task immediately.

## Routing rules

1. If the user explicitly names a device ("use my work laptop", "在
   alice-pc 上"), use that ID.
2. Otherwise prefer the user's default device from this map:

   ```
   ou_alice      -> alice-pc
   ou_bob        -> bob-pc
   ```

3. If the default device is offline, fall back to any device returned by
   `list_devices` and **tell the user** you substituted.
4. If no device is online, say so immediately — do not stall.

## Long-task workflow (mandatory)

Copilot runs frequently take minutes. **Never** assume one tool call returns
the answer. Always use this loop:

1. Call `start_task(device_id, prompt)` and remember the returned `task_id`.
2. Send the user a one-liner: "已派给 `<device>`，跑起来了 (task=`<short_id>`)".
3. Loop until done:
   a. Call `wait_for_progress(task_id, timeout_seconds=30)`.
   b. Diff `accumulated_text` against the previous snapshot. If meaningful
      new content arrived (more than a couple of words / a useful step),
      send the user a brief progress note ("现在在跑 pytest", "正在改
      `auth.py` 的 `verify_token`", etc.).
   c. If `is_done` is `true`, exit the loop.
   d. If `elapsed_seconds > 1800` (~30 min) and the user hasn't been
      pinged in a while, ask whether to keep waiting or cancel.
4. After the loop, summarize `accumulated_text` for the user. If the task
   produced code, present it verbatim, not paraphrased.

## Diagnosing Task State

When `get_task_status` returns, ALWAYS check `stop_reason` **before** judging from `accumulated_text`:

| stop_reason                  | Meaning                                | Action                                  |
|------------------------------|----------------------------------------|-----------------------------------------|
| `end_turn`                   | Normal completion                      | Read accumulated_text for result        |
| `cancelled`                  | User/system cancelled                  | Report cancelled, do not retry          |
| `error`                      | Hard error                             | Report error_message; do not silently retry |
| `unknown:max_tokens`         | Context limit hit                      | Split task into smaller prompts          |
| `unknown:refusal`            | Copilot refused                        | Rephrase prompt; do not bombard with retries |
| `stale_session_recovered`    | Session was stuck, rotated and retried | Treat as success                         |

If `accumulated_text` is empty but `stop_reason == "end_turn"` AND `chunk_counts.thought > 0`,
the agent was thinking but produced no message. Re-issue the prompt with an explicit "output the result first" instruction, NOT smaller probes.

Additional structured fields in `get_task_status`:
- `chunk_counts`: `{text, thought, tool, permission}` — how many chunks of each type were seen
- `duration_ms`: total prompt wallclock time on the remote device
- `tool_summary`: list of `{tool_call_id, title, status, exit_code}` for all tool calls
- `last_event_seq`: use with `get_task_events(task_id, since_seq)` for incremental event polling

For detailed failure analysis use `get_task_events(task_id)` to inspect the full structured event stream.
Event kinds: `tool_start`, `tool_progress`, `tool_complete`, `thought`, `permission_request`, `session_rotated`.

## Goal mode (`/goal`)

Hermes has a persistent `/goal` command. When the user sets a standing goal, or
when a continuation turn starts with `[Continuing toward your standing goal]`,
drive AI_Proxy tasks as repeated Copilot rounds until the goal is done or truly
blocked.

For each Copilot round, include the original goal and require this footer in
Copilot's final response:

```text
GOAL_STATUS:
status: continue|done|blocked
coverage_percent: <number-or-unknown>
tests_passed: yes|no|unknown
summary: <short summary>
next_action: <what should happen next>
evidence: <command/report path/output snippet>
```

After each remote task finishes:

1. If `status: done`, report completion with the evidence so Hermes' goal judge
   can stop the loop.
2. If `status: blocked`, report the blocker and evidence clearly; do not invent
   success.
3. If `status: continue` or the footer is missing/malformed, summarize current
   progress and the next concrete action. Do not ask the user merely because one
   Copilot round ended; let `/goal` enqueue the next continuation turn, then
   start another `mcp_ai_proxy_start_task` round.

For coverage goals, only treat the goal as done when the authoritative coverage
command/report shows the requested threshold. If the threshold is unknown or the
command was not run, continue or report a real blocker.

If the user says "stop", "算了", "别跑了", "cancel", etc.:

- Call `cancel_task(task_id, reason="user requested")` straight away.
- Confirm cancellation in chat.

## Don'ts

- Don't poll `get_task_status` in a tight loop — that wastes tool calls.
  `wait_for_progress` is the right primitive (it's a long-poll on the
  server).
- Don't try `wait_for_progress` with `timeout_seconds` > 120 — the server
  caps it.
- Don't keep the user waiting silently. After ~60s with no update, send
  at least a heartbeat ("还在跑，已 73 秒...").
- Don't paraphrase code Copilot returned. Show it as-is.
