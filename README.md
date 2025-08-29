Sweeper Service
===============

Purpose
- Periodically (default: every 30 minutes) checks all user subaddresses stored by MoneroWalletManager, sweeps any unlocked funds from each subaddress to a primary address, and credits the equivalent amount into the platform's "fake funds" via the Transactions service.

Environment
- MONERO_SERVICE_URL: Base URL to MoneroWalletManager (default http://monero:8004). In the stack use http://api-manager:8000/monero.
- TRANSACTIONS_SERVICE_URL: Base URL to Transactions service (default http://transactions:8003). In the stack use http://api-manager:8000/transactions.
- SWEEP_INTERVAL_SECONDS: Interval between sweep cycles (default 1800 = 30 minutes).
- MIN_SWEEP_XMR: Minimum unlocked balance in XMR to trigger a sweep from a subaddress (default 0.0001).
- TARGET_SWEEP_ADDRESS: Optional destination address. If not set, the service fetches /primary_address from MoneroWalletManager.
- LOG_LEVEL: INFO by default.

How it works
1) Discover sweep target address.
2) List all AddressMap entries via GET /monero/addresses.
3) For each subaddress (excluding the target itself):
   - GET /monero/balance/{address} and read unlocked_balance_xmr.
   - If unlocked >= MIN_SWEEP_XMR, POST /monero/sweep_all {from_address, to_address}.
   - On success, POST /transactions/balance/{user_id}/increase {amount_xmr, kind: "fake"}.
4) Log per-address results and a summary at the end of each cycle.

Run locally (docker)
- Included in docker-compose as `sweeper`. Ensure monero-wallet-rpc and MoneroWalletManager are functioning first.

Notes
- The sweeper skips sweeping the target address to avoid loops.
- Swept totals are obtained from the /sweep_all response (total_xmr). If the wallet has pending unlock times, sweeps may be partial.
