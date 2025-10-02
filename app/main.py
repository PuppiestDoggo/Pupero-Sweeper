import os
import asyncio
import logging
import json
from typing import Any, Dict, List
from datetime import datetime, timezone

import httpx
from dotenv import load_dotenv

load_dotenv()

def _normalize_service_url(val: str | None, kind: str) -> str:
    # kind: "monero" or "transactions"
    # Prefer direct service defaults to reduce proxy/startup coupling
    default = "http://pupero-WalletManager:8004" if kind == "monero" else "http://pupero-transactions:8003"
    if not val:
        return default
    v = val.strip().rstrip("/")
    if "://" in v:
        return v
    name = v
    # Map known containers to ports and paths
    if name in {"api-manager", "pupero-api-manager"}:
        base = f"http://{name}:8000"
        return base + ("/monero" if kind == "monero" else "/transactions")
    if name in {"monero", "pupero-WalletManager"}:
        return f"http://{name}:8004"
    if name in {"transactions", "pupero-transactions"}:
        return f"http://{name}:8003"
    # Fallback: assume direct service defaults
    return default

MONERO_BASE = _normalize_service_url(os.getenv("MONERO_SERVICE_URL"), "monero")
TX_BASE = _normalize_service_url(os.getenv("TRANSACTIONS_SERVICE_URL"), "transactions")
SWEEP_INTERVAL = int(os.getenv("SWEEP_INTERVAL_SECONDS", "1800"))
MIN_SWEEP_XMR = float(os.getenv("MIN_SWEEP_XMR", "0.0001"))
TARGET_SWEEP_ADDRESS = os.getenv("TARGET_SWEEP_ADDRESS")
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()

logger = logging.getLogger("pupero_sweeper")
if not logger.handlers:
    h = logging.StreamHandler()
    logger.setLevel(getattr(logging, LOG_LEVEL, logging.INFO))
    logger.addHandler(h)


async def get_primary_address(client: httpx.AsyncClient) -> str:
    url = f"{MONERO_BASE}/primary_address"
    last_err = None
    for attempt in range(3):
        try:
            r = await client.get(url, timeout=20.0)
            r.raise_for_status()
            data = r.json()
            addr = data.get("address")
            if not addr:
                raise RuntimeError("primary_address returned no address")
            return addr
        except Exception as e:
            last_err = e
            # small backoff to allow services to come up
            await asyncio.sleep(1.0)
    raise RuntimeError(str(last_err) if last_err else "failed to fetch primary_address")


async def list_address_mappings(client: httpx.AsyncClient) -> List[Dict[str, Any]]:
    url = f"{MONERO_BASE}/addresses"
    r = await client.get(url, timeout=20.0)
    r.raise_for_status()
    return r.json() or []


async def get_unlocked_xmr(client: httpx.AsyncClient, address: str) -> float:
    url = f"{MONERO_BASE}/balance/{address}"
    r = await client.get(url, timeout=30.0)
    r.raise_for_status()
    data = r.json() or {}
    return float(data.get("unlocked_balance_xmr", 0.0))


async def sweep_from_address(client: httpx.AsyncClient, from_address: str, to_address: str) -> float:
    url = f"{MONERO_BASE}/sweep_all"
    payload = {"from_address": from_address, "to_address": to_address}
    r = await client.post(url, json=payload, timeout=60.0)
    r.raise_for_status()
    data = r.json() or {}
    total = float(data.get("total_xmr", 0.0))
    return total


async def credit_real_funds(client: httpx.AsyncClient, user_id: int, amount_xmr: float) -> None:
    if amount_xmr <= 0:
        return
    url = f"{TX_BASE}/balance/{user_id}/increase"
    payload = {"amount_xmr": amount_xmr, "kind": "real"}
    r = await client.post(url, json=payload, timeout=20.0)
    r.raise_for_status()


async def sweep_cycle():
    async with httpx.AsyncClient() as client:
        try:
            target = TARGET_SWEEP_ADDRESS or await get_primary_address(client)
        except Exception as e:
            logger.error(json.dumps({"event": "sweep_target_error", "error": str(e)}))
            return
        try:
            mappings = await list_address_mappings(client)
        except Exception as e:
            logger.error(json.dumps({"event": "list_addresses_error", "error": str(e)}))
            return
        summary = {"checked": 0, "swept": 0, "credited": 0.0}
        for m in mappings:
            try:
                addr = m.get("address")
                user_id = int(m.get("user_id"))
                if not addr or not user_id:
                    continue
                if addr == target:
                    # Avoid sweeping the target itself
                    continue
                summary["checked"] += 1
                unlocked = await get_unlocked_xmr(client, addr)
                if unlocked >= MIN_SWEEP_XMR:
                    swept = await sweep_from_address(client, addr, target)
                    if swept > 0:
                        await credit_real_funds(client, user_id, swept)
                        summary["swept"] += 1
                        summary["credited"] += swept
                        logger.info(json.dumps({"event": "swept_and_credited", "user_id": user_id, "from": addr, "to": target, "amount_xmr": swept}))
                # If this address is disabled and past deletion_date and now empty, delete mapping
                try:
                    is_disabled = bool(m.get("is_disabled", False))
                    deletion_date = m.get("deletion_date")
                    addr_id = m.get("id")
                    if is_disabled and deletion_date and addr_id and unlocked < MIN_SWEEP_XMR:
                        # Parse ISO date; handle possible 'Z' suffix
                        try:
                            cutoff = datetime.fromisoformat(str(deletion_date).replace("Z", "+00:00"))
                        except Exception:
                            cutoff = None
                        if cutoff and datetime.now(timezone.utc) >= cutoff.replace(tzinfo=timezone.utc):
                            dr = await client.delete(f"{MONERO_BASE}/addresses/{addr_id}", timeout=15.0)
                            if dr.status_code in (200, 204):
                                logger.info(json.dumps({"event": "address_deleted", "address_id": addr_id, "address": addr, "user_id": user_id}))
                except Exception as e:
                    logger.warning(json.dumps({"event": "address_delete_check_failed", "address": addr, "error": str(e)}))
            except httpx.HTTPStatusError as e:
                logger.warning(json.dumps({"event": "address_process_http_error", "address": m.get("address"), "status": e.response.status_code if e.response else None}))
            except Exception as e:
                logger.warning(json.dumps({"event": "address_process_error", "address": m.get("address"), "error": str(e)}))
        logger.info(json.dumps({"event": "sweep_cycle_summary", **summary}))


async def main_loop():
    logger.info(json.dumps({"event": "sweeper_start", "interval_seconds": SWEEP_INTERVAL, "min_sweep_xmr": MIN_SWEEP_XMR}))
    while True:
        try:
            await sweep_cycle()
        except Exception as e:
            logger.error(json.dumps({"event": "sweep_cycle_exception", "error": str(e)}))
        await asyncio.sleep(SWEEP_INTERVAL)


if __name__ == "__main__":
    try:
        asyncio.run(main_loop())
    except KeyboardInterrupt:
        pass
