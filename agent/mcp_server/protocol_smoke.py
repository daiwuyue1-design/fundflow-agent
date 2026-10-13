"""真实 stdio 握手与缓存调用测试，不冒充 AGH 会话验证。"""
import asyncio
import json
import sys
import time
from pathlib import Path
import tempfile
from datetime import timedelta
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from common import DELIVERABLES, PROJECT_ROOT
from fixtures import generate

async def main():
    data = generate()
    marker = DELIVERABLES / '_warmup.json'
    marker.unlink(missing_ok=True)
    (DELIVERABLES / 'protocol_output.txt').unlink(missing_ok=True)
    started = time.time()
    params = StdioServerParameters(command=sys.executable, args=[str(Path(__file__).with_name('server.py'))])
    timings = {}
    with open(DELIVERABLES / 'protocol_stderr.txt', 'w', encoding='utf-8') as errlog:
        async with stdio_client(params, errlog=errlog) as (read, write):
            async with ClientSession(read, write, read_timeout_seconds=timedelta(seconds=60)) as session:
                await session.initialize()
                deadline = time.monotonic() + 120
                state = {}
                while time.monotonic() < deadline:
                    if marker.exists() and marker.stat().st_mtime > started:
                        state = json.loads(marker.read_text(encoding='utf-8'))
                        if state.get('status') == 'done':
                            break
                        if state.get('status') == 'failed':
                            raise RuntimeError(f'预热失败：{state}')
                    await asyncio.sleep(2)
                else:
                    raise RuntimeError(f'预热等待超时（120秒）；最新状态：{state}；标记：{marker}')
                listing = await session.list_tools()
                names = [t.name for t in listing.tools]
                assert set(names) == {'scan_dataset', 'build_fund_graph', 'classify_tiers', 'rank_suspects', 'export_evidence',
                                      'detect_laundering', 'trace_funds', 'profile_account', 'scan_structure', 'simulate_case', 'anonymize_dataset'}
                simulated = await session.call_tool('simulate_case', {'name': 'protocol_simulate', 'size': 30, 'seed': 731})
                assert not simulated.isError
                generated = json.loads(simulated.content[0].text)
                assert generated['结论']['成功'] is True and Path(generated['artifacts']['标准答案路径']).exists()
                with tempfile.TemporaryDirectory(prefix='protocol_anon_') as anon:
                    key = Path(anon) / 'key.txt'; out = Path(anon) / 'out'; key.write_text('synthetic-test-key', encoding='utf-8')
                    anonymized = await session.call_tool('anonymize_dataset', {'input_dir': str(data), 'output_dir': str(out), 'key_file': str(key)})
                    assert not anonymized.isError
                    anon_value = json.loads(anonymized.content[0].text)
                    assert anon_value['结论']['成功'] is True and anon_value['artifacts']['输出目录'] == str(out.resolve())
                clock = time.perf_counter()
                result = await session.call_tool('scan_dataset', {'folder_path': str(data)})
                timings['scan_dataset'] = time.perf_counter() - clock
                assert not result.isError
                scan = json.loads(result.content[0].text)
                did = scan['结论']['dataset_id']
                assert set(scan) == {'结论', '证据', '异常', 'artifacts'}
                for name in ('build_fund_graph', 'classify_tiers', 'detect_laundering', 'scan_structure', 'rank_suspects'):
                    clock = time.perf_counter()
                    call = await session.call_tool(name, {'dataset_id': did})
                    timings[name] = time.perf_counter() - clock
                    assert not call.isError
                    value = json.loads(call.content[0].text)
                    assert value['结论'].get('成功') is not False, value
                for name, arguments in [('profile_account', {'dataset_id': did, 'account_id': 'synthetic_receiver'}),
                                        ('trace_funds', {'dataset_id': did}), ('export_evidence', {'dataset_id': did})]:
                    call = await session.call_tool(name, arguments)
                    assert not call.isError, call
                    value = json.loads(call.content[0].text)
                    assert value['结论'].get('成功') is not False, value
    assert timings['classify_tiers'] < 60, timings
    preload = (DELIVERABLES / 'protocol_stderr.txt').read_text(encoding='utf-8')
    assert '主线程预载完成' in preload, preload
    prewarm = (DELIVERABLES / '_prewarm.log').read_text(encoding='utf-8')
    imports = [json.loads(line) for line in prewarm.splitlines() if line.startswith('{')]
    source = next(record['sklearn_file'] for record in imports if record.get('status') == 'imports-ok')
    assert Path(source).resolve().is_relative_to(Path(sys.prefix).resolve()), source
    output = ('PASS 子进程预热：' + json.dumps(state, ensure_ascii=False)
              + '\nPASS 每工具耗时（秒）：' + json.dumps(timings)
              + '\nPASS classify 首调耗时：' + str(timings['classify_tiers']) + '秒'
              + '\nPASS stdio initialize/list_tools：' + ', '.join(names)
              + '\nPASS stdio 十一工具：simulate_case生成与anonymize_dataset脱敏成功；九工具全链跨调用dataset_id缓存有效，JSON协议未受日志污染\nPROTOCOL ALL PASS\n')
    (DELIVERABLES / 'protocol_output.txt').write_text(output, encoding='utf-8')
    print(output, end='')

if __name__ == '__main__':
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    try:
        asyncio.run(main())
    except Exception:
        import traceback
        (DELIVERABLES / 'protocol_output.txt').write_text('PROTOCOL FAIL\n' + traceback.format_exc(), encoding='utf-8')
        raise
