# Forever auction tracker (cloud)

Runs `track.py` every hour on GitHub Actions: downloads every WoW Forever EU auction house from Blizzard's API,
compares each with the previous hour and uploads one small file per run (`h-<UTC time>.json.gz`) to the release
**data**, next to `state.json.gz` (the previous snapshot, needed for the comparison). Files older than 30 days are
deleted (Blizzard's API terms). The desk on your PC pulls the new files with `cloud_sync.py`.

Setup (done once): repository secrets `BLIZZARD_CLIENT_ID` and `BLIZZARD_CLIENT_SECRET`, and a release tagged `data`.
Run by hand: Actions → track → Run workflow. File format: see the docstring at the top of `track.py`.
