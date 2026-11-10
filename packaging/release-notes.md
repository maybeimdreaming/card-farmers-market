Card Farmer's Market farms your Steam trading card drops, then sells each card or turns it into gems by rules you set. The Items tab can also list skins, cases and other marketable items, only when you say so.

**New in 0.4:**
- **Safer with your money and cards (beta 10):** buy orders are saved the moment Steam takes them, so none is ever left on the market without the app knowing. A card Steam shows no price for is never turned into gems by itself. An item or listing whose price fell more than 15% since you looked goes back for your OK instead of being listed. A listing you cancel for an item waits for review rather than being listed again.
- **Stops when you say stop:** Stop farming stops the games at once, and starting a game yourself pauses farming within seconds, whatever else the app is busy with. Checking gems and boosters no longer freezes the window.
- **Prices for backgrounds, emoticons and booster packs** are found again (the search missed nearly all of them), and buying checks its orders with half the requests.
- **Sturdier:** a settings or state file damaged by a crash or a full disk no longer keeps the app from opening (the last good copy is used); a sign-in Steam has ended brings back the sign-in screen; `card-farm.log` in the data folder records what went wrong, for problem reports.
- **Quick badge:** pick any game with trading cards (your started badges, ones not crafted yet, the market's cheapest sets, or a search) and see exactly what it costs to take its badge to level 5: every card's listings with how many are up at each price, and the cards you already have counted. One button buys those cards and crafts each level. If some sell first it buys the next ones, never more than 10% (at least 5¢) over the quote, and never more than your Steam wallet holds.
- **One Badges tab** for everything that crafts badges: Quick badge, finishing sets you hold most of (moved from the Cards tab), and reaching a Steam level (the old Level up tab). Activity now shows under every tab.
- **Buying cards works again:** Steam's market item pages had started refusing the app's requests, which stopped "Buy & craft" and Level up's "Now" buying.
- **A garden:** a new look, with the tabs on a strip of grass over rocky ground, and a flower in the grass for every card you farm (foil cards grow a shimmering one). Themes: Light, Dark, Pink, or Auto to follow your system.
- Friendlier wording throughout, shorter explanations, and "Activity" instead of "Tape".
- Thanks to **hardwarebob** for the ideas behind the next three changes and the farm history (pull request #1).
- **Clearer problems:** when a game won't start, it says why (Steam not signed in, the game not owned, Steam's library broken); connection and Steam server troubles read in plain words.
- **"Too many requests" pauses everything on that Steam site,** not just market prices, so the limit isn't kept tripped from another corner.
- **Farm history:** every card the app farms is written to `farm-history.json` in its data folder (when, which card, foil or not, and the version that farmed it). The garden's flowers are the cards the app itself farmed.
- **Stays signed in.** The sign-in now renews itself the way the Steam website does. Before, it stopped working about a day after signing in: drops weren't seen anymore and the games list went stale (games looked like they still had drops).
- **Start / Stop farming** at the top of the window.
- **Playtime first, then one game at a time:** games with less than 2 hours of playtime idle together (up to 32) until they reach it, since most games don't drop cards before that; then games idle one at a time, in your "Farm first" order, which is how Steam drops cards fastest. The "Games at once" setting is gone.
- **Amounts in cents or dollars:** limits show as 50¢ under a dollar and $5.00 from there on; type either.
- A **Support on Ko-fi** link at the top of the window, for anyone who'd like to leave a tip.
- **Skipping:** a game you skip stays skipped until you unskip it, **Unskip all** brings every game back, and free-to-play games like CS2 (whose drops come from in-game purchases, while you play) are skipped unless you choose **Try**.
- **Market tab:** your listings next to today's prices, with Reprice (cancel and relist) and Cancel; a 30-day earnings chart, your sales by day and by game, and an optional notice when something sells.
- **Gems tab:** pack gems into sacks and sell them; booster packs whose market price beats their gem cost, made and listed in one click; booster packs you already have, sold or opened, whichever is worth more.
- **Reach a Steam level:** pick a level and a budget, and it finds the cheapest badge crafts and does them, buying now or with buy orders under the market price.
- **Finish sets:** card sets you hold most of: "Buy & craft" buys the missing cards (within your limit, checked before anything is bought) and crafts the badge; the set's cards are kept, not sold, until it's done.
- **Sales checks:** cards, items and booster packs are priced at what really sells, not at old asking prices nobody pays.
- **Easier on Steam's market:** prices are looked up while signed in, and when Steam says "too many requests", lookups pause for a while instead of failing over and over.
- **One Mac download for every Mac:** `Card-Farm-macOS.zip` runs on Apple silicon and Intel.
- New name: Card Farmer's Market (the download files keep the Card Farm name for now, so updates keep working). Updates install themselves with the Update button at the top.

## Downloads

| System | File | How to start it |
|---|---|---|
| Windows 10 / 11 (64-bit) | `Card-Farm-Windows.zip` | Unzip it anywhere and run **Card Farm.exe** in the `Card Farm` folder. Windows may warn that the app is from an unknown publisher: choose **More info → Run anyway**. |
| macOS 12 or newer, Apple silicon or Intel | `Card-Farm-macOS.zip` | Unzip it and move **Card Farm** to Applications. The first time, right-click it and choose **Open**, then **Open** again (it isn't notarised by Apple). `Card-Farm-macOS-Intel.zip` is the same app, there so older copies on Intel Macs can update. |
| Linux | `card-farm.pyz` | `chmod +x card-farm.pyz && ./card-farm.pyz`. Needs Python 3.10+, GTK 4, libadwaita and WebKitGTK 6 with PyGObject (most GNOME and KDE desktops have them). |

Steam must be running and signed in. Sign-in is by QR code with the Steam mobile app; it never sees your password. Market listings still need your OK in the Steam mobile app.

The Windows and macOS downloads contain Python and the pywebview library; their licences are in `THIRD-PARTY-NOTICES.txt` inside the download.
