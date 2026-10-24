"""
FastAPI Routes for Multi-Account Monitoring, VPN Proxy Management, and Load Balancing.
"""

from fastapi import APIRouter, HTTPException
from typing import Dict, Any, List
import time

from ..accounts import global_account_manager
from ..proxy_manager import global_proxy_manager
from ..pool import global_pool

router = APIRouter()

@router.get("/v1/accounts")
async def get_accounts_status() -> Dict[str, Any]:
    """Returns real-time status of all configured accounts and their assigned VPN nodes."""
    acc_list = [acc.to_dict() for acc in global_account_manager.accounts.values()]
    healthy_count = len(global_account_manager.get_healthy_accounts())
    cooling_count = sum(1 for a in global_account_manager.accounts.values() if a.is_cooling)

    return {
        "summary": {
            "total_accounts": len(global_account_manager.accounts),
            "healthy_accounts": healthy_count,
            "cooling_accounts": cooling_count,
            "load_balance_strategy": global_account_manager.load_balance_strategy,
            "subscription_configured": bool(global_account_manager.subscription_url),
            "singbox_active": global_proxy_manager.process is not None
        },
        "accounts": acc_list
    }

@router.post("/v1/accounts/reload")
async def reload_accounts() -> Dict[str, Any]:
    """Hot-reloads accounts.json and updates proxy bindings without gateway downtime."""
    try:
        global_account_manager.load_accounts()
        await global_pool.initialize()
        return {
            "status": "success",
            "message": "Accounts and proxy bindings reloaded successfully.",
            "total_accounts": len(global_account_manager.accounts)
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to reload accounts: {e}")

@router.get("/v1/proxies/nodes")
async def get_proxy_nodes() -> Dict[str, Any]:
    """Returns the list of parsed VPN nodes from subscription or manual configuration."""
    nodes = [node.to_dict() for node in global_proxy_manager.nodes]
    return {
        "count": len(nodes),
        "active_inbounds": global_proxy_manager.active_inbounds,
        "nodes": nodes
    }

@router.post("/v1/proxies/refresh")
async def refresh_proxies() -> Dict[str, Any]:
    """Re-fetches subscription URL, re-parses nodes, and updates Sing-box tunnels."""
    if not global_account_manager.subscription_url:
        raise HTTPException(status_code=400, detail="No subscription_url configured in accounts.json")

    nodes = global_proxy_manager.fetch_subscription(global_account_manager.subscription_url)
    global_account_manager.load_accounts()
    return {
        "status": "success",
        "node_count": len(nodes),
        "active_inbounds": global_proxy_manager.active_inbounds
    }
