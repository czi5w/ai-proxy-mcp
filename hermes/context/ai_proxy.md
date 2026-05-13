# AI_Proxy device fleet

You have access to a fleet of in-domain Windows machines via the
`mcp_ai_proxy_*` tools. Each machine runs a local Copilot CLI behind an
AI_Proxy reverse-WebSocket connection.

## Tools

- `mcp_ai_proxy_list_devices()` — returns the IDs of every connected machine.
- `mcp_ai_proxy_run_on_device(device_id, prompt)` — sends `prompt` to the named
  machine and returns the full Copilot reply. **This is your primary tool for
  any coding question or task that needs to touch the user's actual codebase.**
- `mcp_ai_proxy_get_device_status(device_id)` — checks whether a device is
  connected and whether it is currently busy.

## Routing rules

1. If the user explicitly names a device (e.g. "use my work laptop", "在
   alice-pc 上"), call `run_on_device` with that ID directly.
2. Otherwise, prefer the user's default machine from this map:

   ```
   ou_alice      -> alice-pc
   ou_bob        -> bob-pc
   ```

   (If your map differs, edit this file.)
3. If the default device is offline, fall back to any other connected device
   (`list_devices`) and **mention the substitution to the user**.
4. If no device is online, tell the user immediately — do not stall.

## Behavior

- Always present the Copilot reply to the user verbatim or as a short summary.
  Do not paraphrase code unless the user asked for a summary.
- If a `run_on_device` call fails or times out, retry **once** on the same
  device, then surface the error.
- For long-running tasks, mention which device is running it in your first
  reply so the user knows what to watch.
- Don't poll `get_device_status` repeatedly — it's for diagnostics, not
  progress tracking.
