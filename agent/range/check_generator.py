"""验证字节复现、三种规模的全量解析与显式坏文件清单（兼容大结果落盘降级）。"""
import json
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'mcp_server'))
from generator import generate
from tools import scan_dataset, build_fund_graph
from common import CACHE, DELIVERABLES

def _scan(dataset_dir):
    """scan_dataset 兼容：大结果落盘时返回不含明细，从'完整结果'文件读回。"""
    r = scan_dataset(dataset_dir)
    if '失败清单' not in r.get('证据', {}):
        full = r.get('artifacts', {}).get('完整结果')
        if full and Path(full).is_file():
            r = json.loads(Path(full).read_text(encoding='utf-8'))
    return r

def main():
    output = []
    for size in (30, 100, 500):
        a = generate('a_check_' + str(size), size=size, noise=True, seed=310)
        manifest = Path(a['清单']).read_bytes()
        truth = Path(a['标准答案路径']).read_bytes()
        b = generate('a_check_' + str(size), size=size, noise=True, seed=310)
        assert manifest == Path(b['清单']).read_bytes()
        assert truth == Path(b['标准答案路径']).read_bytes()
        r = _scan(a['数据集目录'])
        assert not r['证据']['失败清单'], r
        g = build_fund_graph(r['结论']['dataset_id'])
        assert g['结论']['节点数'] == size, g
        output.append({'size': size, 'failed': 0, 'byte_reproducible': True, 'graph': g['结论']})
        CACHE.clear()
    a = generate('a_check_bad', quality='missing', seed=310)
    r = _scan(a['数据集目录'])
    assert len(r['证据']['失败清单']) == 2, r
    output.append({'bad_failures': r['证据']['失败清单']})
    s = json.dumps(output, ensure_ascii=False, indent=2)
    (DELIVERABLES / 'phase3_a_check.json').write_text(s, encoding='utf-8')
    print(s)

if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    main()
