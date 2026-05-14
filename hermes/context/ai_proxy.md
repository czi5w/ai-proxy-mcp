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
