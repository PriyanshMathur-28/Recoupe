import os
from dotenv import load_dotenv
import razorpay

load_dotenv()
client = razorpay.Client(auth=(os.getenv("RAZORPAY_KEY_ID"), os.getenv("RAZORPAY_KEY_SECRET")))
try:
    links = client.payment_link.all({"count": 100})
    items = links.get("payment_links", links.get("items", []))
    print("count:", len(items))
    statuses = {}
    for it in items:
        statuses[it.get("status")] = statuses.get(it.get("status"), 0) + 1
    print("by status:", statuses)
    for it in items[:5]:
        print(it.get("id"), it.get("status"), it.get("amount"), (it.get("customer") or {}).get("email"))
except Exception as e:
    print("ERR", type(e).__name__, repr(e))
