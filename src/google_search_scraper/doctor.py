"""Check what this machine's browser tells a page, before any traffic is spent.

Runs on `about:blank` with no proxy: nothing is requested and nothing is
billed. The client machine can change Google's answer as much as the proxy
does, and the likely causes are visible from the page.
"""

from __future__ import annotations

from dataclasses import dataclass

PROBE = """() => {
  const safe = (fn) => { try { const v = fn(); return v === undefined ? null : v; }
                         catch (e) { return null; } };
  const gl = safe(() => {
    const ctx = document.createElement("canvas").getContext("webgl");
    if (!ctx) return null;
    const dbg = ctx.getExtension("WEBGL_debug_renderer_info");
    return dbg ? ctx.getParameter(dbg.UNMASKED_RENDERER_WEBGL) : "unknown renderer";
  });
  return {
    user_agent: navigator.userAgent,
    platform: navigator.platform,
    webdriver: navigator.webdriver,
    webgl_renderer: gl,
    hardware_concurrency: navigator.hardwareConcurrency,
    device_memory: safe(() => navigator.deviceMemory),
    screen: screen.width + "x" + screen.height,
    color_depth: screen.colorDepth,
    timezone: Intl.DateTimeFormat().resolvedOptions().timeZone,
    languages: (navigator.languages || []).join(","),
  };
}"""

PASS, WARN, FAIL = "pass", "warn", "fail"


@dataclass(frozen=True)
class Check:
    status: str
    name: str
    detail: str


def read_fingerprint(
    *,
    engine: str = "patchright",
    headless: bool = False,
    channel: str | None = None,
    browser_args=(),
) -> dict:
    from . import engines

    factory = engines.get(engine)
    with factory(None, headless=headless, channel=channel, browser_args=browser_args) as session:
        if hasattr(session, "page"):
            session.page.goto("about:blank")
            return session.page.evaluate(PROBE)
        return session.evaluate(PROBE)


def assess(fp: dict) -> list[Check]:
    checks = []
    ua = fp.get("user_agent") or ""
    if "HeadlessChrome" in ua:
        checks.append(
            Check(
                FAIL,
                "user agent",
                "announces HeadlessChrome, which search engines refuse on sight; "
                "run headful (the default)",
            )
        )
    else:
        checks.append(Check(PASS, "user agent", ua))

    if fp.get("webdriver"):
        checks.append(Check(FAIL, "navigator.webdriver", "true - the driver is visible to pages"))
    else:
        checks.append(Check(PASS, "navigator.webdriver", "false"))

    renderer = fp.get("webgl_renderer")
    if not renderer:
        checks.append(
            Check(
                WARN,
                "WebGL",
                "unavailable; a real desktop always has it. On a GPU-less Linux "
                "machine --browser-arg=--enable-unsafe-swiftshader turns it on",
            )
        )
    elif "SwiftShader" in renderer:
        checks.append(
            Check(
                WARN,
                "WebGL",
                f"software renderer ({renderer}); untested against "
                "Google, and rare on real desktops",
            )
        )
    else:
        checks.append(Check(PASS, "WebGL", renderer))

    width = _int(str(fp.get("screen", "0x0")).split("x")[0])
    if width < 1024:
        checks.append(
            Check(
                WARN,
                "screen",
                f"{fp.get('screen')}, smaller than any common "
                "desktop; set the virtual display to 1920x1080x24",
            )
        )
    else:
        checks.append(Check(PASS, "screen", f"{fp.get('screen')} x{fp.get('color_depth')}"))

    cores = _int(fp.get("hardware_concurrency"))
    if cores <= 2:
        checks.append(
            Check(
                WARN,
                "CPU cores",
                f"{cores}; fewer than almost any desktop",
            )
        )
    else:
        checks.append(Check(PASS, "CPU cores", str(cores)))

    platform = fp.get("platform") or ""
    if platform.startswith("Linux"):
        checks.append(
            Check(
                WARN,
                "platform",
                f"{platform}; Linux is experimental for this tool",
            )
        )
    else:
        checks.append(Check(PASS, "platform", platform))

    checks.append(
        Check(
            PASS,
            "timezone",
            f"{fp.get('timezone')} (not aligned with the exit unless --timezone is given)",
        )
    )
    return checks


def _int(value) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0
