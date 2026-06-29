"""Strategy filters (A1) ported to the Python engine — pre-registered, default OFF.

Only the gating logic is unit-tested here; whether the filters actually help is a
held-out, multiplicity-corrected question answered by scripts/eval_strategy_filters.py,
never by an assertion on in-sample CLV."""

from types import SimpleNamespace

from wc_kalshi.engine.builders import build_runtime
from wc_kalshi.engine.match_loop import TickProcessor
from wc_kalshi.models.schemas import OrderAction


def _proc(cfg):
    return TickProcessor(build_runtime(cfg), trade=True, persist=False)


def test_filters_default_off(cfg, match_factory):
    assert cfg.strategy.sell_only is False
    assert cfg.strategy.disable_buys is False
    assert cfg.strategy.max_entry_minute is None
    proc = _proc(cfg.model_copy(deep=True))
    m = match_factory(match_id="m", minute=85)
    # Nothing is filtered by default — a late BUY still passes.
    assert proc._passes_strategy_filter(SimpleNamespace(action=OrderAction.BUY), m) is True


def test_sell_only_drops_buys(cfg, match_factory):
    c = cfg.model_copy(deep=True)
    c.strategy.sell_only = True
    proc = _proc(c)
    m = match_factory(match_id="m", minute=30)
    assert proc._passes_strategy_filter(SimpleNamespace(action=OrderAction.BUY), m) is False
    assert proc._passes_strategy_filter(SimpleNamespace(action=OrderAction.SELL), m) is True


def test_max_entry_minute_drops_late_entries(cfg, match_factory):
    c = cfg.model_copy(deep=True)
    c.strategy.max_entry_minute = 70
    proc = _proc(c)
    sell = SimpleNamespace(action=OrderAction.SELL)
    assert proc._passes_strategy_filter(sell, match_factory(match_id="m", minute=80)) is False
    assert proc._passes_strategy_filter(sell, match_factory(match_id="m", minute=70)) is True


def test_disable_buys(cfg, match_factory):
    c = cfg.model_copy(deep=True)
    c.strategy.disable_buys = True
    proc = _proc(c)
    m = match_factory(match_id="m", minute=30)
    assert proc._passes_strategy_filter(SimpleNamespace(action=OrderAction.BUY), m) is False
    assert proc._passes_strategy_filter(SimpleNamespace(action=OrderAction.SELL), m) is True
