# Recovery Agent — Live Demo Script

This script walks through the end-to-end "10/10 moment" of the No-Show Recovery Agent.

## Setup Requirements
1. The server must be running (`python run_all.py --host 0.0.0.0`).
2. An ngrok tunnel must be active on port 5000.
3. Razorpay webhooks must be configured to point to your ngrok URL (`https://<id>.ngrok-free.app/webhooks/razorpay`) and listen for `payment_link.paid` and `payment.captured`.
4. The database should be reset to a clean state (`python batch_runner.py --reset-attempts`).

## The Demo Flow

### Phase 1: Ingestion & Rules (The "Agentic" part)
1. **Upload the data:** Open the UI and click "Upload Data". Select the provided `recovery_cases.csv`.
   * *Talking point:* "We drop in a raw CSV of calendar no-shows and failed subscriptions. The agent parses this instantly."
2. **Review the Funnel:** Point to the new funnel at the top.
   * *Talking point:* "Instantly we see our pipeline: Detected, Attempted, Recovered, and Still at Risk. It's a real funnel, not just flat metrics."
3. **Show RBI Compliance:** Open the drawer for case `SUB010` (Sana Kapoor).
   * *Talking point:* "The decision engine doesn't just blindly send emails. It enforces RBI e-mandate rules. Look here — 3 of 3 attempts used. The stopping rule fired, and the case was escalated to human review. The UI explains exactly why."
4. **Show High-Value Guardrail:** Open the drawer for `SUB001` (Leena Krishnan - ₹6000+ subscription) - or any high value subscription above ₹5000.
   * *Talking point:* "We also built in a business guardrail. If a failed subscription is over ₹5,000, it requires human sign-off. We don't want automated systems blindly retrying massive debits."

### Phase 2: Action & Recovery (The Loop)
5. **Send the Link:** Find case `SUB002` (Diya Patel) or `NS001` (Aarav Sharma) which is in the `Retry Payment` or `Charge Fee` state. Click **Send email**.
   * *Talking point:* "This generates a Razorpay payment link dynamically and emails the client."
6. **Open the Email:** Check your inbox (if you used your own email for testing) or open the Razorpay Dashboard to find the generated Payment Link.
7. **Make the Payment:** Open the payment link in a new tab. Complete the transaction using Razorpay Test Mode credentials (e.g. Netbanking -> Success).
   * *Talking point:* "The client gets the link, opens it, and pays. Now watch the dashboard."

### Phase 3: The Payoff (The "10/10 Moment")
8. **Watch the Dashboard Update:** Switch back to the No-Show Recovery Agent dashboard. Wait a few seconds for the webhook to fire.
   * *Talking point:* "We aren't refreshing the page. The webhook comes in, the agent intercepts it, verifies the signature, and..." (The UI updates).
9. **Show the Metrics:** Point to the top funnel again.
   * *Talking point:* "Look at the funnel. The Recovered amount just went up. This isn't 'we sent a link so we assume they paid'. This is 100% verified, cash-in-bank recovery."
10. **Export the Immutable Audit Trail:** Open the drawer for the case that was just paid. Go to the **History** tab.
    * *Talking point:* "Every single step — detection, the rule that fired, the link creation, and the final webhook confirmation — is logged. Click 'Download audit trail' to get a permanent CSV record for compliance."

*(End of Demo)*
