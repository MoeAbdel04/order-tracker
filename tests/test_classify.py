"""Subject line -> order status, and sender -> retailer. The subjects are the formats the five
retailers really send (order numbers replaced)."""
import pytest

import tracker

SUBJECTS = [
    # Target
    ("Thanks for shopping with us! Here's your order #:902000000001234.", "Order Confirmed"),
    ("Release day is getting closer! We’ll process your preorder payment soon #902000000001234", "Pre-order"),
    ("Get ready for something special! An item from order #902000000001234 is about to ship.", "Shipped"),
    ("Get ready for something special! Items from order #902000000001234are about to ship.", "Shipped"),
    ("Your order arrives tomorrow! Order #902000000001234", "Shipped"),
    ("Your order arrives today! Order #902000000001234", "Out for Delivery"),
    ("Part of your order arrives today! Order #902000000001234", "Out for Delivery"),
    ("Your preorder items arrive today! Order #902000000001234", "Out for Delivery"),
    ("An item has arrived from order #902000000001234!", "Delivered"),
    ("Items have arrived from order #902000000001234!", "Delivered"),
    ("⭐ Please review your latest order. #902000000001234", "Delivered"),
    ("There’s a new estimated delivery date for an item from your order ending in 1234.", "Delayed"),
    ("You've successfully canceled an item from your order ending in 1234.", "Item Cancelled"),
    ("You've successfully canceled items from your order ending in 1234.", "Item Cancelled"),
    ("Sorry, we had to cancel order #902000000001234.", "Cancelled"),
    # Amazon
    ("Ordered 1 item: Toys & Games", "Order Confirmed"),
    ('Ordered: "Schylling NeeDoh Teenie..."', "Order Confirmed"),
    ("Shipped 3 items: Toys & Games", "Shipped"),
    ('Shipped: 2 "Schylling NeeDoh Teenie..."', "Shipped"),
    ("Out for delivery 2 items: Toys & Games", "Out for Delivery"),
    ("Delivered 7 items: Video Games, Toys & Games", "Delivered"),
    ('Delivered: 2 "Schylling NeeDoh Teenie..."', "Delivered"),
    ("Delivery estimate update for your Amazon order #111-2223334-5556667", "Delayed"),
    ("Item cancelled successfully: Wireless Gaming Controllers", "Item Cancelled"),
    # Mattel
    ("Your Mattel Creations Order #CHP10000001 is confirmed", "Order Confirmed"),
    ("Your Mattel Creations Order # has shipped", "Shipped"),
    ("Your Mattel Creations Order #CHP10000001 is out for delivery", "Out for Delivery"),
    ("Your Mattel Creations Order #CHP10000001 has been delivered", "Delivered"),
    ("Your Mattel Creations Order #CHP10000001 has been canceled", "Cancelled"),
    # Pokémon Center
    ("Thank you for shopping at PokemonCenter.com!", "Order Confirmed"),
    ("Your Pokémon Center order is on its way!", "Shipped"),
    ("Your package will arrive soon!", "Shipped"),
    ("Your package has been delivered!", "Delivered"),
    ("Your order has been canceled", "Cancelled"),
]


@pytest.mark.parametrize("subject, expected", SUBJECTS)
def test_order_subjects(subject, expected):
    assert tracker.classify(subject) == expected


@pytest.mark.parametrize("subject", [
    "Weekly Ad: 20% off toys this week only",
    "Pre-order now: the new holiday collection",
    "Your order could not be cancelled",
    "$10 off your next order",
    "Your Circle membership renews soon",
])
def test_marketing_and_notices_are_skipped(subject):
    assert tracker.classify(subject) == "skip"


@pytest.mark.parametrize("subject", ["Your receipt from the store", "Password changed", ""])
def test_unrelated_subjects_are_not_orders(subject):
    assert tracker.classify(subject) is None


def test_matching_ignores_case():
    assert tracker.classify("YOUR ORDER HAS SHIPPED") == "Shipped"


@pytest.mark.xfail(strict=True, reason="known bug: 'on the way' in a promo subject counts as Shipped, "
                                       "so this Target promo shows up as a phantom shipped order")
def test_savings_promo_is_not_a_shipment():
    assert tracker.classify("SO happy you are here—major savings are on the way.") in (None, "skip")


@pytest.mark.parametrize("sender, expected", [
    ("Target <orders@oe1.target.com>", "Target"),
    ('"Amazon.com" <shipment-tracking@amazon.com>', "Amazon"),
    ("Mattel Creations <orders@mattel.com>", "Mattel"),
    ("Pokémon Center <info@em.pokemon.com>", "Pokémon Center"),
    ("Topps <orders@topps.com>", "Topps"),
    # Hide My Email relay addresses keep the retailer in the local part
    ("Target <orders_at_oe1_target_com_abc123@icloud.com>", "Target"),
    # marketing / other departments of the same retailers
    ("Amazon Customer Reviews <customer-reviews@amazon.com>", None),
    ("Amazon Prime <prime@amazon.com>", None),
    ("Target Survey <survey@target.com>", None),
    ("Somebody Else <hello@example.com>", None),
])
def test_retailer_from_sender(sender, expected):
    assert tracker.retailer_of(sender) == expected
