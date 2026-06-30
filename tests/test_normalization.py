"""Unit tests for fusion splitting and gene normalization."""

import pytest

from src.pipeline.normalization import split_fusion, _is_ensembl_id


def test_split_double_colon():
    g1, g2 = split_fusion("ANKRD13A::ACACB")
    assert g1 == "ANKRD13A"
    assert g2 == "ACACB"


def test_split_double_dash():
    g1, g2 = split_fusion("EML4--ALK")
    assert g1 == "EML4"
    assert g2 == "ALK"


def test_split_slash():
    g1, g2 = split_fusion("BCR/ABL1")
    assert g1 == "BCR"
    assert g2 == "ABL1"


def test_split_no_separator():
    g1, g2 = split_fusion("KRAS")
    assert g1 == "KRAS"
    assert g2 is None


def test_ensembl_id_detection():
    assert _is_ensembl_id("ENSG00000253796")
    assert not _is_ensembl_id("ACACB")
    assert not _is_ensembl_id("ANKRD13A")
