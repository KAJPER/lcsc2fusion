import pytest

from lcsc2fusion.links import lcsc_product_url


def test_product_url_for_a_canonical_id() -> None:
    assert lcsc_product_url("C2040") == "https://www.lcsc.com/product-detail/C2040.html"


@pytest.mark.parametrize(
    "bad", ["", "c2040", "2040", "C", "C20 40", "C2040/../x", "C2040?x=1", "C" + "9" * 13]
)
def test_product_url_refuses_anything_but_a_canonical_id(bad: str) -> None:
    with pytest.raises(ValueError):
        lcsc_product_url(bad)
