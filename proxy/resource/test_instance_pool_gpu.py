import importlib.util
import sys
import types
from pathlib import Path

proxy_mod = types.ModuleType('proxy')
strategy_mod = types.ModuleType('proxy.strategy')
least_load_mod = types.ModuleType('proxy.strategy.least_load')
class LeastLoadStrategy:
    def compute_score(self, *args, **kwargs):
        return {}
least_load_mod.LeastLoadStrategy = LeastLoadStrategy
sys.modules.setdefault('proxy', proxy_mod)
sys.modules.setdefault('proxy.strategy', strategy_mod)
sys.modules.setdefault('proxy.strategy.least_load', least_load_mod)

module_path = Path(__file__).resolve().with_name('instance_pool.py')
spec = importlib.util.spec_from_file_location('instance_pool_under_test', module_path)
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)
_as_float = module._as_float
_resource_from_snapshot = module._resource_from_snapshot


def test_strict_float_and_gpu_normalization():
    assert _as_float('0') == 0.0
    assert _as_float('[Not Supported]') is None
    assert _as_float('N/A') is None
    assert _as_float({}) is None
    snap = {
        'timestamp_ms': 1000,
        'devices': {
            'gpu': [
                {'utilization_pct_avg': 0, 'utilization_pct_current': 5, 'utilization_pct_max': 10, 'utilization_sample_ok': True, 'utilization_sample_count': 2, 'utilization_window_ms': 5000, 'utilization_sample_timestamp_ms': 900, 'utilization_source': 'nvidia-smi', 'memory_used_mb': 1, 'memory_total_mb': 10},
                {'utilization_pct': '[Not Supported]', 'utilization_sample_ok': False, 'gpu_util': 99, 'memory_used_mb': 2, 'memory_total_mb': 20},
            ],
        },
    }
    resource = _resource_from_snapshot(snap, reported_at=1, metadata={})
    assert resource.gpu_util_avg == 0.0
    assert resource.gpu_util_current == 5.0
    assert resource.gpu_util_max == 10.0
    assert resource.gpu_sample_count == 2
    assert resource.gpu_sample_window_ms == 5000
    assert resource.gpu_sample_source == 'nvidia-smi'
    assert resource.gpu_sample_quality == 'ok'
    assert resource.gpu_mem_used_mb == 3.0
    assert resource.gpu_mem_total_mb == 30.0


if __name__ == '__main__':
    test_strict_float_and_gpu_normalization()
    print('instance_pool gpu tests passed')
