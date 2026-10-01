# google-search-scraper: headful Chromium on a virtual display, serving the
# SerpApi-style API on port 8000 by default.
#
#   docker build -t google-search-scraper .
#   docker run --rm google-search-scraper doctor
#   docker run --rm -p 8000:8000 -e GSS_API_KEY=... -e GSS_PROXY='http://u-{session}:p@host:port' \
#       google-search-scraper serve --host 0.0.0.0
#
# Headful on purpose: headless builds announce HeadlessChrome. A real display is
# replaced by Xvfb at 1920x1080x24, not Xvfb's small default, because the screen
# is visible to every page.
#
# WebGL: headful Chrome on a GPU-less Linux machine has no WebGL at all, which a
# real desktop always has, so the image turns on Chrome's software renderer.
# Run `doctor` to see what this container reports.

FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    DISPLAY=:99 \
    GSS_BROWSER_ARGS="--enable-unsafe-swiftshader"

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        xvfb xauth tini \
        fonts-liberation fonts-dejavu-core fonts-noto-core fonts-noto-color-emoji \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip install --no-cache-dir ".[nodemaven]" \
    && patchright install --with-deps chromium \
    && rm -rf /var/lib/apt/lists/*

COPY docker/entrypoint.sh /usr/local/bin/entrypoint
RUN chmod +x /usr/local/bin/entrypoint \
    && useradd --create-home scraper \
    && chmod -R a+rx /ms-playwright
USER scraper

EXPOSE 8000
ENTRYPOINT ["tini", "--", "/usr/local/bin/entrypoint"]
CMD ["serve", "--host", "0.0.0.0", "--port", "8000"]
