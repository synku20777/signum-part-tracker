from __future__ import annotations

from pathlib import Path

import yaml  # type: ignore[import-untyped]  # PyYAML does not ship inline typing.

from irmscher_tracker.domain import (
    MatchResult,
    NegativeRules,
    NormalizedListing,
    PartDefinition,
    PartsConfig,
    ScoringReason,
)
from irmscher_tracker.normalizer import extract_part_numbers, normalize_part_number

ALGORITHM_VERSION = "1.0"

# Scoring weights
EXACT_PART_NUMBER_TITLE = 120
EXACT_PART_NUMBER_DESCRIPTION = 90
IRMSCHER_IN_TITLE = 25
SIGNUM_IN_TITLE = 20
VECTRA_C_IN_TITLE = 8
PART_NAME_ALIAS = 15
FACELIFT_KEYWORD = 10
EXCLUDED_PART_NUMBER = -200
INCOMPATIBLE_MODEL = -75
REPLICA_KEYWORD = -40


class PartMatcher:
    def __init__(self, config_path: str | Path):
        self._config = self._load_config(config_path)
        self._normalized_parts: dict[str, PartDefinition] = {}
        self._part_number_map: dict[str, str] = {}  # normalized number -> part_id
        self._build_indexes()

    @staticmethod
    def _load_config(config_path: str | Path) -> PartsConfig:
        with open(config_path) as f:
            data = yaml.safe_load(f)
        return PartsConfig.model_validate(data)

    def _build_indexes(self) -> None:
        for part in self._config.parts:
            self._normalized_parts[part.id] = part
            for pn in part.part_numbers:
                normalized = normalize_part_number(pn)
                self._part_number_map[normalized] = part.id

    @property
    def parts(self) -> list[PartDefinition]:
        return self._config.parts

    @property
    def negative_rules(self) -> NegativeRules:
        return self._config.negative_rules

    def match(self, listing: NormalizedListing) -> MatchResult | None:
        """Score a listing and refuse ambiguous part assignments."""
        results: list[MatchResult] = []
        for part in self._config.parts:
            result = self._score_against_part(listing, part)
            if result is not None:
                results.append(result)
        if not results:
            return None

        eligible = [result for result in results if result.has_part_specific_evidence]
        if not eligible:
            return max(results, key=lambda result: result.total_score)
        highest = max(result.total_score for result in eligible)
        winners = [result for result in eligible if result.total_score == highest]
        return winners[0] if len(winners) == 1 else None

    def _score_against_part(
        self, listing: NormalizedListing, part: PartDefinition
    ) -> MatchResult | None:
        reasons: list[ScoringReason] = []
        title_lower = listing.title.lower()
        desc_lower = listing.description.lower()
        combined = f"{title_lower} {desc_lower}"

        # Extract part numbers from title and description
        title_numbers = extract_part_numbers(listing.title)
        desc_numbers = extract_part_numbers(listing.description)

        part_normalized = [normalize_part_number(pn) for pn in part.part_numbers]

        # Check for excluded part numbers first (negative override)
        excluded_normalized = [
            normalize_part_number(pn) for pn in self._config.negative_rules.excluded_part_numbers
        ]
        all_numbers = set(title_numbers + desc_numbers)
        for excluded in excluded_normalized:
            if excluded in all_numbers:
                reasons.append(
                    ScoringReason(
                        rule="excluded_part_number",
                        points=EXCLUDED_PART_NUMBER,
                        detail=f"Excluded part number {excluded} found",
                    )
                )

        # Exact part number in title
        for pn in part_normalized:
            if pn in title_numbers:
                reasons.append(
                    ScoringReason(
                        rule="exact_part_number_title",
                        points=EXACT_PART_NUMBER_TITLE,
                        detail=f"Part number {pn} in title",
                    )
                )
                break

        # Exact part number in description
        for pn in part_normalized:
            if pn in desc_numbers:
                reasons.append(
                    ScoringReason(
                        rule="exact_part_number_description",
                        points=EXACT_PART_NUMBER_DESCRIPTION,
                        detail=f"Part number {pn} in description",
                    )
                )
                break

        # Irmscher in title
        if "irmscher" in title_lower:
            reasons.append(
                ScoringReason(
                    rule="irmscher_in_title",
                    points=IRMSCHER_IN_TITLE,
                    detail="'Irmscher' found in title",
                )
            )

        # Signum in title
        if "signum" in title_lower:
            reasons.append(
                ScoringReason(
                    rule="signum_in_title",
                    points=SIGNUM_IN_TITLE,
                    detail="'Signum' found in title",
                )
            )

        # Vectra C in title
        if "vectra" in title_lower and (
            "c" in title_lower.split() or "vectra c" in title_lower or "vectra-c" in title_lower
        ):
            reasons.append(
                ScoringReason(
                    rule="vectra_c_in_title",
                    points=VECTRA_C_IN_TITLE,
                    detail="'Vectra C' found in title",
                )
            )

        # Part name aliases
        all_aliases: list[str] = []
        for lang_aliases in part.aliases.values():
            all_aliases.extend(lang_aliases)
        for alias in all_aliases:
            if alias.lower() in combined:
                reasons.append(
                    ScoringReason(
                        rule="part_name_alias",
                        points=PART_NAME_ALIAS,
                        detail=f"Alias '{alias}' found",
                    )
                )
                break  # Only count once

        # Facelift / MY06 keywords
        facelift_keywords = [
            "facelift",
            "face lift",
            "fl",
            "my06",
            "my2006",
            "mj06",
            "mj2006",
            "2006",
        ]
        for kw in facelift_keywords:
            if kw in combined:
                reasons.append(
                    ScoringReason(
                        rule="facelift_keyword",
                        points=FACELIFT_KEYWORD,
                        detail=f"Facelift keyword '{kw}' found",
                    )
                )
                break

        # Incompatible model check
        for model in self._config.negative_rules.incompatible_models:
            model_lower = model.lower()
            if model_lower in title_lower and "signum" not in title_lower:
                reasons.append(
                    ScoringReason(
                        rule="incompatible_model",
                        points=INCOMPATIBLE_MODEL,
                        detail=f"Incompatible model '{model}' found without Signum",
                    )
                )
                break

        # Replica/style check
        for kw in self._config.negative_rules.excluded_keywords:
            if kw.lower() in combined:
                reasons.append(
                    ScoringReason(
                        rule="replica_keyword",
                        points=REPLICA_KEYWORD,
                        detail=f"Negative keyword '{kw}' found",
                    )
                )
                break

        if not reasons:
            return None

        has_excluded_part = any(r.rule == "excluded_part_number" for r in reasons)
        has_exact = any(
            r.rule in ("exact_part_number_title", "exact_part_number_description") for r in reasons
        )
        has_alias = any(r.rule == "part_name_alias" for r in reasons)
        has_incompatible = any(
            r.rule in ("incompatible_model", "replica_keyword") for r in reasons
        )

        if has_excluded_part or (has_incompatible and not has_exact):
            compatibility_status = "incompatible"
        elif has_exact:
            compatibility_status = "exact"
        elif any(
            r.rule
            in ("part_name_alias", "facelift_keyword", "irmscher_in_title", "signum_in_title")
            for r in reasons
        ):
            compatibility_status = "probable"
        else:
            compatibility_status = "unknown"

        total = sum(r.points for r in reasons)
        return MatchResult(
            part_id=part.id,
            part_name=part.name,
            total_score=total,
            compatibility_status=compatibility_status,
            reasons=reasons,
            has_part_specific_evidence=has_exact or has_alias,
            algorithm_version=ALGORITHM_VERSION,
        )

    def get_search_queries(self) -> list[str]:
        """Generate search queries from parts config."""
        queries: list[str] = [
            "Irmscher Signum",
            "Irmscher Vectra Signum",
            "Opel Signum Irmscher",
        ]
        seen_numbers: set[str] = set()
        for part in self._config.parts:
            for pn in part.part_numbers:
                normalized = normalize_part_number(pn)
                if normalized not in seen_numbers:
                    queries.append(f"Irmscher {normalized}")
                    seen_numbers.add(normalized)
        return queries
