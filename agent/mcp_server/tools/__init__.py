"""十一个独立动作的注册表。"""
from .scan_dataset import scan_dataset
from .build_fund_graph import build_fund_graph
from .classify_tiers import classify_tiers
from .rank_suspects import rank_suspects
from .export_evidence import export_evidence
from .detect_laundering import detect_laundering
from .trace_funds import trace_funds
from .profile_account import profile_account
from .scan_structure import scan_structure
from .simulate_case import simulate_case
from .anonymize_dataset import anonymize_dataset

def register_all(mcp):
    import functools
    import inspect
    import anyio
    def asynchronous(fn):
        @functools.wraps(fn)
        async def run(**kwargs):
            return await anyio.to_thread.run_sync(functools.partial(fn, **kwargs))
        run.__signature__ = inspect.signature(fn)
        return run
    for fn in (scan_dataset, build_fund_graph, classify_tiers, rank_suspects, export_evidence,
               detect_laundering, trace_funds, profile_account, scan_structure, simulate_case, anonymize_dataset):
        mcp.tool()(asynchronous(fn))
