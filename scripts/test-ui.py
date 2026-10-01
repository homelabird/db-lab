#!/usr/bin/env python3
"""Run the existing UI smoke in real Chromium with every HTTP request mocked."""
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]


def main():
    try:
        from playwright.sync_api import Error, sync_playwright
    except ImportError:
        print('BLOCKED: install scripts/requirements-ui.txt and run python -m playwright install chromium.', file=sys.stderr)
        return 127
    html = (ROOT / 'mvp-lab/mvp_app/index.html').read_text(encoding='utf-8')
    smoke = (ROOT / 'mvp-lab/tests/ui_smoke.js').read_text(encoding='utf-8')
    started = time.monotonic()
    with sync_playwright() as playwright:
        try:
            browser = playwright.chromium.launch()
        except Error as exc:
            if "Executable doesn't exist" in str(exc):
                print('BLOCKED: run python -m playwright install chromium.', file=sys.stderr)
                return 127
            raise
        try:
            for width, height in ((1280, 900), (390, 844)):
                context = browser.new_context(viewport={'width': width, 'height': height}, service_workers='block')
                try:
                    errors = []
                    def fixture(route):
                        if route.request.url == 'https://db-lab.invalid/':
                            route.fulfill(content_type='text/html', body=html)
                        elif route.request.url == 'https://db-lab.invalid/api/orders':
                            route.fulfill(json={'orders': []})
                        else:
                            errors.append('Unexpected request: ' + route.request.url)
                            route.abort()
                    context.route('**/*', fixture)
                    page = context.new_page()
                    page.on('pageerror', lambda error: errors.append(str(error)))
                    page.goto('https://db-lab.invalid/')
                    result = page.evaluate('() => Promise.race([' + smoke +
                                           ", new Promise((_, reject) => setTimeout(() => reject(Error('UI smoke timed out')), 30000))])")
                    assert isinstance(result, str) and result.startswith('PASS:'), result
                    assert not errors, errors
                    print(f'Chromium {width}x{height}: {result}')
                finally:
                    context.close()
        finally:
            browser.close()
    print(f'Ran 2 tests in {time.monotonic() - started:.3f}s')
    print('PASS: mocked browser UI only; no DB or live API was contacted.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
