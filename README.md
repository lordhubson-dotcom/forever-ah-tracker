# Forever auction tracker

Records every WoW Forever EU auction house from Blizzard's Game Data API once an hour, using GitHub Actions.

Every 10 minutes `track.py --check` asks whether Blizzard has published a new snapshot (about 20 minutes past the
hour); only then does it download the previous snapshot, compare each house with the previous hour and upload one file
(`h-<UTC time>.json.gz`: per item prices, plus flows - new listings, units gone, sold, reposts - and a price ladder) to
the release **data**, next to `state.json.gz` and `latest.txt`. Files older than 30 days are deleted (Blizzard's API
terms). `keepalive.yml` commits weekly so GitHub doesn't switch the schedule off.

File format: see the docstring at the top of `track.py`. Data: Blizzard Entertainment's Game Data API.
Setup (once): repository secrets `BLIZZARD_CLIENT_ID` and `BLIZZARD_CLIENT_SECRET`, and a release tagged `data`.
