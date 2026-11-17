"""Small pure functions: carriers, dates, addresses, product-name matching."""
import pytest

import tracker
from conftest import HOME


@pytest.mark.parametrize("number, carrier, url_part", [
    ("1Z999AA10123456784", "UPS", "ups.com/track?tracknum=1Z999AA10123456784"),
    ("TBA123456789012", "Amazon", None),
    ("9400111899223344556677", "USPS", "tools.usps.com"),
    ("EZ123456789US", "USPS", "tools.usps.com"),
    ("123456789012", "FedEx", "fedex.com/fedextrack/?trknbr=123456789012"),
    ("123456789012345", "FedEx", "fedex.com"),
    ("LP00123456789", "Carrier", "parcelsapp.com/en/tracking/LP00123456789"),
])
def test_carrier_for(number, carrier, url_part):
    name, url = tracker.carrier_for(number)
    assert name == carrier
    assert (url is None) if url_part is None else (url_part in url)


SENT_MONDAY = "2026-11-16T17:00:00+00:00"  # midday, so the local date is Nov 16 in any US or UTC zone


@pytest.mark.parametrize("eta, expected", [
    ("today", "Mon, Nov 16"),
    ("tomorrow", "Tue, Nov 17"),
    ("Tuesday", "Tue, Nov 17"),
    ("Thursday", "Thu, Nov 19"),
    ("Monday", "Mon, Nov 23"),               # a weekday name never means the day the email was sent
    ("tomorrow by 8 PM", "Tue, Nov 17 by 8 PM"),
    ("Wednesday, November 26", "Wednesday, November 26"),  # an explicit date is left alone
    ("Fri, Nov 27", "Fri, Nov 27"),
    ("11/27", "11/27"),
    ("soon", "soon"),
    (None, None),
])
def test_absolute_eta(eta, expected):
    assert tracker.absolute_eta(eta, SENT_MONDAY) == expected


@pytest.mark.parametrize("parts, expected", [
    (["100 example way", "Testville, Ca 90000"], "100 Example Way, Testville, CA 90000"),
    (["100 example way", "Testville, North Carolina 27000", "United States"], "100 Example Way, Testville, NC 27000"),
    ([" 500 Other rd", " Elsewhere", " NC", " 28000"], "500 Other Rd, Elsewhere, NC 28000"),
    (["100 McDonald St", "Testville, NC 27000"], "100 McDonald St, Testville, NC 27000"),  # mixed case is kept
])
def test_tidy_address(parts, expected):
    assert tracker.tidy_address(parts) == expected


@pytest.mark.parametrize("text, expected", [
    ("Delivers to: Alex Rivera, 100 Example Way, Testville, NC 27000", "100 Example Way, Testville, NC 27000"),
    ("Delivers to:\n100 Example Way, Testville, NC 27000", "100 Example Way, Testville, NC 27000"),
    ("Shipping Address\nAlex Rivera\n100 Example Way\nTestville, NC 27000\nUS\nBilling Address\n9 Bill St",
     "100 Example Way, Testville, NC 27000"),
    ("Alex - SAN JOSE, CA", "San Jose, CA (city only)"),
    ("Shipping Address\nWe'll email you when it ships", None),  # label without an address
    ("Thanks for your order!", None),
])
def test_parse_ship_to(text, expected):
    assert tracker.parse_ship_to(text) == expected


@pytest.mark.parametrize("ship_to, expected", [
    ("100 Example Way, Testville, NC 27000", False),
    ("100 N Example Way, Testville, NC 27000", False),        # direction prefix
    ("100.9 Example Way, Testville, NC 27000", False),        # decimal house-number variant
    ("100 Example Way Space 1, Testville, NC 27000", False),  # unit
    ("100 example way, Testville, NC 27000-1234", False),     # ZIP+4, any case
    ("100 Example Way, Testville, NC 27999", True),           # same street name, different zip
    ("1000 Example Way, Testville, NC 27000", True),          # house number must match exactly
    ("500 Other Rd, Elsewhere, NC 28000", True),
    ("Testville, NC (city only)", None),                      # can't tell from the city alone
    ("Elsewhere, NC (city only)", True),
    (None, None),
])
def test_ships_elsewhere(ship_to, expected):
    assert tracker.ships_elsewhere(ship_to, HOME) is expected


def test_ships_elsewhere_is_unknown_without_a_complete_home_address():
    assert tracker.ships_elsewhere("500 Other Rd, Elsewhere, NC 28000", {}) is None
    assert tracker.ships_elsewhere("500 Other Rd, Elsewhere, NC 28000", {"street_number": "100"}) is None


@pytest.mark.parametrize("a, b, expected", [
    ("Pokémon Trading Card Game: Elite Trainer Box", "Pokemon TCG Elite Trainer Box", True),
    ("Fuxury Bubble Mailers 6x10 100 P", "Fuxury Bubble Mailers 6x10 100 Pack Padded Envelopes", True),
    ("Pokémon TCG: Mini Tin – Lapras (Day)", "Pokémon TCG: Mini Tin – Pikachu (Day)", True),  # known limit: variants look alike
    ("Pokémon TCG: Elite Trainer Box", "Monster High Oogie Boogie Doll", False),
    ("", "Anything", False),
])
def test_names_match(a, b, expected):
    assert tracker.names_match(a, b) is expected


@pytest.mark.parametrize("src, expected", [
    ("https://m.media-amazon.com/images/I/abc123._SS142_.jpg", "https://m.media-amazon.com/images/I/abc123._SS200_.jpg"),
    ("https://target.scene7.com/is/image/Target/GUEST_1?wid=600", "https://target.scene7.com/is/image/Target/GUEST_1?wid=200&hei=200&fmt=webp&qlt=80"),
    ("https://cdn.shopify.com/s/files/1/doll.jpg", "https://cdn.shopify.com/s/files/1/doll.jpg?width=200"),
    ("https://example.com/other.png", "https://example.com/other.png"),
])
def test_product_img_url(src, expected):
    assert tracker.product_img_url(src) == expected


def test_parse_images_skips_logos_and_tracking_pixels():
    html = ('<img alt="Target Logo" src="https://target.scene7.com/is/image/Target/logo">'
            '<img alt="" src="https://target.scene7.com/is/image/Target/GUEST_0">'
            '<img src="https://pixel.example.com/open.gif">'
            "<img alt='Image of Trainer Box.' src='https://target.scene7.com/is/image/Target/GUEST_1'>")
    assert tracker.parse_images(html) == [
        ("Trainer Box", "https://target.scene7.com/is/image/Target/GUEST_1?wid=200&hei=200&fmt=webp&qlt=80")]
    assert tracker.parse_images(None) == []
