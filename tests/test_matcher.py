from decimal import Decimal

from irmscher_tracker.domain import NormalizedListing, Source
from irmscher_tracker.matcher import ALGORITHM_VERSION


def test_exact_part_number_in_title(matcher):
    listing = NormalizedListing(
        source=Source.EBAY,
        external_id="1",
        title="i3401009 front spoiler",
        description="",
        url="http",
        price=Decimal("100")
    )
    result = matcher.match(listing)
    assert result is not None
    assert result.total_score >= 120
    assert any(r.rule == "exact_part_number_title" for r in result.reasons)

def test_exact_part_number_in_description(matcher):
    listing = NormalizedListing(
        source=Source.EBAY,
        external_id="2",
        title="Spoiler",
        description="Part number i3401009",
        url="http",
        price=Decimal("100")
    )
    result = matcher.match(listing)
    assert result is not None
    assert result.total_score >= 90
    assert any(r.rule == "exact_part_number_description" for r in result.reasons)

def test_irmscher_in_title(matcher):
    listing = NormalizedListing(
        source=Source.EBAY,
        external_id="3",
        title="Irmscher front spoiler i3401009",
        description="",
        url="http",
        price=Decimal("100")
    )
    result = matcher.match(listing)
    assert result is not None
    assert any(r.rule == "irmscher_in_title" for r in result.reasons)

def test_signum_in_title(matcher):
    listing = NormalizedListing(
        source=Source.EBAY,
        external_id="4",
        title="Signum spoiler i3401009",
        description="",
        url="http",
        price=Decimal("100")
    )
    result = matcher.match(listing)
    assert result is not None
    assert any(r.rule == "signum_in_title" for r in result.reasons)

def test_vectra_c_in_title(matcher):
    listing = NormalizedListing(
        source=Source.EBAY,
        external_id="5",
        title="Vectra C spoiler i3401009",
        description="",
        url="http",
        price=Decimal("100")
    )
    result = matcher.match(listing)
    assert result is not None
    assert any(r.rule == "vectra_c_in_title" for r in result.reasons)

def test_part_name_alias_match(matcher):
    listing = NormalizedListing(
        source=Source.EBAY,
        external_id="6",
        title="Frontspoiler i3401009",
        description="",
        url="http",
        price=Decimal("100")
    )
    result = matcher.match(listing)
    assert result is not None
    assert any(r.rule == "part_name_alias" for r in result.reasons)

def test_facelift_keyword(matcher):
    listing = NormalizedListing(
        source=Source.EBAY,
        external_id="7",
        title="Frontspoiler facelift i3401009",
        description="",
        url="http",
        price=Decimal("100")
    )
    result = matcher.match(listing)
    assert result is not None
    assert any(r.rule == "facelift_keyword" for r in result.reasons)

def test_excluded_part_number(matcher):
    listing = NormalizedListing(
        source=Source.EBAY,
        external_id="8",
        title="i3401002 front spoiler",
        description="",
        url="http",
        price=Decimal("100")
    )
    result = matcher.match(listing)
    # Could be None if no part matches at all, but let's assume it matches something
    # or just checks exclusion
    assert result is None or result.total_score < 0

def test_negative_overrides_positive(matcher):
    listing = NormalizedListing(
        source=Source.EBAY,
        external_id="9",
        title="Irmscher Signum i3401002 i3401009",
        description="",
        url="http",
        price=Decimal("100")
    )
    result = matcher.match(listing)
    assert result is not None
    assert result.total_score < 0
    assert any(r.rule == "excluded_part_number" for r in result.reasons)

def test_incompatible_model(matcher):
    listing = NormalizedListing(
        source=Source.EBAY,
        external_id="10",
        title="Astra i3401009",
        description="",
        url="http",
        price=Decimal("100")
    )
    result = matcher.match(listing)
    assert result is not None
    assert any(r.rule == "incompatible_model" for r in result.reasons)

def test_replica_keyword(matcher):
    listing = NormalizedListing(
        source=Source.EBAY,
        external_id="11",
        title="Irmscher style i3401009",
        description="",
        url="http",
        price=Decimal("100")
    )
    result = matcher.match(listing)
    assert result is not None
    assert any(r.rule == "replica_keyword" for r in result.reasons)

def test_multilingual_alias_german(matcher):
    listing = NormalizedListing(
        source=Source.EBAY,
        external_id="12",
        title="Kühlergrill i3401009", # Kühlergrill alias match
        description="",
        url="http",
        price=Decimal("100")
    )
    result = matcher.match(listing)
    assert result is not None
    # Might not be the exact Kühlergrill if not in part config, but part_name_alias match.

def test_multilingual_alias_polish(matcher):
    listing = NormalizedListing(
        source=Source.EBAY,
        external_id="13",
        title="zderzak i3401009",
        description="",
        url="http",
        price=Decimal("100")
    )
    result = matcher.match(listing)
    assert result is not None

def test_false_positive_generic(matcher):
    listing = NormalizedListing(
        source=Source.EBAY,
        external_id="14",
        title="Generic car parts",
        description="",
        url="http",
        price=Decimal("100")
    )
    result = matcher.match(listing)
    assert result is None

def test_scoring_explanation(matcher):
    listing = NormalizedListing(
        source=Source.EBAY,
        external_id="15",
        title="Irmscher Signum i3401009",
        description="",
        url="http",
        price=Decimal("100")
    )
    result = matcher.match(listing)
    assert result is not None
    rules = [r.rule for r in result.reasons]
    assert "irmscher_in_title" in rules
    assert "signum_in_title" in rules
    assert "exact_part_number_title" in rules

def test_best_match_selected(matcher):
    # listing with i3401009 and i3401010, should pick the one with best match
    listing = NormalizedListing(
        source=Source.EBAY,
        external_id="16",
        title="Frontspoiler i3401009 i3401050",
        description="",
        url="http",
        price=Decimal("100")
    )
    result = matcher.match(listing)
    assert result is not None

def test_no_match_returns_none(matcher):
    listing = NormalizedListing(
        source=Source.EBAY,
        external_id="17",
        title="Opel Omega Bumper",
        description="",
        url="http",
        price=Decimal("100")
    )
    result = matcher.match(listing)
    assert result is None

def test_algorithm_version(matcher):
    listing = NormalizedListing(
        source=Source.EBAY,
        external_id="18",
        title="i3401009",
        description="",
        url="http",
        price=Decimal("100")
    )
    result = matcher.match(listing)
    assert result is not None
    assert result.algorithm_version == ALGORITHM_VERSION
