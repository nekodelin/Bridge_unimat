import asyncio
from typing import Any

from fastapi import WebSocket


class WebSocketBroadcaster:
    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._clients: set[WebSocket] = set()

    async def connect(self, websocket: WebSocket) -> int:
        await websocket.accept()
        async with self._lock:
            self._clients.add(websocket)
            return len(self._clients)

    async def disconnect(self, websocket: WebSocket) -> int:
        async with self._lock:
            self._clients.discard(websocket)
            return len(self._clients)

    async def client_count(self) -> int:
        async with self._lock:
            return len(self._clients)

    async def broadcast(self, payload: dict[str, Any]) -> None:
        async with self._lock:
            clients = list(self._clients)

        stale: list[WebSocket] = []
        for client in clients:
            try:
                await client.send_json(payload)
            except Exception:
                stale.append(client)

        if not stale:
            return
        async with self._lock:
            for client in stale:
                self._clients.discard(client)
