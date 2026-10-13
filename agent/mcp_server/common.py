"""统一契约、进程内上下文与旧引擎输出隔离。"""
import contextlib
import functools
import json
import logging
import sys
import threading
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DELIVERABLES = PROJECT_ROOT / 'agent' / 'deliverables'
sys.path.insert(0, str(PROJECT_ROOT))
with contextlib.redirect_stdout(sys.stderr):
    import gambling_analysis as ga

LOCK = threading.RLock()
CACHE = {}

def ok(结论=None, 证据=None, 异常=None, artifacts=None):
    return {'结论': 结论 or {}, '证据': 证据 or {}, '异常': 异常 or [], 'artifacts': artifacts or {}}

def log(msg):
    print(msg, file=sys.stderr)

def tool(fn):
    """串行执行旧代码，防止全局 stdout/导出目录修改相互干扰。"""
    @functools.wraps(fn)
    def wrapped(*args, **kwargs):
        with LOCK:
            try:
                with contextlib.redirect_stdout(sys.stderr):
                    return fn(*args, **kwargs)
            except Exception as exc:
                logging.exception('工具 %s 失败', fn.__name__)
                return ok({'成功': False}, 异常=[{'工具': fn.__name__, '类型': type(exc).__name__, '原因': str(exc)}])
    return wrapped

def context(dataset_id):
    if dataset_id not in CACHE:
        raise ValueError('dataset_id 不存在或服务器已重启，请重新 scan_dataset')
    return CACHE[dataset_id]

def folder(dataset_id):
    path = DELIVERABLES / dataset_id
    path.mkdir(parents=True, exist_ok=True)
    return path

def write_json(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2, default=str, allow_nan=False), encoding='utf-8')

def finish(ctx, name, result):
    """完整结果保留在文件中；大表与大体积结果返回摘要。"""
    path = folder(ctx['id']) / (name + '.json')
    result['artifacts']['完整结果'] = str(path)
    write_json(path, result)
    ctx.setdefault('results', {})[name] = result
    summary = json.loads(json.dumps(result, ensure_ascii=False, default=str))
    for key, value in list(summary['结论'].items()):
        if isinstance(value, list) and len(value) > 50:
            summary['结论'][key] = value[:50]
            summary['结论'][key + '总数'] = len(value)
    if len(json.dumps(summary, ensure_ascii=False).encode('utf-8')) > 190000:
        summary = ok({'dataset_id': ctx['id'], '说明': '结果超过返回上限，请读取完整结果文件'},
                     {'完整结果': str(path)}, ['大结果已落盘，完整异常见文件'], result['artifacts'])
    return summary

def observe(method, callback, events=('return',), args=(), kwargs=None):
    """只观察指定旧方法的局部变量，不复制算法、不改变其执行结果。"""
    code = method.__func__.__code__
    previous = sys.gettrace()
    def trace(frame, event, arg):
        if frame.f_code is code:
            if event in events:
                callback(event, frame.f_locals)
            return trace
        return None
    try:
        sys.settrace(trace)
        return method(*args, **(kwargs or {}))
    finally:
        sys.settrace(previous)
