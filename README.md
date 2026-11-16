# Order Tracker

A local dashboard for online orders. It reads order emails from your iCloud and/or Gmail inbox
(read-only), works out what each order is and where it stands, and shows everything on one page
served from your own PC.

Supported retailers: **Target, Mattel, Amazon, Pokémon Center** and **Topps**.

Everything runs locally on Windows. Nothing is uploaded anywhere: the inbox is opened read-only
over IMAP, passwords stay in Windows Credential Manager, and the dashboard is only reachable at
`127.0.0.1`.

## Features

- **Order status from email**: confirmed, shipped, out for delivery, delivered, delayed,
  cancelled, merged per order across all the emails a retailer sends.
- **Order details**: items, quantities, prices, totals, tracking numbers, delivery estimates,
  the name on the order, the shipping address and product photos where the email has them.
- **Dashboard** with tabs for Orders, Arriving, Items, Card market, Spending and Redirects,
  plus search, sorting, quick filters, CSV export, light/dark theme and keyboard shortcuts.
- **Pin, note and "mark as received"** per order, kept across rescans.
- **Windows notifications** when an order changes status, and when a package ships to an
  address that isn't your home address.
- **Trading card market prices** for sealed Pokémon, One Piece and Lorcana products in your
  orders, from [tcgcsv.com](https://tcgcsv.com) (a daily mirror of TCGplayer prices).
- **Optional shipment redirects**: select packages on the dashboard and hand them to a separate
  RedirectShipment tool (not part of this repo).

## Requirements

- Windows 10 or 11
- Python 3.10 or newer
- Google Chrome (optional; the default browser is used if Chrome isn't installed)
- An iCloud and/or Gmail account with an **app-specific password**

## Setup

1. Install the one dependency:

   ```
   pip install -r requirements.txt
   ```

2. Copy `accounts.example.json` to `accounts.json` and put in your inbox address(es). Remove
   the line for an account you don't use.

   ```json
   {
     "icloud": "you@icloud.com",
     "gmail": "you@gmail.com"
   }
   ```

3. Create an app-specific password and store it in Windows Credential Manager:

   ```
   python tracker.py --set-password           # iCloud  (create one at account.apple.com)
   python tracker.py --set-password gmail     # Gmail   (myaccount.google.com/apppasswords)
   ```

4. Run the first scan and open the dashboard:

   ```
   python tracker.py --open
   ```

## Usage

| Command | What it does |
| --- | --- |
| `python tracker.py` | Check the inbox for new emails and rebuild the dashboard |
| `python tracker.py --open` | Same, then open the dashboard |
| `python tracker.py --show` | Open the dashboard without scanning |
| `python tracker.py --serve` | Run the local dashboard server in the foreground |
| `python tracker.py --rescan` | Forget cached emails and re-read the last `--days` days (default 45) |
| `python tracker.py --summary [retailer]` | Print active orders in the terminal |
| `python tracker.py --offline` | Skip the inbox check and use cached emails |
| `python tracker.py --quiet` | No notifications |

The dashboard is served at `http://127.0.0.1:8765/` and has its own Refresh button.

To keep it up to date automatically, add a Windows Task Scheduler task that runs
`pythonw tracker.py` every 30 minutes.

## Files

| File | Purpose |
| --- | --- |
| `tracker.py` | Inbox scan, email parsing, notifications, local server |
| `market.py` | Trading card market prices |
| `dashboard_template.html` | The dashboard page; order data is injected into it |
| `make_icons.py` | Generates the icons in `icons/` (needs Pillow) |
| `track_orders.py` | Older first-draft scanner, kept for reference |

Created at runtime and git-ignored, because they hold personal data:

| File | Purpose |
| --- | --- |
| `accounts.json` | Your inbox addresses |
| `data/` | Parsed email cache, price history, order notes, logs |
| `dashboard.html` | The generated dashboard |

## Known gaps

- Amazon confirmation emails often have no product names or prices.
- Mattel shipping emails have no prices.
- Pokémon Center emails have no product photos.
- Topps parsing is untested against real order emails.
- Windows only (notifications, Credential Manager and shortcuts are Windows-specific).

## Contributing

This is a personal project. Only the owner can push to it; you're welcome to fork it or open an
issue.
