"""Merging the emails of one order into a single order with one status."""
import pytest

import tracker
from conftest import item, make_email

BOX = "Pokémon Trading Card Game: Elite Trainer Box"


def build(*emails):
    return tracker.build_orders(list(emails))


def day(n, hour=12):
    return f"2026-11-{n:02d}T{hour:02d}:00:00+00:00"


# ---------------------------------------------------------------- from fixture emails
def test_target_order_fully_cancelled(scan_orders):
    """Confirmation for 2 of an item, then both cancelled -> the whole order is cancelled."""
    o = scan_orders("target_confirmed", "target_item_cancelled")["Target|902000000001234"]
    assert o["status"] == "Cancelled"
    assert [(i["qty"], i["cancelled"], i["price"]) for i in o["items"]] == [(2, 2, 49.99)]
    assert len(o["history"]) == 2


def test_target_shipped_then_delayed_stays_shipped_but_flagged(scan_orders):
    o = scan_orders("target_shipped", "target_delayed")["Target|902000000005678"]
    assert (o["status"], o["delayed"]) == ("Shipped", True)
    assert o["tracking"] == [{"number": "1Z999AA10123456784", "carrier": "UPS",
                              "url": "https://www.ups.com/track?tracknum=1Z999AA10123456784"}]
    assert o["away"] is True  # shipped to an address that isn't home


def test_amazon_confirmed_then_shipped(scan_orders):
    o = scan_orders("amazon_confirmed", "amazon_shipped")["Amazon|111-2223334-5556667"]
    assert (o["status"], o["total"]) == ("Shipped", 16.23)
    assert o["eta"] == "Tue, Nov 17"  # "tomorrow" in an email sent Monday Nov 16
    assert len(o["items"]) == 1
    assert o["link"] == "https://www.amazon.com/gp/your-account/order-details?orderID=111-2223334-5556667"
    assert o["away"] is None  # only the city is known and it's the home city


def test_mattel_shipped_keeps_price_from_the_confirmation(scan_orders):
    o = scan_orders("mattel_confirmed", "mattel_shipped")["Mattel|CHP10000001"]
    assert (o["status"], o["total"], o["away"]) == ("Shipped", 159.10, False)
    assert [(i["qty"], i["price"]) for i in o["items"]] == [(2, 70.0)]
    assert [t["carrier"] for t in o["tracking"]] == ["UPS"]


def test_pokemon_center_emails_merge_into_one_order(scan_orders):
    orders = scan_orders("pokemon_confirmed", "pokemon_shipped")
    assert list(orders) == ["Pokémon Center|P0040000001"]
    o = orders["Pokémon Center|P0040000001"]
    assert (o["status"], o["total"], len(o["items"])) == ("Shipped", 32.19, 2)
    assert o["ship_to"] == "100 Example Way, Testville, NC 27000"  # latest address wins


def test_emails_out_of_order_give_the_same_result(scan_orders):
    a = scan_orders("mattel_confirmed", "mattel_shipped")
    b = scan_orders("mattel_shipped", "mattel_confirmed")
    assert a == b


def test_orders_are_sorted_newest_update_first(scan_orders):
    orders = list(scan_orders("pokemon_confirmed", "target_shipped", "mattel_confirmed").values())
    assert [o["retailer"] for o in orders] == ["Target", "Mattel", "Pokémon Center"]


# ---------------------------------------------------------------- status rules
def test_full_lifecycle():
    (o,) = build(make_email("Order Confirmed", date=day(1)), make_email("Shipped", date=day(2)),
                 make_email("Out for Delivery", date=day(3)), make_email("Delivered", date=day(4)))
    assert o["status"] == "Delivered"
    assert [h["status"] for h in o["history"]] == ["Order Confirmed", "Shipped", "Out for Delivery", "Delivered"]
    assert o["updated"] == day(4)


def test_late_confirmation_does_not_undo_shipped():
    (o,) = build(make_email("Shipped", date=day(1)), make_email("Order Confirmed", date=day(2)))
    assert o["status"] == "Shipped"


def test_delay_before_shipping_shows_as_delayed_until_it_ships():
    confirmed, delayed = make_email("Order Confirmed", date=day(1)), make_email("Delayed", date=day(2))
    (o,) = build(confirmed, delayed)
    assert (o["status"], o["delayed"]) == ("Delayed", True)
    (o,) = build(confirmed, delayed, make_email("Shipped", date=day(3)))
    assert (o["status"], o["delayed"]) == ("Shipped", False)


def test_cancellation_is_final():
    (o,) = build(make_email("Cancelled", date=day(1)), make_email("Shipped", date=day(2)))
    assert o["status"] == "Cancelled"


def test_partial_item_cancellation_keeps_the_order_active():
    (o,) = build(make_email("Order Confirmed", date=day(1), items=[item(BOX, 3, 49.99), item("Booster Bundle", 1)]),
                 make_email("Item Cancelled", date=day(2), items=[item(BOX, 2)]))
    assert o["status"] == "Order Confirmed"
    assert {i["name"]: i["cancelled"] for i in o["items"]} == {BOX: 2, "Booster Bundle": 0}


def test_cancelled_quantity_never_exceeds_ordered_quantity():
    (o,) = build(make_email("Order Confirmed", date=day(1), items=[item(BOX, 1)]),
                 make_email("Item Cancelled", date=day(2), items=[item(BOX, 1)]),
                 make_email("Item Cancelled", date=day(3), items=[item(BOX, 1)]))
    assert o["items"][0]["cancelled"] == 1


# ---------------------------------------------------------------- merging rules
def test_same_email_in_two_inboxes_counts_once():
    e = make_email("Item Cancelled", date=day(2), items=[item(BOX, 1)])
    (o,) = build(make_email("Order Confirmed", date=day(1), items=[item(BOX, 2)]), e, dict(e))
    assert len(o["history"]) == 2
    assert o["items"][0]["cancelled"] == 1


def test_orders_of_different_retailers_with_the_same_number_stay_apart():
    orders = build(make_email("Shipped", order="12345678", retailer="Target"),
                   make_email("Shipped", order="12345678", retailer="Mattel"))
    assert sorted(o["key"] for o in orders) == ["Mattel|12345678", "Target|12345678"]


def test_email_without_order_number_joins_its_order_by_tracking_number():
    (o,) = build(make_email("Shipped", date=day(1), tracking=["1Z999AA10123456784"]),
                 make_email("Delivered", date=day(2), order=None, tracking=["1Z999AA10123456784"]))
    assert (o["order"], o["status"]) == ("900000000000001", "Delivered")


def test_truncated_item_name_merges_with_the_full_name():
    full = "Fuxury Bubble Mailers 6x10 100 Pack Padded Envelopes"
    (o,) = build(make_email("Order Confirmed", date=day(1), items=[item("Fuxury Bubble Mailers 6x10 100 P", 1, 12.99)]),
                 make_email("Shipped", date=day(2), items=[item(full, 1)]))
    assert [(i["name"], i["price"]) for i in o["items"]] == [(full, 12.99)]


def test_category_placeholder_is_dropped_once_the_product_is_known():
    (o,) = build(make_email("Order Confirmed", date=day(1), items=[item("Toys & Games (unnamed)", 1)]),
                 make_email("Shipped", date=day(2), items=[item("Schylling NeeDoh Teenie", 1)]))
    assert [i["name"] for i in o["items"]] == ["Schylling NeeDoh Teenie"]


def test_quantity_comes_from_the_confirmation_not_from_partial_shipments():
    (o,) = build(make_email("Order Confirmed", date=day(1), items=[item(BOX, 3)]),
                 make_email("Shipped", date=day(2), items=[item(BOX, 1)]))
    assert o["items"][0]["qty"] == 3


def test_total_comes_from_the_confirmation_not_from_a_partial_charge():
    (o,) = build(make_email("Shipped", date=day(1), total=64.19), make_email("Order Confirmed", date=day(2), total=128.38))
    assert o["total"] == 128.38
    (o,) = build(make_email("Order Confirmed", date=day(1), total=128.38), make_email("Shipped", date=day(2), total=64.19))
    assert o["total"] == 128.38


def test_delivered_email_does_not_overwrite_the_eta():
    (o,) = build(make_email("Shipped", date=day(1), eta="Fri, Nov 6"), make_email("Delivered", date=day(5), eta="today"))
    assert o["eta"] == "Fri, Nov 6"


def test_city_only_address_does_not_replace_a_full_one():
    full = "100 Example Way, Testville, NC 27000"
    (o,) = build(make_email("Order Confirmed", date=day(1), ship_to=full),
                 make_email("Shipped", date=day(2), ship_to="Testville, NC (city only)"))
    assert o["ship_to"] == full


def test_photo_is_borrowed_from_the_same_product_in_another_order():
    with_photo = dict(item(BOX), img="https://example.com/box.jpg")
    orders = build(make_email("Shipped", order="900000000000001", items=[with_photo]),
                   make_email("Shipped", order="900000000000002", retailer="Pokémon Center",
                              items=[item("Pokémon TCG: Elite Trainer Box")]))
    borrowed = next(o for o in orders if o["retailer"] == "Pokémon Center")["items"][0]
    assert (borrowed["img"], borrowed["img_borrowed"]) == ("https://example.com/box.jpg", True)


def test_no_emails_no_orders():
    assert build() == []


# ---------------------------------------------------------------- what the dashboard acts on
@pytest.mark.parametrize("status, expected", [
    ("Shipped", {"1Z999AA10123456784", "123456789012"}),
    ("Delivered", set()),
    ("Cancelled", set()),
])
def test_only_ups_and_fedex_packages_of_active_orders_can_be_redirected(status, expected):
    orders = build(make_email(status, tracking=["1Z999AA10123456784", "123456789012", "TBA123456789012",
                                                "9400111899223344556677"]))
    assert tracker.redirectable_numbers(orders) == expected
