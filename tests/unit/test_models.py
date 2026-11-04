"""Tests for core data models."""

import pytest

from phasmid_pm.core.models import BrowserSettings, generate_short_id


def test_short_id_shape_and_uniqueness():
    ids = {generate_short_id() for _ in range(1000)}
    assert len(ids) == 1000  # all unique
    for value in ids:
        assert len(value) == 8
        assert not (set(value) & set("0o1li"))  # no confusing characters


def test_a_gpu_is_normalised_and_checked():
    assert BrowserSettings(gpu=" 0x10DE:0x2504 ").gpu == "0x10de:0x2504"
    assert BrowserSettings(gpu="").gpu is None
    with pytest.raises(ValueError, match="vendor_id"):
        BrowserSettings(gpu="RTX 3060")


def test_blank_languages_follow_the_proxy():
    assert BrowserSettings(languages=["", " "]).languages is None
    assert BrowserSettings(languages=[" de-DE", "de"]).languages == ["de-DE", "de"]


def test_only_the_oses_phasmid_can_claim_are_accepted():
    with pytest.raises(ValueError):
        BrowserSettings(os="macos")


def test_settings_from_an_older_version_still_load():
    """Unknown keys are ignored rather than refusing the stored row."""
    assert BrowserSettings(**{"os": "linux", "screen": "1920x1080"}).os == "linux"
