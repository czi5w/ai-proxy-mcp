# Production deployment

Two systemd services on the same host (Raspberry Pi or VPS):

- `hermes-gateway` — installed by Hermes itself (`hermes gateway install`).
- `ai-proxy-mcp` — installed manually with the unit file in this folder.

## 1. Install the bridge

```bash
sudo apt update
sudo apt install -y python3-venv git

git clone <repo> /home/pi/ai-proxy-mcp
cd /home/pi/ai-proxy-mcp

python3 -m venv .venv
.venv/bin/pip install -e .

cp .env.example .env
# edit .env if needed
```

## 2. Install the systemd unit

The unit file assumes user `pi` and path `/home/pi/ai-proxy-mcp`. **You
almost certainly need to edit it** to match your actual user and install
location.

Easiest with `sed` (replace `myuser` with your real username):

```bash
USER_NAME=$(whoami)
sed "s|User=pi|User=$USER_NAME|g; s|Group=pi|Group=$USER_NAME|g; \
     s|/home/pi/|$HOME/|g" deploy/ai-proxy-mcp.service \
  | sudo tee /etc/systemd/system/ai-proxy-mcp.service > /dev/null

sudo systemctl daemon-reload
sudo systemctl enable --now ai-proxy-mcp
sudo systemctl status ai-proxy-mcp
```

Don't forget to create the `.env` file before starting:

```bash
cp .env.example .env
$EDITOR .env
```

Logs:

```bash
journalctl -u ai-proxy-mcp -f
```

## 3. Install Hermes

```bash
curl -fsSL https://raw.githubusercontent.com/NousResearch/hermes-agent/main/scripts/install.sh | bash
source ~/.bashrc

# Configure Feishu (and any other platforms)
hermes gateway setup

# Wire the MCP bridge
mkdir -p ~/.hermes
# Merge the snippet into ~/.hermes/config.yaml:
cat /home/pi/ai-proxy-mcp/hermes/config.yaml.example >> ~/.hermes/config.yaml
$EDITOR ~/.hermes/config.yaml      # consolidate any duplicate keys

# Drop the context file as AGENTS.md in ~/.hermes (or .hermes.md)
cp /home/pi/ai-proxy-mcp/hermes/context/ai_proxy.md ~/.hermes/AGENTS.md

# Tell the gateway to chdir into ~/.hermes so it discovers AGENTS.md.
# Add to ~/.hermes/config.yaml:
#   terminal:
#     cwd: /home/pi/.hermes

# Run as a user service
hermes gateway install
hermes gateway start
hermes gateway status
```

Hermes logs:

```bash
journalctl --user -u hermes-gateway -f
```

## 4. Smoke test

1. Start one AI_Proxy on a Windows machine:
   ```powershell
   ai_proxy.exe --config config\proxy.toml `
                --ws-reverse <pi-ip>:8765 `
                --device-id alice-pc
   ```
2. In `journalctl -u ai-proxy-mcp -f` you should see
   `device "alice-pc" registered ...`.
3. From Feishu, DM the bot: `@bot list my devices`. Hermes should call
   `mcp_ai_proxy_list_devices` and reply with `["alice-pc"]`.
4. Then ask: `@bot in alice-pc, write a quicksort in python`. Hermes should
   call `mcp_ai_proxy_start_task(device_id="alice-pc", prompt=...)`, then
   loop `mcp_ai_proxy_wait_for_progress(task_id, 30)` and relay each
   progress chunk to the user before posting the final result.

## 5. Restart order on reboot

Both units come up in parallel. The MCP bridge is order-independent: AI_Proxy
clients reconnect with backoff, and Hermes only calls MCP on demand.
