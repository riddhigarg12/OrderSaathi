# OrderSaathi backend

Python 3.10+.

## Run
    pip install -r requirements.txt
    cp .env.example .env        # fill in values
    uvicorn main:app --reload --port 8000 --env-file .env
    ngrok http 8000             # public URL for Bolna

Dashboard: http://localhost:8000. The database is seeded on first start (11 products, 4 retailers, 4 overdue invoices).

## Wire up Bolna
1. In `ordersaathi_function_schemas.json`, replace `YOUR_BACKEND_URL` with your ngrok host and `YOUR_API_TOKEN` with your `API_TOKEN`. Paste each function into the matching agent's Tools tab.
2. Agent webhook URL (both agents): `https://<ngrok-host>/webhooks/bolna?key=<API_TOKEN>`
3. Transfer Call tool: if you enable its pre-call webhook, point it at `https://<ngrok-host>/webhooks/escalation?key=<API_TOKEN>` and send JSON with `call_sid` and `reason`.
4. Set `BOLNA_API_KEY` and `BOLNA_OUTBOUND_AGENT_ID` in `.env` for outbound calls.

## Test the tools without voice
    H='Authorization: Bearer change-me'
    curl -s -X POST localhost:8000/tools/search_product -H "$H" -H 'Content-Type: application/json' -d '{"call_sid":"t1","query":"parle ji"}'
    curl -s -X POST localhost:8000/tools/update_cart -H "$H" -H 'Content-Type: application/json' -d '{"call_sid":"t1","from_number":"9XXXXXXXXX","product_id":"P01","quantity":12}'
    curl -s -X POST localhost:8000/tools/confirm_order -H "$H" -H 'Content-Type: application/json' -d '{"call_sid":"t1","from_number":"9XXXXXXXXX","read_back_total":4560}'
    curl -s -X POST localhost:8000/tools/log_payment_promise -H "$H" -H 'Content-Type: application/json' -d '{"call_sid":"t2","invoice_no":"INV-2041","outcome":"will_pay","promised_date_text":"Friday tak"}'

Demo cases built into the seed data: "maggi" is out of stock with a Yippee substitute; "surf excel" is ambiguous (1kg vs 500g); Verma Stores trips the credit-limit review on any order.

## Check before the demo
- The `/webhooks/bolna` field names (`id`, `status`, `transcript`, `telephony_data`) are my best guess. Place one real call, print the payload, and adjust that handler if the call row does not link to its order.
- Outbound `user_data` keys must match the `{variables}` in the outbound prompt exactly.
- The dashboard has no login and `/api/state` is open. Keep the tunnel private after the demo.
