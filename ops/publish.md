# Publishing to the site (shared by daily.md and pregame.md)

## Database (splits, line movement, Kalshi prices)
1. ArtifactData `list` on the site url for collections `splits`, `splitshist`, `kalshi` (query.limit 200).
   Build a JSON file mapping "collection/doc_id" -> version for every doc returned.
2. `python3 sync_site.py <that file>` prints one JSON list of writes per line (max 50 each).
3. ArtifactData `batch` with each list as `writes`. If a batch is refused for a version
   conflict, re-list, rebuild the file and retry once.

## Data files (daily only)
Publish site/index.html with these files (Artifact `publish`, `url` = the site, `file_path` = site/index.html,
`files` = {"data/<name>": "site/data/<name>"} for trends.json, props.json, model.json, injuries.json,
people.json, signals.json, propmodel.json, weather.json). Do NOT pass `capabilities` (keeps the database rules) or `icon`.
If the publish is refused because a published file wasn't read in this session, Artifact `read` that
path (out_dir in the scratchpad), then publish again.
