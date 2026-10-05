# -*- coding: utf-8 -*-
"""进程内事件总线：坐席广播房 + 每会话访客房。

单进程 uvicorn 部署；未来多进程时可替换为 Redis pub/sub，接口保持不变。
"""
from __future__ import annotations

import asyncio
from collections import defaultdict
from typing import Any


class EventBus:
    def __init__(self) -> None:
        self._rooms: dict[str, set[asyncio.Queue]] = defaultdict(set)

    def subscribe(self, room: str) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=200)
        self._rooms[room].add(q)
        return q

    def unsubscribe(self, room: str, q: asyncio.Queue) -> None:
        self._rooms.get(room, set()).discard(q)

    def publish(self, room: str, event: dict[str, Any]) -> None:
        for q in list(self._rooms.get(room, set())):
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                # 慢消费者丢弃最旧事件，SSE 端可用消息列表接口补偿。
                try:
                    q.get_nowait()
                    q.put_nowait(event)
                except Exception:
                    pass

    @staticmethod
    def conv_room(conv_id: int) -> str:
        return f"conv:{conv_id}"

    AGENTS_ROOM = "agents"


bus = EventBus()
