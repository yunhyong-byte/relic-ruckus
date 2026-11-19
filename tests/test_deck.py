from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from hashlib import sha256
from io import BytesIO
from random import Random

from PIL import Image
import pytest

from relic.cards import CATEGORIES, MODES, STATS, validate_card
from relic.database import Deck, StaleEdit
from relic.editor import fields_for, make_draft
from relic.rules import Game
from relic.seed import ART


def test_seeded_deck_has_distinct_decodable_art_and_balanced_scores(tmp_path):
    deck = Deck(tmp_path / "deck.sqlite3").snapshot()
    assert Counter(card.category for card in deck) == dict.fromkeys(CATEGORIES, 6)
    assert len({card.name for card in deck}) == len({card.id for card in deck}) == 36
    assert len({card.scores for card in deck}) == 36
    assert len({sha256(card.image).hexdigest() for card in deck}) == 36
    assert {path.stem for path in ART.glob("*.webp")} == {card.id for card in deck}
    for card in deck:
        validate_card(card)
        assert card.image == (ART / f"{card.id}.webp").read_bytes()
        assert card.mime == "image/webp" and card.shows_image and card.description
        with Image.open(BytesIO(card.image)) as image:
            image.load()
            assert image.size == (540, 540)
        assert max(sum(other.score(stat) < card.score(stat) for other in deck) for stat in STATS) >= 16
    columns = list(zip(*(card.scores for card in deck)))
    for column in columns:
        assert max(column) - min(column) >= 55
        assert 25 <= len(set(column)) < 36  # Distinct full profiles still allow tie rounds.
    maxima = tuple(max(column) for column in columns)
    assert not any(card.scores == maxima for card in deck)


def test_init_concurrency_edit_reopen_snapshot_and_revision(tmp_path):
    path = tmp_path / "shared.sqlite3"
    with ThreadPoolExecutor(max_workers=2) as executor:
        first, second = executor.map(Deck, (path, path))
    original = first.get("milla")
    old_match = Game(["Jo"], first.snapshot(), Random(5))
    draft = replace(original, scores=(12, *original.scores[1:]), bio="Museum playtest. A second sentence.")
    second.save(draft)
    reopened = Deck(path)
    new_match = Game(["Jo"], reopened.snapshot(), Random(5))
    assert old_match.deck["milla"] == original
    assert new_match.deck["milla"].score("Fragility") == 12
    assert new_match.deck["milla"].bio == draft.bio
    assert new_match.deck["milla"].image == original.image
    with pytest.raises(StaleEdit):
        first.save(draft)


def test_editor_validates_images_credits_and_all_display_modes(tmp_path):
    deck = Deck(tmp_path / "local.sqlite3")
    card = deck.get("milla")
    values = fields_for(card)
    values["Value"] = "101"
    with pytest.raises(ValueError, match="six stats"):
        make_draft(card, values)
    values["Value"] = "52"
    with pytest.raises(ValueError, match="decoded"):
        make_draft(card, values, upload=b"corrupt")

    picture = BytesIO()
    Image.new("RGB", (3, 3), "orange").save(picture, "PNG")
    new_values = {
        **values,
        "alt": "An orange square",
        "archive": "An archival example.",
        "image_source": "https://example.org/images/relic-12",
        "image_creator": "Image maker",
        "image_rights": "CC0 1.0",
        "image_credit": "Image collection, record 12",
        "text_source": "https://example.org/archive/text-6",
        "text_creator": "Text author",
        "text_rights": "Public domain",
        "text_credit": "Text collection, record 6",
    }
    for mode in MODES:
        current = deck.get("milla")
        draft = make_draft(current, {**new_values, "mode": mode},
                           upload=picture.getvalue() if mode == MODES[0] else None)
        deck.save(draft)
        saved = deck.get("milla")
        assert saved.mime == "image/png"
        assert saved.text_credit.creator == "Text author"
        assert saved.image_credit.creator == "Image maker"
        assert saved.description == ("" if mode == "Image only" else new_values["archive"])
        assert saved.shows_image == (mode != "Description only")
        assert saved.image == picture.getvalue()
    remove = make_draft(saved, {**new_values, "mode": MODES[2]}, remove=True)
    deck.save(remove)
    assert deck.get("milla").image is None
