"""Optional offline Chromium captures. Install Playwright separately to use this."""
import argparse
import os
import struct
import zlib
from pathlib import Path
import tempfile

TIMEOUT_MS = 15_000
INSTALL_HINT = "Run python -m pip install playwright, then python -m playwright install chromium."

# Every await is bounded, including font loading and image decoding.
READY_SCRIPT = """async () => {
  let timer;
  try {
    await Promise.race([
      (async () => {
        await document.fonts.ready;
        if ([...document.fonts].some(font => font.status === 'error'))
          throw new Error('A document font failed to load');
        await Promise.all([...document.images].map(async image => {
          await image.decode();
          if (!image.naturalWidth) throw new Error('An image failed to load');
        }));
      })(),
      new Promise((_, reject) => { timer = setTimeout(() => reject(new Error('Fonts/images timed out')), 12000); })
    ]);
  } finally { clearTimeout(timer); }
}"""


def prepare_page(context, html):
    """Shared by capture and local visual QA; never permits external requests."""
    page = context.new_page()
    page.set_default_timeout(TIMEOUT_MS)
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.on("console", lambda message: errors.append(message.text) if message.type == "error" else None)
    uri = html.resolve().as_uri()

    def offline(route):
        if route.request.url.split("#", 1)[0] == uri or route.request.url.startswith("data:"):
            route.continue_()
        else:
            errors.append("Blocked non-embedded resource: " + route.request.url)
            route.abort()

    page.route("**/*", offline)
    page.goto(uri, wait_until="load", timeout=TIMEOUT_MS)
    page.add_style_tag(content="*,*::before,*::after{animation:none!important;transition:none!important;caret-color:transparent!important}html{scroll-behavior:auto!important}")
    page.evaluate(READY_SCRIPT)
    if errors:
        raise RuntimeError("; ".join(errors))
    return page, errors


def validate_png(path, expected_size):
    """Check complete PNG chunks, checksums and viewport dimensions."""
    data = Path(path).read_bytes()
    if not data.startswith(b"\x89PNG\r\n\x1a\n"):
        raise ValueError(f"Invalid PNG screenshot: {path}")
    offset, size, image_data = 8, None, False
    while offset + 12 <= len(data):
        length = int.from_bytes(data[offset:offset + 4], "big")
        kind = data[offset + 4:offset + 8]
        end = offset + 8 + length
        if end + 4 > len(data) or zlib.crc32(data[offset + 4:end]) != int.from_bytes(data[end:end + 4], "big"):
            raise ValueError(f"Incomplete or damaged PNG screenshot: {path}")
        if offset == 8:
            if kind != b"IHDR" or length != 13:
                raise ValueError(f"Missing PNG header: {path}")
            size = struct.unpack(">II", data[offset + 8:offset + 16])
        if kind == b"IDAT" and length:
            image_data = True
        if kind == b"IEND":
            if length or end + 4 != len(data) or not image_data or size != expected_size:
                break
            return
        offset = end + 4
    raise ValueError(f"Screenshot must be a complete {expected_size[0]} ? {expected_size[1]} PNG: {path}")


class ScreenshotSession:
    """One optional browser per batch, with per-page failures and reliable cleanup."""
    def __init__(self):
        self.playwright = None
        self.browser = None
        self.error = None

    def __enter__(self):
        try:
            from playwright.sync_api import sync_playwright
            self.playwright = sync_playwright().start()
            self.browser = self.playwright.chromium.launch(headless=True)
        except Exception as error:
            self.error = f"Chromium unavailable: {error}. {INSTALL_HINT}"
        return self

    def __exit__(self, *exc):
        try:
            if self.browser is not None:
                self.browser.close()
        finally:
            if self.playwright is not None:
                self.playwright.stop()

    def capture(self, html):
        if self.error:
            raise RuntimeError(self.error)
        html = Path(html).resolve()
        if not html.is_file():
            raise ValueError(f"HTML file not found: {html}")
        with self.browser.new_context(viewport={"width": 1440, "height": 1000},
                                      device_scale_factor=1, reduced_motion="reduce",
                                      service_workers="block") as context:
            page, errors = prepare_page(context, html)
            with tempfile.TemporaryDirectory(dir=html.parent) as folder:
                staged = Path(folder)
                outputs = []
                for name, width, height in (("desktop.png", 1440, 1000), ("mobile.png", 390, 844)):
                    page.set_viewport_size({"width": width, "height": height})
                    page.evaluate(READY_SCRIPT)
                    page.evaluate("window.scrollTo(0, 0)")
                    page.wait_for_function("window.scrollX === 0 && window.scrollY === 0", timeout=TIMEOUT_MS)
                    page.screenshot(path=str(staged / name), animations="disabled", timeout=TIMEOUT_MS)
                    validate_png(staged / name, (width, height))
                    outputs.append(html.parent / name)
                if errors:
                    raise RuntimeError("; ".join(errors))
                for output in outputs:
                    os.replace(staged / output.name, output)
        return outputs


def capture_batch(html_paths):
    """Standalone capture helper; never creates or removes fullpage.png."""
    paths = [Path(path).resolve() for path in html_paths]
    result = {"captured": [], "failed": []}
    if not paths:
        return result
    with ScreenshotSession() as session:
        for path in paths:
            try:
                result["captured"].append((path, session.capture(path)))
            except Exception as error:
                result["failed"].append((path, str(error)))
    return result


def print_capture_summary(result):
    print(f"Screenshots: {len(result['captured'])} pages captured | {len(result['failed'])} pages failed")
    for _, outputs in result["captured"]:
        for output in outputs:
            print(f"screenshot: {output}")
    for html, reason in result["failed"]:
        print(f"screenshot failed [{html}]: {reason}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("html", nargs="+", type=Path, help="Existing local index.html files")
    result = capture_batch(parser.parse_args().html)
    print_capture_summary(result)
    return 1 if result["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
