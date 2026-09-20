"""Modular building blocks for the QDII ranking pipeline.

The legacy ``update_qdii_ranking`` module remains the public compatibility
surface while the implementation is migrated into this package incrementally.
"""

from .contracts import RANKING_SCHEMA_VERSION

__all__ = ["RANKING_SCHEMA_VERSION"]
