"""第十工具：仅生成合成场景，不运行评测。"""
from pathlib import Path
import sys
from common import ok, tool
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from range.generator import generate, load_scenario


@tool
def simulate_case(scenario_name: str | None = None, name: str | None = None,
                  size: int | None = None, noise: bool | None = None,
                  quality: str | None = None, seed: int | None = None) -> dict:
    """生成合成xlsx账单、独立标准答案与统计摘要（写文件）。传入已有场景名，或内联name/size(30/100/500)/noise/quality(full/missing)/seed；两种方式互斥。本动作不评测；同名重跑替换该生成器管理的文件。"""
    if scenario_name is not None:
        if any(v is not None for v in (name, size, noise, quality, seed)):
            raise ValueError('场景名与内联参数不能混用')
        params = load_scenario(scenario_name)
    else:
        params = dict(name=name if name is not None else 'inline_demo',
                      size=size if size is not None else 30, noise=noise if noise is not None else False,
                      quality=quality if quality is not None else 'full', seed=seed if seed is not None else 1)
    artifacts = generate(**params)
    return ok({'成功': True, '场景名': params['name'], '动作': '仅生成合成数据与独立标准答案'},
              {'参数': params, '统计摘要': artifacts['统计摘要'], '标准答案来源': '生成时角色预设，不从工具预测反推'},
              artifacts['统计摘要']['预期失败'], artifacts)
