"""Cached eBay Taxonomy lookups for category-scoped discovery.

Category filters are only used on broad/bundle searches. Exact-model searches
stay unrestricted so a wrong category assignment cannot hide a deal.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Callable

from src.ebay.client import EbayClient, EbayError
from src.storage.repository import Repository

log = logging.getLogger(__name__)


class TaxonomyService:
    def __init__(
        self,
        client: EbayClient,
        repository: Repository,
        *,
        cache_days: int = 30,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._client = client
        self._repo = repository
        self._cache_days = cache_days
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def category_for(self, marketplace: str, hint: str | None) -> str | None:
        if not hint:
            return None
        now = self._clock()
        cached = self._repo.get_taxonomy_category(
            marketplace, hint, max_age_days=self._cache_days, now=now
        )
        if cached is not None:
            return str(cached["category_id"])
        try:
            tree_id = self._client.taxonomy_tree_id(marketplace)
            suggestions = self._client.taxonomy_suggestions(tree_id, hint)
        except EbayError as exc:
            log.warning(
                "taxonomy lookup failed; continuing without category filter",
                extra={"event": "taxonomy_error", "marketplace": marketplace, "detail": str(exc)},
            )
            return None
        for suggestion in suggestions:
            category = suggestion.get("category") if isinstance(suggestion, dict) else None
            if not isinstance(category, dict):
                continue
            category_id = category.get("categoryId")
            if category_id:
                self._repo.save_taxonomy_category(
                    marketplace=marketplace,
                    hint=hint,
                    category_id=str(category_id),
                    category_name=str(category.get("categoryName") or "") or None,
                    tree_id=tree_id,
                    now=now,
                )
                return str(category_id)
        log.warning(
            "no taxonomy category suggestion",
            extra={"event": "taxonomy_empty", "marketplace": marketplace, "hint": hint},
        )
        return None
