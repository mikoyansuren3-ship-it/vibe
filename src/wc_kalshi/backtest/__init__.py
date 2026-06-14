"""Backtest / replay harness: evaluate the strategy on stored or synthetic data."""

from .replay import BacktestResult, Backtester

__all__ = ["Backtester", "BacktestResult"]
