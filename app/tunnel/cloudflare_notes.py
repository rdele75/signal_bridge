"""Notes on exposing SignalBridge with Cloudflare Tunnel.

Cloudflare Tunnel (cloudflared) gives you a stable HTTPS URL bound to
a domain you own, free of charge. It's a good upgrade from ngrok once
your TradingView alerts are wired and you want a permanent endpoint.

Quick setup (named tunnel, recommended):

1. Install cloudflared:
       https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/
2. Log in:
       cloudflared tunnel login
3. Create a tunnel:
       cloudflared tunnel create signalbridge
4. Route a hostname under a domain you control in Cloudflare:
       cloudflared tunnel route dns signalbridge sb.example.com
5. Create a config file (typically ~/.cloudflared/config.yml):

       tunnel: signalbridge
       credentials-file: /home/you/.cloudflared/<UUID>.json
       ingress:
         - hostname: sb.example.com
           service: http://127.0.0.1:8000
         - service: http_status:404

6. Run:
       cloudflared tunnel run signalbridge

7. Point TradingView at:
       https://sb.example.com/webhooks/tradingview

Quick-and-dirty alternative (no domain needed):

       cloudflared tunnel --url http://127.0.0.1:8000

This prints an ephemeral *.trycloudflare.com URL. Useful for testing,
but it rotates each run and is not stable enough for production alerts.
"""

NOTES = __doc__
