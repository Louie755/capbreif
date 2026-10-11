# CapBrief

Study the business of sports. Built by Louie Suh.

A Flask app: the site lives in `static/`, and `app.py` calls the Claude API, enforces the free limit and unlocks Pro through Stripe.

## Run it on your computer

```bash
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env        # then fill in the keys, and uncomment DEV=1
python app.py               # open http://localhost:5000
```

## Launch checklist

1. **Claude API key.** Create one at console.anthropic.com, add billing and set a monthly spend limit so a traffic spike can't surprise you.
2. **Stripe.**
   - Create two products: Pro at $9.99/month and Front Office at $29/month.
   - Make a Payment Link for each. Under *After payment*, choose "Don't show confirmation page" and redirect to
     `https://YOURDOMAIN/success?session_id={CHECKOUT_SESSION_ID}`
   - Turn on the Customer Portal (Settings → Billing → Customer portal) so "Manage plan" works.
   - Use test mode keys first, buy with card 4242 4242 4242 4242, then switch to live keys.
3. **Deploy (Render example).** Push this folder to GitHub → New Web Service on render.com → it detects the `Procfile`. Add every variable from `.env.example` (leave `DEV` out) in the Environment tab.
4. **Domain.** Add `capbrief.com` (or a subdomain like `capbrief.nhlanalytica.com`) in Render's Custom Domains and point your DNS at it. HTTPS is automatic.
5. **Edit before going live.**
   - `static/privacy.html` and `static/terms.html`: replace `hello@capbrief.com` with your email and read them through. They're a starting point, not legal advice.
   - The "Why I built this" note in `static/index.html`, so it's in your words.

## Good to know

- Pro is tied to the browser where someone paid (a signed cookie). Paying on a laptop doesn't unlock their phone. Adding email login is the next upgrade.
- Free-limit counters live in memory and reset when the server restarts. Keep `--workers 1`, or move counters to Redis when you scale.
- Models are set by `NOTES_MODEL` and `ASK_MODEL` env vars if you want to swap them.
