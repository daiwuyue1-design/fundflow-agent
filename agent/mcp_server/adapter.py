"""旧引擎的可观察适配：参数覆盖、规则轨迹、评分构成。"""
import math
from common import ga, observe

LEVELS = {1: '一级代理嫌疑', 2: '二级代理嫌疑', 3: '三级代理嫌疑', 4: '赌客线索', 5: '待定外围'}
THRESHOLD_KEYS = ('p50_in', 'p70_in', 'p80_in', 'p90_in', 'p95_in', 'p99_in',
                  'p70_out_d', 'p80_out_d', 'p90_out_d', 'p80_in_d')

def validate_thresholds(values):
    for key, value in values.items():
        if key not in THRESHOLD_KEYS:
            raise ValueError(f'未知阈值 {key}；支持：{THRESHOLD_KEYS}')
        if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value) or value < 0:
            raise ValueError(f'{key} 必须为有限非负数')

class EvidenceAnalyzer(ga.GamblingAnalyzer):
    """默认行为直接继承，仅增加可观测字段。"""
    def __init__(self, transactions, identity):
        super().__init__(transactions, identity)
        self.overrides = {}
        self.rules = {}
        self.cluster_evidence = {}
        self.score_parts = {}

    def _compute_percentiles(self):
        values = super()._compute_percentiles()
        values.update(self.overrides)
        for keys in (THRESHOLD_KEYS[:6], THRESHOLD_KEYS[6:9]):
            if any(values[a] > values[b] for a, b in zip(keys, keys[1:])):
                raise ValueError('覆盖后阈值必须按百分位单调递增；请同时覆盖相邻门槛')
        self.parameters = {k: float(values[k]) for k in THRESHOLD_KEYS}
        return values

    def _record_rule(self, method, label, info, *args):
        before = info['level']
        method(info, *args)
        node = self._info_nodes[id(info)]
        self.rules.setdefault(node, []).append({'规则': label, '之前': before, '之后': info['level']})

    def _classify_gambler(self, info, *args):
        self._record_rule(super()._classify_gambler, '赌客修正：净流出、低度数、纯充值或返奖特征', info, *args)

    def _classify_levels(self):
        self._info_nodes = {id(v): n for n, v in self.nodes.items()}
        return super()._classify_levels()

    def _kmeans_clustering(self, X, node_list):
        # 本机 stdio 子进程的 OpenMP 线程过度调度会使微型聚类超时；限制计算线程不改算法参数。
        from sklearn.cluster import KMeans
        import os
        os.environ['LOKY_MAX_CPU_COUNT'] = '1'
        from threadpoolctl import threadpool_limits
        with threadpool_limits(limits=1):
            return super()._kmeans_clustering(X, node_list)

    def _classify_agent(self, info, *args):
        self._record_rule(super()._classify_agent, '代理加强：出度≥p80且流入≥p70；出度≥p90且流入≥p80；或入度≥p80且流入≥p70', info, *args)

    def _validate_level1_agent(self, info, *args):
        self._record_rule(super()._validate_level1_agent, '一级收紧：流入≥p95且出度≥p80或入度≥max(p80出度,3)', info, *args)

    def _apply_rule_corrections(self, *args):
        super()._apply_rule_corrections(*args)
        for n, v in self.nodes.items():
            self.rules.setdefault(n, []).append({'规则': '外围修正：总流量<1000且出入度≤1', '之后': v['level']})

    def _fix_topology_consistency(self):
        before = {n: v['level'] for n, v in self.nodes.items()}
        super()._fix_topology_consistency()
        for n, v in self.nodes.items():
            self.rules.setdefault(n, []).append({'规则': '拓扑一致性修正（最多三轮）', '之前': before[n], '之后': v['level'],
                                                  '上游最终层级': {str(p): self.nodes[p]['level'] for p in self.G.predecessors(n)}})

    def _assign_levels_from_clusters(self, X, labels, nodes, k):
        super()._assign_levels_from_clusters(X, labels, nodes, k)
        stats = []
        import numpy as np
        for cl in sorted(set(labels)):
            arr = X[labels == cl]
            score = float(np.median(arr[:, 0]) * .5 + np.median(arr[:, 2]) * .5 - np.median(arr[:, 6]) * .3)
            stats.append({'簇': int(cl), '节点数': len(arr), '代理得分': score})
        self.cluster_evidence = {'k': k, '簇统计': stats}
        for n, cl in zip(nodes, labels):
            self.rules[n] = [{'规则': '九维特征标准化 K-Means，按簇代理得分排序映射初始层级', '簇': int(cl), '之后': self.nodes[n]['level']}]

    def score_with_evidence(self):
        def capture(event, local):
            if event == 'line' and 'credit_score' in local and 'flow_score_raw' in local:
                n = local['node']
                info = local['info']
                kw = local['kw']
                parts = {'资金量': 30 * local['flow_score_raw'],
                         '层级': local['level_score'].get(info['level'], 2),
                         '洗钱附加': min(local['launder_sc'] * .15, 15),
                         '网络中心度': 20 * local['degree_ratio'] ** .7,
                         '关键词': 8 + 7 * min(kw / local['max_kw'], 1) if kw > 0 else 0,
                         '夜间': 5 * local['night_ratio'], '爆发': 5 if str(n) in local['burst_nodes'] else 0,
                         '信用及单向附加': min(local['credit_score'], 15)}
                self.score_parts[n] = parts
        observe(super()._score_suspects, capture, events=('line',))
