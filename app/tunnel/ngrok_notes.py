"""Notes on exposing SignalBridge with ngrok.

ngrok is the quickest way to give TradingView a public HTTPS URL that
forwards to your local SignalBridge instance.

1. Install ngrok: https://ngrok.com/download
2. Sign up and run `ngrok config add-authtoken <YOUR_TOKEN>`.
3. Start SignalBridge: `./run.sh` (or `run.bat` on Windows).
4. In a second terminal, run:

       ngrok http 8000

   ngrok prints a public HTTPS URL, e.g. https://abc123.ngrok-free.app.

5. In TradingView, set the alert webhook URL to:

       https://abc123.ngrok-free.app/webhooks/tradingview

Caveats:
- Free ngrok URLs change every time you restart ngrok. Use a reserved
  domain on a paid plan to keep TradingView alerts stable.
- ngrok will receive every request before SignalBridge. Treat it as
  trusted-enough for a single-user setup, and rely on the shared
  secret field for authenticity.
"""

NOTES = __doc__
