"""最小装配探针：原文记录stdin，放行；只用于合成验证。"""
import json
from pathlib import Path
import sys

def main():
    raw = sys.stdin.buffer.read().decode('utf-8')
    payload = json.loads(raw)
    if not isinstance(payload, dict):
        raise ValueError('探针stdin必须为JSON对象')
    root = Path(__file__).resolve().parents[2]
    runtime = root / 'agent/deliverables'
    runtime.mkdir(parents=True, exist_ok=True)
    with (runtime / 'hooks_probe.log').open('a', encoding='utf-8') as stream:
        stream.write(raw + '\n')
    print('{}')
    return 0

if __name__ == '__main__':
    raise SystemExit(main())
