"""合成动作契约、具名/内联参数、无stdout污染和错误边界。"""
import contextlib
import io
import json
from pathlib import Path
import sys
from common import DELIVERABLES, CACHE
from tools import simulate_case, scan_dataset

def main():
    captured = io.StringIO()
    before = set(CACHE)
    with contextlib.redirect_stdout(captured):
        a = simulate_case(name='p3_smoke', size=30, noise=True, quality='full', seed=711)
        assert set(a) == {'结论', '证据', '异常', 'artifacts'} and a['结论']['成功']
        manifest = Path(a['artifacts']['清单']).read_bytes()
        b = simulate_case(name='p3_smoke', size=30, noise=True, quality='full', seed=711)
        assert manifest == Path(b['artifacts']['清单']).read_bytes()
        assert set(CACHE) == before, '生成动作不能隐含建图/评测'
        scan = scan_dataset(a['artifacts']['数据集目录'])
        assert not scan['证据']['失败清单']
        c = simulate_case(scenario_name='s30_noise_missing_dev')
        assert len(c['异常']) == 2
        for kwargs in ({'name': '../escape'}, {'name': 'invalid', 'size': 31},
                       {'scenario_name': 'unknown'}, {'scenario_name': 's30_clean_full_dev', 'seed': 1}):
            assert simulate_case(**kwargs)['结论']['成功'] is False
    assert captured.getvalue() == ''
    output = 'PASS simulate_case具名/内联生成、三段式契约、字节复现、坏数据预期、仅生成、错误显式与stdout纪律\nP3 ALL PASS\n'
    (DELIVERABLES / 'phase3_simulate_output.txt').write_text(output, encoding='utf-8')
    print(output)

if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    main()
