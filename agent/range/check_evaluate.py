"""指标失败隔离与零分母验证，不改动冻结参数。"""
import json
from evaluate import metrics, evaluate, DELIVERABLES
import evaluate as module

def main():
    rows = [{'true_tier': '赌客线索', 'pred_tier': None, 'true_laundering': True, 'pred_laundering': None},
            {'true_tier': '赌客线索', 'pred_tier': '赌客线索', 'true_laundering': False, 'pred_laundering': False}]
    m = metrics(rows)
    assert m['tier_valid'] == 1 and m['tier_failed'] == 1
    assert m['laundering_valid'] == 1 and m['laundering_failed'] == 1
    assert m['FN'] == 0 and m['laundering_recall'] == '不适用'
    assert metrics([])['tier_accuracy'] == '不适用'
    original = module.scan_dataset
    try:
        module.scan_dataset = lambda *a, **k: {'结论': {'成功': False}, '证据': {}, '异常': ['合成注入体检失败'], 'artifacts': {}}
        r, samples = evaluate(dict(name='failure_check', size=30, seed=19, noise=False, quality='full', split='dev'),
                              {'version': 'failure-check', 'thresholds': {}})
        assert r['status'] == '失败' and r['laundering_failed'] == 30
        assert r['laundering_valid'] == 0 and r['FN'] == 0 and r['TN'] == 0
        assert r['failures'] and len(samples) == 30
    finally:
        module.scan_dataset = original
    output = 'PASS 零分母标不适用；缺失预测另列且不当阴性；体检失败保留30个失败样本\n'
    (DELIVERABLES / 'phase3_metrics_check.txt').write_text(output, encoding='utf-8')
    print(output)

if __name__ == '__main__':
    import sys
    sys.stdout.reconfigure(encoding='utf-8')
    main()
