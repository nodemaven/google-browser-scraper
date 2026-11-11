# gserp is now SERP Maven

This project was renamed to [serpmaven](https://pypi.org/project/serpmaven/) (SERP Maven).

```
pip install serpmaven
```

To keep the old names, install this package instead; it installs serpmaven too:

```
pip install gserp
```

Installing `gserp` installs `serpmaven` and keeps the old names working: the
`gserp` command, `import gserp` (with a deprecation warning) and the `GSERP_*`
environment variables. New code should use `serpmaven`, `import serpmaven` and
`SERPMAVEN_*`.

Source and documentation: https://github.com/nodemaven/serpmaven
