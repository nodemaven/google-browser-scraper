# Usage endpoint

The Cloudflare Worker that receives serpmaven's usage ping. What the client sends and how to
turn it off is in [`src/serpmaven/usage.py`](../src/serpmaven/usage.py) and the main README.

Stored per ping, in the Workers Analytics Engine dataset `serpmaven_usage`: the serpmaven
version, OS, Python version, command, proxy kind (`nodemaven`, `nodemaven-demo`, `url`,
`none`), the country of the request and the machine's random id. Not stored: the address the
request came from, or anything about queries, results or proxies.

Deploy with [Wrangler](https://developers.cloudflare.com/workers/wrangler/): set the route in
`wrangler.toml`, run `wrangler deploy`, then put the URL in `ENDPOINT` in `usage.py`.

Runs in the last 30 days, by version (each row is sampled, so sum `_sample_interval`):

```sql
SELECT blob1 AS version, SUM(_sample_interval) AS runs
FROM serpmaven_usage
WHERE timestamp > NOW() - INTERVAL '30' DAY
GROUP BY version
ORDER BY runs DESC
```

Machines are counted by grouping on `index1`, the machine's id.
