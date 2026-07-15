# scheduler/resource/proxy_pool.py
# -*- coding: utf-8 -*-
"""
Proxy 资源池：Scheduler 控制平面的“状态模型层”。

设计目的：
- control_plane.py 只负责 HTTP 接入、参数校验、把状态写进池
- 调度策略（未来的 CacheRoute）只读这个池来选择 proxy
- 将“状态模型”和“HTTP/协议层”解耦，避免后续策略改动牵一发动全身

当前实现是“内存版”（单进程单 worker），足够验证功能。
后续如果要做分布式（多 scheduler 实例共享 proxy 状态），可替换该文件实现为
Redis/etcd/SQL 等，而不用改 control_plane API 与调度代码。
"""

from __future__ import annotations

import time
import asyncio
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class ProxyLoad:
    """
    Proxy 的“负载信息”结构体（后续可扩充）。

    说明：
    - 负载字段不应“拍脑袋”定义太多；先给出最通用的占位字段。
    - 后续如果 proxy 可以周期性上报统计信息（如 inflight/qps/gpu_util/kv_hit），
      只需要在这里加字段，再在 control_plane 的 heartbeat 更新即可。
    """
    # ---- static capability (register-time) ----
    max_capacity: int = 0      # 最大处理能力（注册时上报，生命周期内不变）

    instance_count: int = 0     # proxy管理的实例数量（注册上报）
    kv_mem_per_instance_gb: float = 0.0  # 单实例KV内存大小（GB）（注册上报）
    kv_cache_pool_gb: float = 0.0  # instance_count * kv_mem_per_instance_gb（scheduler计算）

    # ---- dynamic load (heartbeat) ----
    inflight: int = 0          # 正在处理的请求数（或 decode session 数）
    qps_1m: float = 0.0        # 最近 1 分钟 QPS（proxy 上报值）
    gpu_util: float = 0.0      # GPU 利用率（0-100 或 0-1，由你统一约定）


@dataclass
class ProxyInfo:
    """
    Proxy 的“静态 + 动态”信息结构体。

    静态信息（注册时给）：
    - proxy_id/host/port/endpoints/tags/weight/meta

    动态信息（心跳/监控更新）：
    - load/last_seen_at

    备注：
    - endpoints 约定采用 OpenAI 风格 path 片段：["chat/completions","completions"]
    - meta 放不方便结构化的扩展字段（字典），例如版本号/机型/TP size 等
    """
    proxy_id: str
    host: str
    port: int
    endpoints: List[str] = field(default_factory=list)

    tags: List[str] = field(default_factory=list)
    weight: float = 1.0
    meta: Dict[str, Any] = field(default_factory=dict)
    kv_cache_update_policy: str = "lru"

    # 负载信息，后续调度策略会用
    load: ProxyLoad = field(default_factory=ProxyLoad)

    # 时间戳：注册时间、最后心跳时间
    registered_at: float = field(default_factory=lambda: time.time())
    last_seen_at: float = field(default_factory=lambda: time.time())
    pool_resource: Dict[str, Any] = field(default_factory=dict)
    pool_resource_reported_at: Optional[float] = None

    def touch(self) -> None:
        """收到心跳/更新时刷新 last_seen_at。"""
        self.last_seen_at = time.time()

    def is_alive(self, ttl_s: int, now: Optional[float] = None) -> bool:
        """
        根据 TTL 判断 proxy 是否存活。
        - ttl_s: 超过 ttl_s 没心跳就判定失活
        - now: 可选，用于批量判断时减少多次 time.time() 调用
        """
        now = now or time.time()
        return (now - self.last_seen_at) <= ttl_s


class ProxyPool:
    """
    Proxy 资源池（内存版）。

    并发模型：
    - control_plane 的 register/heartbeat/unregister 会并发访问
    - scheduler 的数据平面（调度逻辑）会并发 list/get
    - 所有读写通过 asyncio.Lock 串行化，避免状态竞争

    注意：
    - 这是单进程内存结构。如果你开多 worker，会出现“每个进程一份池”。
      你目前的设计（7002 embedded server）也要求单进程单 worker，否则端口冲突。
    """
    def __init__(self, ttl_s: int = 30):
        self.ttl_s = ttl_s
        self._lock = asyncio.Lock()
        self._data: Dict[str, ProxyInfo] = {}

    async def upsert(self, info: ProxyInfo) -> None:
        """
        注册/更新一个 proxy（幂等 upsert）。

        行为约定：
        - 若首次出现 proxy_id：直接插入
        - 若已存在同 proxy_id：更新所有字段，但保留原 registered_at（表示首次注册时间）
        """
        async with self._lock:
            old = self._data.get(info.proxy_id)
            if old is None:
                self._data[info.proxy_id] = info
                return

            # 保留首次注册时间，其他字段以最新为准
            info.registered_at = old.registered_at
            if not info.pool_resource and old.pool_resource:
                info.pool_resource = old.pool_resource
                info.pool_resource_reported_at = old.pool_resource_reported_at
            self._data[info.proxy_id] = info

    async def heartbeat(
        self,
        proxy_id: str,
        load: Optional[ProxyLoad] = None,
        meta_patch: Optional[Dict[str, Any]] = None,
        pool_resource: Optional[Dict[str, Any]] = None,
    ) -> bool:
        """
        proxy 心跳。
        - 成功：刷新 last_seen_at；如果提供 load，则一并更新
        - 失败：proxy_id 不存在，返回 False
        """
        async with self._lock:
            p = self._data.get(proxy_id)
            if not p:
                return False

            p.touch()
            if load is not None:
                p.load.inflight = int(load.inflight)
                p.load.qps_1m = float(load.qps_1m)
                p.load.gpu_util = float(load.gpu_util)
            if meta_patch:
                p.meta.update(dict(meta_patch))
            if pool_resource is not None:
                p.pool_resource = dict(pool_resource)
                p.pool_resource_reported_at = time.time()
            return True

    async def remove(self, proxy_id: str) -> None:
        """注销一个 proxy（不存在也不报错）。"""
        async with self._lock:
            self._data.pop(proxy_id, None)

    async def get(self, proxy_id: str) -> Optional[ProxyInfo]:
        """获取单个 proxy 信息（可能返回 None）。"""
        async with self._lock:
            return self._data.get(proxy_id)

    async def list(self, include_dead: bool = False) -> List[ProxyInfo]:
        """
        列出 proxy 列表。
        - include_dead=False：只返回存活的 proxy
        - include_dead=True：返回全部（含失活）
        """
        async with self._lock:
            now = time.time()
            out: List[ProxyInfo] = []
            for p in self._data.values():
                alive = p.is_alive(self.ttl_s, now=now)
                if (not include_dead) and (not alive):
                    continue
                out.append(p)

            # 输出顺序稳定：按最近心跳时间排序（最新的在前）
            out.sort(key=lambda x: x.last_seen_at, reverse=True)
            return out

    async def inflight_delta(self, proxy_id: str, delta: int) -> bool:
        async with self._lock:
            p = self._data.get(proxy_id)
            if not p:
                return False
            v = int(p.load.inflight) + int(delta)
            if v < 0:
                v = 0
            p.load.inflight = v
            return True
