# Steam Scout

A personal Steam sale recommender built with Python 3.10+ and its standard library. No packages to install. It creates a local HTML dashboard you can open in a browser, caches game metadata in SQLite, and can refresh every 24 hours while running.

## Try it

From this folder, run:

```powershell
python scout.py --demo --open
```

The demo uses invented games, prices, and a sample library. It needs no credentials and creates `demo.html`, separate from live results.

If Python is not installed, install Python 3.10 or later from https://www.python.org/downloads/.

## Connect your account

1. Copy `config.example.json` to `config.json`.
2. Set `steam_id` to your 17-digit Steam ID, as a quoted string. Set `country` to your store country (the example uses US). The budget is in that country's returned pricing currency.
3. Set Steam profile privacy **Game details** to **Public** and allow playtime visibility. Signing in alone would not bypass privacy restrictions.
4. Obtain a Steam Web API key at https://steamcommunity.com/dev/apikey and an IsThereAnyDeal API key at https://isthereanydeal.com/apps/.
5. Create `secrets.json` in this folder:

```json
{
  "steam_api_key": "YOUR_STEAM_WEB_API_KEY",
  "itad_api_key": "YOUR_ISTHEREANYDEAL_API_KEY"
}
```

Store keys locally; do not paste them into chat or commit them. These files are excluded by `.gitignore`. Steam receives the Steam key and ID; IsThereAnyDeal receives its key, country, and game IDs. Raw credentials are not written into the report or database.

Run a live refresh:

```powershell
python scout.py --open
```

This writes `report.html`. The first refresh can take several minutes as game metadata is fetched. API calls have timeouts and bounded retries. Later runs use metadata cached for 30 days. Prices and the library are fetched fresh each run. A failed refresh preserves the previous dated report.

## Daily refresh

```powershell
python scout.py --daily
```

This refreshes immediately and then every 24 hours, and retries on the next daily cycle after a failed refresh. Keep the process and computer running. Ctrl+C stops it. Daily refresh has **not** been installed or started automatically. For unattended use, Windows Task Scheduler can run the interpreter with the full path to `scout.py` once each day, without `--daily`. The program resolves all files relative to its own location, so the scheduler's working directory does not matter.

## Preferences

- `max_price`: maximum price in the selected country's currency.
- `min_discount`: minimum percentage reduction.
- `favorite_tags`: tags to boost, such as `["RPG", "Turn-Based Strategy"]`; useful with little play history.
- `excluded_tags`: tags to exclude, case insensitive.
- `ignored_appids`: Steam app IDs to hide, as integers.
- `library_sample`: how many played-library entries to enrich; default 40.
- `max_deals`: number of offers to scan from the provider's default ordering; default 200. This is a bounded sample, **not all Steam sales**. Increase for wider coverage and more API calls.
- `top_n`: maximum recommendations to show.

All owned IDs are excluded even when only a sample is used to learn preferences. Entries without a verified Steam app ID are skipped. DLC and bundles are skipped. Expired offers are excluded when the provider supplies an expiry.

## Ranking

Played games contribute tag weights using capped logarithmic total playtime, plus recent playtime when Steam includes it. Unplayed games do not shape the taste profile. Manually selected tags add weight. Each candidate receives up to 70 points from its three strongest matching tags, 20 from Steam review quality (smoothed toward 50% for small review counts), and 10 from the discount. This is an initial heuristic, not a trained model or a probability of liking a game. Missing reviews use a neutral value. Without matching tags, recommendations are labeled discovery picks.

Ownership is based on Steam's returned owned-games list; family-shared games, alternative editions, and bundles may not map to that list. Pricing is a snapshot of the provider's data; check the offer before buying. No purchases or messages are sent.

## Project structure and future website

`scout.py` keeps API access (`Client`, `collect`), ranking (`taste`, `rank`), persistence, and HTML rendering in separate functions. This keeps the personal version small while allowing a later API/web frontend to reuse the ranking logic. Before public hosting, add user authentication, per-user storage, managed secrets, job scheduling, rate limiting, and provider terms review; the current local tool is not a public server.

## Validation

```powershell
python -m unittest -v
```

The tests exercise exclusion, preference ranking, expiry handling, safe HTML output, privacy failures, and pagination with mocked API responses. Live account/API integration requires your credentials and has not been verified in this build.

Sources: [Steam player API](https://partner.steamgames.com/doc/webapi/IPlayerService), [IsThereAnyDeal API and terms](https://docs.isthereanydeal.com/).
