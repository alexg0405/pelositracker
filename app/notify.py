from __future__ import annotations

import asyncio
import ipaddress
import logging
import os
import socket
from urllib.parse import urlparse

import httpx

logger = logging.getLogger(__name__)


def _allowed_hosts() -> set[str]:
    configured = os.getenv("WEBHOOK_ALLOWED_HOSTS", "discord.com,hooks.slack.com")
    return {host.strip().casefold().rstrip(".") for host in configured.split(",") if host.strip()}


async def validate_webhook_url(webhook_url: str) -> str:
    parsed = urlparse(webhook_url)
    host = (parsed.hostname or "").casefold().rstrip(".")
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("webhook port is invalid") from exc
    if (parsed.scheme != "https" or not host or parsed.username or parsed.password
            or parsed.fragment or (port is not None and port != 443)):
        raise ValueError("webhook must be an HTTPS URL without embedded credentials")
    if not any(host == allowed or host.endswith(f".{allowed}") for allowed in _allowed_hosts()):
        raise ValueError("webhook host is not allowlisted")
    try:
        literal = ipaddress.ip_address(host)
        addresses = [literal]
    except ValueError:
        records = await asyncio.to_thread(socket.getaddrinfo, host, 443, type=socket.SOCK_STREAM)
        addresses = [ipaddress.ip_address(record[4][0]) for record in records]
    if not addresses or any(address.is_private or address.is_loopback or address.is_link_local
                            or address.is_multicast or address.is_reserved
                            for address in addresses):
        raise ValueError("webhook host resolves to a prohibited network")
    return webhook_url


async def notify_webhook(webhook_url: str, payload: dict) -> None:
    if not webhook_url:
        return
    try:
        safe_url = await validate_webhook_url(webhook_url)
        if urlparse(safe_url).hostname.casefold().endswith("discord.com"):
            data = {
                "embeds": [{
                    "title": f"Paper signal: {payload.get('bot_name', 'Bot')}",
                    "fields": [
                        {"name": "Event", "value": str(payload.get("event_name", "Unknown"))},
                        {"name": "Selection", "value":
                         f"{payload.get('market')} / {payload.get('outcome')}"},
                        {"name": "Action", "value": str(payload.get("action", "WATCH"))},
                    ],
                    "footer": {"text": "Paper research only"},
                }]
            }
        else:
            data = payload
        async with httpx.AsyncClient(follow_redirects=False, timeout=10.0) as client:
            response = await client.post(safe_url, json=data)
            if response.status_code >= 400:
                logger.warning("Webhook delivery failed with HTTP %s", response.status_code)
    except (ValueError, OSError, httpx.HTTPError) as exc:
        logger.warning("Webhook delivery rejected or failed (%s)", type(exc).__name__)
