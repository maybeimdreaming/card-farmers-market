Card Farmer's Market farms your Steam trading card drops, then sells each card or turns it into gems by rules you set. The Items tab can also list skins, cases and other marketable items, only when you say so.

**Card Farmer's Market 1.0** is the first finished release, out of beta. What it does has all been in the betas; 1.0 is that work settled, with the fixes below.

**What it does**
- **Farms your card drops** natively through Steam's own API library: games under 2 hours of playtime idle together first, then one at a time in the order you pick. It notices stalls, skips free-to-play games, pauses while you play, on battery, or outside the hours you choose, and claims the free sale stickers during Steam sales.
- **Sells or turns each card into gems by your rules** (Auto, Review or Off for each), priced at what really sells, never at old asking prices nobody pays. Foils, chosen games and a minimum price can be kept.
- **Items tab:** prices your other inventories (CS2, TF2, backgrounds, emoticons...) and lists only when you say so; backgrounds and emoticons worth more as gems are suggested for gems.
- **Market tab:** your listings next to today's prices with Reprice and Cancel, a 30-day earnings chart, all-time earnings by month and game, CSV export, and price alerts.
- **Gems tab:** sacks of gems, booster packs worth making, and booster packs you have sold or opened, whichever is worth more.
- **Badges tab:** Quick badge (any game to level 5 at the market's exact listings), Finish sets, and Reach a Steam level with instant buys or buy orders.
- **Signs in by QR code** with the Steam mobile app and stays signed in for months; it never sees your password. **Updates itself** with the Update button.

**Fixed since 0.5.0-beta.1**
- **Buying never repeats.** When Steam said "too many requests" partway through a Quick badge, Buy & craft or Level up, or the app was closed mid-way, the job started over and could buy cards (or place buy orders) it had already bought. Each purchase is now saved as it lands, and a job that runs again buys only what's still missing. The same goes for booster packs: one made before a stop is listed, not made again.
- **Buy orders past 10:** the app reads every open buy order, so a Level up with many orders never mistakes an open one for filled.

**Not in 1.0:** the Keys tab from the development builds (redeeming Steam keys, Humble Bundle) isn't ready and comes in a later version.

Thanks to **hardwarebob** for the ideas behind clearer Steam errors, the shared "too many requests" pause and the farm history (pull request #1).

## Downloads

| System | File | How to start it |
|---|---|---|
| Windows 10 / 11 (64-bit) | `Card-Farmers-Market-Windows.zip` | Unzip it anywhere and run **Card Farmer's Market.exe** in the `Card Farmer's Market` folder. Windows may warn that the app is from an unknown publisher: choose **More info → Run anyway**. |
| macOS 12 or newer, Apple silicon or Intel | `Card-Farmers-Market-macOS.zip` | Unzip it and move **Card Farmer's Market** to Applications. The first time, right-click it and choose **Open**, then **Open** again (it isn't notarised by Apple). |
| Linux | `card-farmers-market.pyz` | `chmod +x card-farmers-market.pyz && ./card-farmers-market.pyz`. Needs Python 3.10+, GTK 4, libadwaita and WebKitGTK 6 with PyGObject (most GNOME and KDE desktops have them). |

Steam must be running and signed in. Sign-in is by QR code with the Steam mobile app; it never sees your password. Market listings still need your OK in the Steam mobile app.

The Windows and macOS downloads contain Python and the pywebview library; their licences are in `THIRD-PARTY-NOTICES.txt` inside the download.

The `Card-Farm-*` files and `card-farm.pyz` are the same program under the old names, kept so copies from before 0.3.0-beta.10 can still update themselves. `-macOS-Intel.zip` is the same as `-macOS.zip`.
