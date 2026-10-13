"""第11工具：对合成/授权分享账单执行脱敏写出。"""
from pathlib import Path
import sys
from common import ok, tool
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from tools_anonymize import anonymize_directory

@tool
def anonymize_dataset(input_dir: str, output_dir: str, key_file: str) -> dict:
    """写文件并输出脱敏清单；密钥只从文件读取，不落盘、打印、日志或返回；仅供脱敏分享。"""
    result = anonymize_directory(input_dir, output_dir, key_file)
    failures = [x for x in result['文件清单'] if x['状态'] == '失败']
    return ok({'成功': True, '输出文件数': result['输出文件数'], '文件失败数': len(failures)},
              {'文件清单': result['文件清单'], '识别列': result['识别列'], '策略': result['策略']},
              failures, {'输出目录': str(Path(output_dir).resolve()), '清单路径': result['清单路径'], '计数摘要': {'成功':result['输出文件数'],'失败':len(failures),'跳过':sum(x['状态']=='跳过' for x in result['文件清单'])}})
