"""fundflow-toolkit MCP stdio 入口。"""
import logging
import os
import subprocess
import sys
import time

# 避免 Windows joblib 物理核心探测启动 PowerShell。
os.environ['LOKY_MAX_CPU_COUNT'] = '1'
from common import log, DELIVERABLES
from mcp.server.fastmcp import FastMCP
from tools import register_all

logging.basicConfig(stream=sys.stderr, level=logging.INFO)

_WARM_CODE = r'''
import json, os, sys, time, traceback
t = time.perf_counter()
m = sys.argv[1]
def st(v):
    tmp = m + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(v, f)
    os.replace(tmp, m)
    print(json.dumps(v), file=sys.stderr, flush=True)
try:
    st({'status': 'running', 'prefix': sys.prefix})
    import numpy
    import sklearn
    from sklearn.cluster import KMeans
    import threadpoolctl
    st({'status': 'imports-ok', 'sklearn_file': sklearn.__file__, 'prefix': sys.prefix})
    with threadpoolctl.threadpool_limits(limits=1):
        KMeans(n_clusters=2, n_init=1, random_state=0).fit([[0., 0.], [0., 1.], [9., 9.], [9., 10.]])
    st({'status': 'done', 'seconds': round(time.perf_counter() - t, 3)})
except Exception as exc:
    st({'status': 'failed', 'error': str(exc)})
    traceback.print_exc(file=sys.stderr)
'''

def _start_prewarm():
    """非阻塞子进程预热，诊断只写文件。"""
    try:
        marker = DELIVERABLES / '_warmup.json'
        marker.parent.mkdir(parents=True, exist_ok=True)
        with open(DELIVERABLES / '_prewarm.log', 'w', encoding='utf-8') as logf:
            subprocess.Popen([sys.executable, '-c', _WARM_CODE, str(marker)],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=logf,
                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    except Exception as exc:
        log(f'子进程预热启动失败（继续运行）：{type(exc).__name__}: {exc}')

def _preload_heavy():
    """主线程预载：必须在任何 anyio 运行环境启动前完成首次加载。"""
    started = time.perf_counter()
    try:
        import numpy
        from sklearn.cluster import KMeans
        import threadpoolctl
        with threadpoolctl.threadpool_limits(limits=1):
            KMeans(n_clusters=2, n_init=1, random_state=0).fit([[0., 0.], [0., 1.], [9., 9.], [9., 10.]])
        log(f'主线程预载完成：{time.perf_counter() - started:.2f}s')
    except Exception as exc:
        log(f'主线程预载失败（继续运行）：{type(exc).__name__}: {exc}')

mcp = FastMCP('fundflow-toolkit')
register_all(mcp)

if __name__ == '__main__':
    _start_prewarm()
    _preload_heavy()
    mcp.run(transport='stdio')
