"""Data ingestion: football match feeds + Kalshi market feeds.

Each family hides behind an interface and normalizes to the shared schemas in
``wc_kalshi.models.schemas`` so downstream stages never see provider details.
"""
