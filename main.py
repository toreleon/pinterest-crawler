import asyncio
import argparse
import hashlib
import os
import re
import logging
import sys
from io import BytesIO
from pathlib import Path
from typing import List, Set, Optional

import httpx
from urllib.parse import quote_plus, urlparse
from playwright.async_api import async_playwright


SEARCH_URL = "https://www.pinterest.com/search/pins/?q={query}"


async def collect_image_urls(page, max_images: int, max_scrolls: int, logger: logging.Logger, scroll_log_every: int) -> List[str]:
    """Scroll the page, collecting unique image URLs from IMG tags.

    The in-page extraction selects a "best" candidate per <img> based on src/srcset,
    preferring higher resolution when possible. Host filtering is applied later in Python.
    """
    collected: Set[str] = set()
    last_count = 0
    same_count_rounds = 0

    for i in range(max_scrolls):
        # Extract image URLs in-page
        urls = await page.evaluate(
            """
            () => {
              const parseSrcset = (srcset) => {
                // returns array of {url, score} where score is width (w) or density (x)
                return srcset.split(',').map(s => s.trim()).map(entry => {
                  const [u, d] = entry.split(/\s+/);
                  let score = 0;
                  if (d && d.endsWith('w')) {
                    score = parseInt(d);
                  } else if (d && d.endsWith('x')) {
                    score = parseFloat(d) * 1000; // weight density roughly
                  }
                  return { url: u, score };
                });
              };

              const pickBest = (img) => {
                const src = img.getAttribute('src') || '';
                const srcset = img.getAttribute('srcset') || '';
                const pool = [];
                if (src) pool.push({ url: src, score: 0 });
                if (srcset) {
                  for (const c of parseSrcset(srcset)) {
                    if (c.url) pool.push(c);
                  }
                }
                if (!pool.length) return null;
                // Prefer URLs containing '/736x/' or '/orig' if present, else highest score
                pool.sort((a,b) => (b.score - a.score));
                const byPref = pool.find(c => /\/736x\//.test(c.url) || /originals/.test(c.url));
                return byPref ? byPref.url : pool[0].url;
              };

              const imgs = Array.from(document.querySelectorAll('img'));
              const urls = new Set();
              for (const img of imgs) {
                const best = pickBest(img);
                if (best) urls.add(best);
              }
              return Array.from(urls);
            }
            """
        )
        for u in urls:
            collected.add(u)

        if i == 0 or (i + 1) % max(1, scroll_log_every) == 0:
            logger.info(f"Scroll {i+1}/{max_scrolls}: collected {len(collected)} URLs")

        if len(collected) >= max_images:
            break

        # Scroll further to load more content
        await page.evaluate(
            "() => window.scrollTo(0, document.body.scrollHeight)"
        )
        await page.wait_for_timeout(1200)

        # Heuristic: stop if no new images after several rounds
        if len(collected) == last_count:
            same_count_rounds += 1
        else:
            same_count_rounds = 0
        last_count = len(collected)
        if same_count_rounds >= 5:
            logger.info("No new images for several scrolls; stopping collection early")
            break

    return list(collected)[:max_images]


def _filename_for_url(url: str) -> str:
    # Derive extension from path if present; default to .jpg
    ext = os.path.splitext(url.split("?")[0])[1].lower()
    if ext not in {".jpg", ".jpeg", ".png", ".webp"}:
        ext = ".jpg"
    digest = hashlib.sha1(url.encode("utf-8")).hexdigest()[:16]
    return f"{digest}{ext}"


def _promote_pinimg_resolution(url: str) -> str:
    """Try to rewrite pinimg URLs to a higher-res variant (736x if possible)."""
    if "i.pinimg.com" not in url:
        return url
    u = url.split("?")[0]
    if "/originals/" in u:
        return url  # already high-res
    # Replace common size-coded segments with 736x
    # e.g., /236x/, /474x/, /564x/, /170x/, /75x75_RS/
    u2 = re.sub(r"/(?:\d+x(?:\d+)?(?:_[A-Za-z]+)?)\/", "/736x/", u)
    return url.replace(u, u2)


async def download_images(urls: List[str], out_dir: Path, concurrency: int, timeout: float, referer: str, min_bytes: int, min_dim: int, logger: logging.Logger, progress_every: int):
    out_dir.mkdir(parents=True, exist_ok=True)

    sem = asyncio.Semaphore(concurrency)

    async def fetch_one(client: httpx.AsyncClient, url: str):
        url = _promote_pinimg_resolution(url)
        fname = _filename_for_url(url)
        fpath = out_dir / fname
        if fpath.exists():
            return "skip"
        headers = {
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
            "Referer": referer,
        }
        try:
            async with sem:
                resp = await client.get(url, headers=headers, timeout=timeout, follow_redirects=True)
            resp.raise_for_status()
            content_type = resp.headers.get("content-type", "").lower()
            data = resp.content

            # Basic sanity check
            if not data or len(data) < max(1024, int(min_bytes)):
                return "tiny"

            # Adjust extension if webp
            if "image/webp" in content_type and fpath.suffix.lower() != ".webp":
                fpath = fpath.with_suffix(".webp")

            # Dimension check via Pillow (optional)
            if min_dim > 0:
                try:
                    from PIL import Image  # type: ignore
                except Exception:
                    # Pillow not available; skip dimension check
                    pass
                else:
                    try:
                        with Image.open(BytesIO(data)) as im:
                            w, h = im.size
                            if w < min_dim or h < min_dim:
                                return "tiny"
                    except Exception:
                        # If not an image or unreadable, mark error
                        return "err"

            fpath.write_bytes(data)
            return "ok"
        except Exception:
            return "err"

    async with httpx.AsyncClient() as client:
        tasks = [asyncio.create_task(fetch_one(client, u)) for u in urls]
        total = len(tasks)
        if total == 0:
            logger.info("No URLs to download")
            return
        ok = skip = tiny = err = 0
        completed = 0
        for coro in asyncio.as_completed(tasks):
            result = await coro
            completed += 1
            if result == "ok":
                ok += 1
            elif result == "skip":
                skip += 1
            elif result == "tiny":
                tiny += 1
            else:
                err += 1
            if completed % max(1, progress_every) == 0 or completed == total:
                logger.info(f"[{completed}/{total}] {ok} ok, {skip} skipped, {tiny} tiny, {err} errors")


async def crawl(
    query: Optional[str],
    start_url: Optional[str],
    allowed_hosts: Optional[Set[str]],
    max_images: int,
    out_dir: Path,
    headless: bool,
    max_scrolls: int,
    concurrency: int,
    timeout: float,
    save_urls: Path | None,
    min_bytes: int,
    min_dim: int,
    logger: logging.Logger,
    scroll_log_every: int,
    progress_every: int,
):
    if start_url:
        open_url = start_url
    else:
        open_url = SEARCH_URL.format(query=quote_plus(query or ""))
    logger.info(f"Opening: {open_url}")

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=headless)
        context = await browser.new_context(
            viewport={"width": 1280, "height": 2000},
            user_agent=(
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
            ),
        )
        page = await context.new_page()
        await page.goto(open_url, wait_until="domcontentloaded")
        await page.wait_for_timeout(1500)

        # Give Pinterest some time to hydrate initial content
        try:
            await page.wait_for_load_state("networkidle", timeout=10000)
        except Exception:
            pass

        urls = await collect_image_urls(page, max_images=max_images, max_scrolls=max_scrolls, logger=logger, scroll_log_every=scroll_log_every)
        await browser.close()

    # Filter and dedupe just in case
    urls = list(dict.fromkeys(urls))  # dedupe

    # Apply host whitelist filtering
    def _host_ok(u: str) -> bool:
        if not allowed_hosts or ("*" in allowed_hosts):
            return True
        try:
            host = urlparse(u).hostname or ""
        except Exception:
            return False
        return any(host == h or host.endswith("." + h) for h in allowed_hosts)

    if allowed_hosts is not None:
        urls = [u for u in urls if _host_ok(u)]

    logger.info(f"Collected {len(urls)} image URLs")

    if save_urls:
        save_urls.parent.mkdir(parents=True, exist_ok=True)
        save_urls.write_text("\n".join(urls))
        logger.info(f"Saved URLs to {save_urls}")

    await download_images(
        urls,
        out_dir=out_dir,
        concurrency=concurrency,
        timeout=timeout,
        referer="https://www.pinterest.com/",
        min_bytes=min_bytes,
        min_dim=min_dim,
        logger=logger,
        progress_every=progress_every,
    )


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Crawl images from Pinterest public pages (search, boards, etc.)")
    group = p.add_mutually_exclusive_group()
    group.add_argument("--query", default=None, help="Search query text (e.g., 'nail art')")
    group.add_argument("--url", dest="url", default=None, help="Start from a Pinterest page URL (search, board, topic)")
    p.add_argument("--max-images", type=int, default=50, help="Maximum images to download")
    p.add_argument("--out", default="images", help="Output directory for images")
    p.add_argument("--headful", action="store_true", help="Run browser non-headless (show window)")
    p.add_argument("--scrolls", type=int, default=80, help="Maximum scroll passes while collecting URLs")
    p.add_argument("--concurrency", type=int, default=8, help="Parallel download workers")
    p.add_argument("--timeout", type=float, default=20.0, help="Per-download timeout in seconds")
    p.add_argument("--save-urls", default=None, help="Optional path to save collected URLs list")
    p.add_argument("--min-bytes", type=int, default=10_000, help="Minimum file size in bytes to keep image")
    p.add_argument("--min-dim", type=int, default=200, help="Minimum width/height in pixels to keep image")
    p.add_argument("--allowed-hosts", default="i.pinimg.com", help="Comma-separated host whitelist (e.g. 'i.pinimg.com,pinimg.com'). Use '*' to allow any host.")
    p.add_argument("--verbose", action="store_true", help="Enable verbose debug logging")
    p.add_argument("--log-file", default=None, help="Optional file to write logs")
    p.add_argument("--progress-every", type=int, default=10, help="Log every N downloads completed")
    p.add_argument("--scroll-log-every", type=int, default=5, help="Log scroll progress every N scrolls")
    return p.parse_args()


def main():
    args = parse_args()
    # Setup logging
    logger = logging.getLogger("pincrawler")
    logger.setLevel(logging.DEBUG if args.verbose else logging.INFO)
    fmt = logging.Formatter("%(asctime)s | %(levelname)s | %(message)s")
    sh = logging.StreamHandler(stream=sys.stdout)
    sh.setLevel(logging.DEBUG if args.verbose else logging.INFO)
    sh.setFormatter(fmt)
    logger.handlers.clear()
    logger.addHandler(sh)
    if args.log_file:
        fh = logging.FileHandler(args.log_file)
        fh.setLevel(logging.DEBUG)
        fh.setFormatter(fmt)
        logger.addHandler(fh)
    out_dir = Path(args.out)
    save_urls = Path(args.save_urls) if args.save_urls else None
    headless = not args.headful
    allowed_hosts: Optional[Set[str]]
    if args.allowed_hosts is None:
        allowed_hosts = None
    else:
        hosts_str = str(args.allowed_hosts).strip()
        if hosts_str == "*":
            allowed_hosts = {"*"}
        else:
            allowed_hosts = {h.strip() for h in hosts_str.split(',') if h.strip()}

    if args.min_dim and args.min_dim > 0:
        try:
            import PIL  # noqa: F401
        except Exception:
            logger.warning("Pillow not installed; --min-dim check will be skipped.")
    asyncio.run(
        crawl(
            query=args.query,
            start_url=args.url,
            allowed_hosts=allowed_hosts,
            max_images=args.max_images,
            out_dir=out_dir,
            headless=headless,
            max_scrolls=args.scrolls,
            concurrency=args.concurrency,
            timeout=args.timeout,
            save_urls=save_urls,
            min_bytes=args.min_bytes,
            min_dim=args.min_dim,
            logger=logger,
            scroll_log_every=args.scroll_log_every,
            progress_every=args.progress_every,
        )
    )


if __name__ == "__main__":
    main()
