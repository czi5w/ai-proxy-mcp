# AI_Proxy MCP Bridge

A small Python service that connects [Hermes Agent](https://github.com/NousResearch/hermes-agent) to a fleet of in-domain machines running [AI_Proxy](../AI_Proxy) + Copilot CLI.

```
Feishu  ──>  Hermes Agent (gateway: hermes-feishu)
                    │
                    │  MCP over HTTP (localhost)
                    ▼
            AI_Proxy MCP Bridge   <── reverse WS ── AI_Proxy alice-pc
                                  <── reverse WS ── AI_Proxy bob-pc
```

The bridge runs two servers in one process:

- **WS Server** on `:8765` — accepts reverse connections from `ai_proxy.exe --ws-reverse`. Re-uses the existing register / request / chunk / done / error protocol; AI_Proxy needs no changes.
- **MCP HTTP Server** on `:8766/mcp` — exposes three tools that Hermes can call.

## MCP tools

| Tool                               | Description                                                              |
|------------------------------------|--------------------------------------------------------------------------|
| `mcp_ai_proxy_list_devices`        | List `device_id` of every connected AI_Proxy.                            |
| `mcp_ai_proxy_run_on_device`       | Send `prompt` to a device and wait for the full reply (default 600 s).   |
| `mcp_ai_proxy_get_device_status`   | Whether a device is connected and whether it has an in-flight task.      |

## Install

```bash
# Raspberry Pi / Ubuntu / macOS — Python 3.10+
git clone <repo> ai-proxy-mcp
cd ai-proxy-mcp

python3 -m venv .venv
source .venv/bin/activate
pip install -e .

cp .env.example .env
# edit .env if defaults don't suit
```

## Run (foreground)

```bash
ai-proxy-mcp
# or
python -m ai_proxy_mcp.main
```

You should see:

```
starting ai-proxy-mcp (ws=0.0.0.0:8765, mcp=127.0.0.1:8766/mcp)
WS server listening on ws://0.0.0.0:8765
MCP server built (tools: list_devices, run_on_device, get_device_status)
MCP HTTP server listening on http://127.0.0.1:8766/mcp
```

## Wire AI_Proxy to it

On each in-domain Windows machine:

```powershell
ai_proxy.exe --config config\proxy.toml `
             --ws-reverse <bridge-host>:8765 `
             --device-id <unique-name>
```

The bridge logs:

```
device "alice-pc" registered from ('192.168.x.x', 54321) (total=1)
```

## Wire Hermes to it

Install Hermes Agent on the same machine that runs the bridge (typically Raspberry Pi):

```bash
curl -fsSL https://raw.githubusercontent.com/NousResearch/hermes-agent/main/scripts/install.sh | bash
source ~/.bashrc

hermes gateway setup        # follow prompts; choose Feishu/Lark
```

Then add the MCP server to `~/.hermes/config.yaml`:

```yaml
mcp_servers:
  ai_proxy:
    url: "http://127.0.0.1:8766/mcp"
    timeout: 600
```

Restart Hermes to pick up the config:

```bash
hermes gateway stop && hermes gateway start
# or
/reload-mcp        # from inside an active Hermes session
```

## Environment variables

See [.env.example](.env.example).

| Var                          | Default          | Purpose                                              |
|------------------------------|------------------|------------------------------------------------------|
| `WS_HOST`                    | `0.0.0.0`        | Where AI_Proxy reverse-connects.                     |
| `WS_PORT`                    | `8765`           |                                                      |
| `MCP_HOST`                   | `127.0.0.1`      | Bind localhost only (Hermes is on the same machine). |
| `MCP_PORT`                   | `8766`           |                                                      |
| `MCP_PATH`                   | `/mcp`           | Informational; FastMCP fixes the path to `/mcp`.     |
| `TASK_TIMEOUT_SECONDS`       | `600`            | Per-tool-call deadline.                              |
| `REGISTER_TIMEOUT_SECONDS`   | `10`             | How long to wait for the first `register` frame.     |
| `LOG_LEVEL`                  | `INFO`           |                                                      |

## systemd (production)

See [deploy/ai-proxy-mcp.service](deploy/ai-proxy-mcp.service).

```bash
sudo cp deploy/ai-proxy-mcp.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now ai-proxy-mcp
journalctl -u ai-proxy-mcp -f
```

## Hermes context file

The bridge ships [hermes/context/ai_proxy.md](hermes/context/ai_proxy.md) — a short prompt that teaches the LLM how to use these tools intelligently.

**Important:** Hermes loads context from its current working directory, not from
an arbitrary file path. The file must be named one of `.hermes.md`,
`AGENTS.md`, `CLAUDE.md`, or `.cursorrules` (first match wins) and live in
the gateway's working directory.

Recommended setup:

```bash
# 1. Drop the file into ~/.hermes as AGENTS.md
cp hermes/context/ai_proxy.md ~/.hermes/AGENTS.md

# 2. Tell the gateway to use ~/.hermes as its CWD
#    Edit ~/.hermes/config.yaml and add:
#       terminal:
#         cwd: /home/<your-user>/.hermes
#    or set MESSAGING_CWD in ~/.hermes/.env

# 3. Restart so it picks up the new CWD + context file
hermes gateway stop && hermes gateway start
```

Verify by DMing the bot `pwd` — it should print the directory containing
your `AGENTS.md`.
