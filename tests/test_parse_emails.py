"""Sample emails -> parsed fields. Each fixture goes through the real scan (headers, status from
the subject, HTML body), exactly as an email from the inbox would."""
import email

import pytest

import tracker

TROVE = "Lorcana Trading Card Game: Attack of The Vine! Illumineer's Trove"
ETB = "Pokémon Trading Card Game: Mega Evolution— Delta Reign Elite Trainer Box"
DOLL = "Monster High The Nightmare Before Christmas Oogie Boogie Doll"


def names(parsed):
    return [(i["name"], i["qty"], i["price"]) for i in parsed["items"]]


# ---------------------------------------------------------------- Target
def test_target_confirmation(parse):
    p = parse("target_confirmed")
    assert (p["retailer"], p["status"], p["order"]) == ("Target", "Order Confirmed", "902000000001234")
    assert names(p) == [(TROVE, 2, 49.99)]
    assert p["total"] == 107.48
    assert p["eta"] == "Fri, Nov 27"
    assert p["placed"] == "November 13, 2026"
    assert p["name"] == "Alex"
    assert p["tracking"] == []
    assert p["date"] == "2026-11-13T19:05:00+00:00"  # stored in UTC


def test_target_address_drops_the_recipient_and_fixes_case(parse):
    assert parse("target_confirmed")["ship_to"] == "100 N Example Way, Testville, NC 27000"
    assert parse("target_shipped")["ship_to"] == "500 Other Rd, Elsewhere, NC 28000"


def test_target_recommended_products_are_not_order_items(parse):
    for fixture in ("target_confirmed", "target_shipped", "target_item_cancelled"):
        assert len(parse(fixture)["items"]) == 1, fixture


def test_target_shipped(parse):
    p = parse("target_shipped")
    assert (p["status"], p["order"]) == ("Shipped", "902000000005678")
    assert p["tracking"] == ["1Z999AA10123456784"]
    assert p["total"] == 64.19  # "we processed a payment of $64.19"
    assert names(p) == [(ETB, 1, None)]  # the $29.99 below belongs to a recommendation


def test_target_product_photo_is_a_thumbnail(parse):
    img = parse("target_confirmed")["items"][0]["img"]
    assert img.startswith("https://target.scene7.com/is/image/Target/GUEST_")
    assert img.endswith("?wid=200&hei=200&fmt=webp&qlt=80")


def test_target_item_cancellation(parse):
    p = parse("target_item_cancelled")
    assert (p["status"], p["order"]) == ("Item Cancelled", "902000000001234")
    assert names(p) == [(TROVE, 2, None)]


def test_target_delay_notice(parse):
    p = parse("target_delayed")
    assert p["subject"].startswith("There’s a new estimated")  # RFC 2047 encoded subject is decoded
    assert (p["status"], p["order"]) == ("Delayed", "902000000005678")
    assert names(p) == [(ETB, 1, None)]


@pytest.mark.xfail(strict=True, reason="known gap: the new date in Target delay emails "
                                       "('Estimated' / 'Fri, November 20, 2026' on two lines) isn't picked up")
def test_target_delay_notice_has_the_new_date(parse):
    assert "20" in (parse("target_delayed")["eta"] or "")


def test_marketing_email_is_not_stored(parse):
    assert parse("target_marketing") is None


# ---------------------------------------------------------------- Amazon
def test_amazon_confirmation_without_product_names(parse):
    p = parse("amazon_confirmed")
    assert (p["retailer"], p["status"], p["order"]) == ("Amazon", "Order Confirmed", "111-2223334-5556667")
    assert names(p) == [("Toys & Games (unnamed)", 1, None)]  # only the category is in the email
    assert p["total"] == 16.23
    assert p["eta"] == "Tuesday"
    assert p["ship_to"] == "Testville, NC (city only)"


def test_amazon_shipped(parse):
    p = parse("amazon_shipped")
    assert (p["status"], p["order"], p["eta"], p["name"]) == ("Shipped", "111-2223334-5556667", "tomorrow", "Alex")
    assert p["tracking"] == []  # the order number must not be mistaken for a tracking number


@pytest.mark.parametrize("subject, expected", [
    ('Shipped: 2 "Schylling NeeDoh Teenie..."', [("Schylling NeeDoh Teenie", 2)]),
    ('Ordered: "Schylling NeeDoh Squishmas..."', [("Schylling NeeDoh Squishmas", 1)]),
    ("Delivered 7 items: Video Games, Toys & Games", [("Video Games, Toys & Games (unnamed)", 7)]),
    ("Your order has shipped", []),
])
def test_amazon_items_from_the_subject(subject, expected):
    assert [(i["name"], i["qty"]) for i in tracker.parse_items("", subject)] == expected


def test_amazon_split_price_is_rejoined():
    msg = email.message_from_string('Content-Type: text/html; charset="utf-8"\n\n'
                                    "<div>Fidget Toy</div><div>Quantity: 1</div><div>$ 23 99</div>")
    assert "$23.99" in tracker.message_text(msg).splitlines()


# ---------------------------------------------------------------- Mattel
def test_mattel_confirmation(parse):
    p = parse("mattel_confirmed")
    assert (p["retailer"], p["status"], p["order"]) == ("Mattel", "Order Confirmed", "CHP10000001")
    assert names(p) == [(DOLL, 2, 70.0)]  # unit price, not the $140.00 line subtotal
    assert p["total"] == 159.10
    assert p["name"] == "Alex"
    assert p["ship_to"] == "100 Example Way, Testville, NC 27000"  # shipping block, not billing
    assert p["items"][0]["img"].endswith("doll.jpg?v=1&width=200")


def test_mattel_shipped_takes_the_order_number_from_the_body(parse):
    p = parse("mattel_shipped")
    assert p["subject"] == "Your Mattel Creations Order # has shipped"  # no number in the subject
    assert (p["status"], p["order"]) == ("Shipped", "CHP10000001")
    assert p["tracking"] == ["1Z999AA10123456795"]
    assert names(p) == [(DOLL, 2, None)]
    assert p["ship_to"] == "100 Example Way, Testville, NC 27000"


# ---------------------------------------------------------------- Pokémon Center
def test_pokemon_center_confirmation(parse):
    p = parse("pokemon_confirmed")
    assert (p["retailer"], p["status"], p["order"]) == ("Pokémon Center", "Order Confirmed", "P0040000001")
    assert names(p) == [  # "Qty" and ": 2" sit in separate table cells
        ("Pokémon TCG: 30th Celebration Mini Tin – Lapras (Day)", 1, 9.99),
        ("Pokémon TCG: 30th Celebration Mini Tin – Pikachu (Day)", 2, 9.99),
    ]
    assert p["total"] == 32.19  # Order Total, not Order Subtotal
    assert p["placed"] == "October 15, 2026"
    assert p["ship_to"] == "100 Example Way Space 1, Testville, NC 27000"


def test_pokemon_center_shipped_joins_the_split_address(parse):
    p = parse("pokemon_shipped")
    assert (p["status"], p["order"]) == ("Shipped", "P0040000001")
    assert p["ship_to"] == "100 Example Way, Testville, NC 27000"  # city / state / zip were on three lines
    assert names(p) == [("Pokémon TCG: 30th Celebration Mini Tin – Lapras (Day)", 1, None),
                        ("Pokémon TCG: 30th Celebration Mini Tin – Pikachu (Day)", 2, None)]
    assert p["tracking"] == []  # the fulfillment ID is not a tracking number


# ---------------------------------------------------------------- Topps
def test_topps_sender_and_generic_layout(parse):
    """No real Topps order email has been seen yet, so this fixture's layout is invented. It
    only shows that a Topps email in a common shop layout goes through the shared parser."""
    p = parse("topps_shipped_synthetic")
    assert (p["retailer"], p["status"], p["order"]) == ("Topps", "Shipped", "TP12345678")
    assert names(p) == [("2026 Topps Chrome Baseball Hobby Box", 1, 249.99)]
    assert p["tracking"] == ["9400111899223344556677"]
    assert p["total"] == 267.49
