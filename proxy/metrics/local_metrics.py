"""
The current heartbeat loop is:
    await client.heartbeat(proxy_id=PROXY_ID)
After pool-level resource statistics are added here later, only extend it to:
    snap = metrics.snapshot()
    await client.heartbeat(
        proxy_id=PROXY_ID,
        inflight=snap["inflight"],
        qps_1m=snap["qps_1m"],
        gpu_util=snap.get("gpu_util"),
)
"""
class ProxyMetrics:
    def inc_inflight(self): ...
    def dec_inflight(self): ...
    def snapshot(self) -> dict:
        return {
            "inflight": ...,
            "qps_1m": ...,
            # add gpu_util later
        }

