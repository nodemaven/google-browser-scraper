# google-browser-scraper is now SERP Maven

This project was renamed to [serpmaven](https://pypi.org/project/serpmaven/) (SERP Maven).

```
pip install serpmaven
```

To keep the old names, install this package instead; it installs serpmaven too:

```
pip install google-browser-scraper
```

Installing `google-browser-scraper` installs `serpmaven` and keeps the old names
working: the `google-browser-scraper` command, `import google_browser_scraper`
(with a deprecation warning) and the `GBS_*` environment variables. New code
should use `serpmaven`, `import serpmaven` and `SERPMAVEN_*`.

Source and documentation: https://github.com/nodemaven/serpmaven
