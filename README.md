Pinterest Image Crawler
=======================

Pincrawl is a simple CLI to crawl public Pinterest pages using a headless browser (Playwright), collect image URLs, and download them locally. It works with both search queries and direct Pinterest URLs (search pages, boards, topics, etc.).

Important: Respect Pinterest's Terms of Service and robots guidelines. Use responsibly for personal/educational purposes. Avoid high request volumes.

Features
--------

- Search or URL: Start from a query or any Pinterest page URL
- Host whitelist: Default to `i.pinimg.com` for safety; allow `*` to grab all
- Resolution promotion: Tries to prefer higher-res `736x`/`originals` on pinimg
- Concurrent downloads: Fast but respectful with progress logging
- Optional filtering: Drop tiny files and (optionally) small dimensions

Install
-------

- Python 3.10+
- Install runtime deps and Playwright browser:

```
pip install -U pincrawl
python -m playwright install chromium
```

If developing locally from this repo (pip):

```
pip install -U -e .
python -m playwright install chromium
```

For dimension filtering, install Pillow (optional):

```
pip install 'pincrawl[images]'
```

Using uv
--------

uv can manage the virtualenv and run commands without activating it. From the repo root:

```
# Create venv (uses .python-version = 3.10)
uv venv

# Install the project in editable mode
uv pip install -e .

# Optional: add Pillow for dimension checks
uv pip install '.[images]'

# Install the Chromium browser used by Playwright
uv run python -m playwright install chromium

# Run the CLI
uv run pincrawl --query "sunset photography" --max-images 100 --out images/sunsets
```

If you don’t want to install the console script, you can run directly:

```
uv run python main.py --query "sunset photography" --max-images 100 --out images/sunsets
```

Usage
-----

Using a search query:

```
pincrawl --query "sunset photography" --max-images 100 --out images/sunsets
```

Starting from a specific Pinterest URL (e.g., board or search page):

```
pincrawl --url "https://www.pinterest.com/search/pins/?q=minimalist%20poster" --max-images 80
```

Allow images from any host instead of the default `i.pinimg.com` whitelist:

```
pincrawl --query "interior design" --allowed-hosts "*"
```

CLI Options
-----------

- `--query`: Search query text. Mutually exclusive with `--url`.
- `--url`: Start from a Pinterest page URL (search, board, topic).
- `--max-images`: Maximum images to download (default: `50`).
- `--out`: Output directory for images (default: `images`).
- `--headful`: Run the browser with a visible window (default: headless).
- `--scrolls`: Maximum scroll passes while collecting URLs (default: `80`).
- `--concurrency`: Parallel download workers (default: `8`).
- `--timeout`: Per-download timeout in seconds (default: `20`).
- `--save-urls`: Optional path to save the collected URLs list.
- `--min-bytes`: Minimum file size to accept (default: `10000`).
- `--min-dim`: Minimum width/height in pixels to accept (default: `200`). Requires Pillow if you want dimension checking.
- `--allowed-hosts`: Comma-separated host whitelist (default: `i.pinimg.com`). Use `*` to allow any host.
- `--verbose`: Enable verbose debug logging.
- `--log-file`: Write logs to this file (optional).
- `--progress-every`: Log download progress every N completions (default: `10`).
- `--scroll-log-every`: Log scroll progress every N scrolls (default: `5`).

Notes
-----

- The tool does not log in. It extracts image URLs directly from IMG tags on public pages and prefers higher-resolution `pinimg` variants when possible.
- Pinterest markup and behavior can change; if extraction breaks, update the selectors/heuristics in `main.py`.
- Tune `--max-images`, `--scrolls`, and `--concurrency` conservatively to reduce load.

Developing
----------

- Install dev deps and run from source:

```
pip install -U -e .
python -m playwright install chromium
python main.py --query "example" --max-images 10 --verbose
```

Legal & Ethical
---------------

- Review and comply with Pinterest's Terms of Service and policies for your jurisdiction and use case.
- Only crawl content you’re allowed to. Do not bypass access controls.
- Be considerate with request volume and parallelism to avoid degrading service.

License
-------

MIT. See `LICENSE`.
