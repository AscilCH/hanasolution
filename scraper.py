"""
Multi-source async phone scraper engine for Tunisian companies.
Uses aiohttp for all HTTP. No bs4/openpyxl needed.
"""
import aiohttp
import asyncio
import re
import urllib.parse

# ── Arabic prefix stripping ─────────────────────────────────
ARABIC_PREFIXES = [
    'ودادية أعوان وموظفي',
    'ودادية موظفي وعملة',
    'ودادية موظفي ومتقاعدي',
    'ودادية أعوان ومتقاعدي',
    'ودادية أعوان وموظفي',
    'تعاونية موظفي وعملة',
    'تعاونية أعوان وموظفي',
    'ودادية أعوان',
    'ودادية موظفي',
    'ودادية أساتذة',
    'ودادية إطارات وأعوان',
    'جمعية أعوان',
    'تعاونية موظفي',
    'تعاونية أعوان',
    'النقابة التونسية ل',
    'الهيئة الجهوية ل',
    'الهيئة الجهوية لعمادة',
    'الشركة التونسية ل',
    'المؤسسة التونسية ل',
    'ودادية',
    'تعاونية',
    'جمعية',
]

ARABIC_CONNECTORS = ['أعوان', 'موظفي', 'عمال', 'إطارات', 'وأعوان', 'ومتقاعدي']

def parse_company_name(name: str) -> dict:
    """Parse an Arabic/mixed company name into searchable components."""
    original = name.strip()
    clean = original

    for prefix in ARABIC_PREFIXES:
        if clean.startswith(prefix):
            clean = clean[len(prefix):].strip()
            for conn in ARABIC_CONNECTORS:
                if clean.startswith(conn):
                    clean = clean[len(conn):].strip()
            break

    # Remove leading dashes or bullet chars
    clean = re.sub(r'^[\s—–\-•]+', '', clean).strip()

    abbreviations = re.findall(r'\b[A-Z]{2,}\b', original)
    english_parts = re.findall(r'[A-Za-zÀ-ÿ][A-Za-zÀ-ÿ\s&.\'-]+', original)
    english = ' '.join(p.strip() for p in english_parts if len(p.strip()) > 1)

    return {
        'original': original,
        'clean': clean,
        'abbreviations': abbreviations,
        'english': english,
    }


# ── Phone extraction ────────────────────────────────────────
PHONE_PATTERNS = [
    re.compile(r'(?:\+216|00216|\(216\))[\s./-]*(\d{2})[\s./-]*(\d{3})[\s./-]*(\d{3})'),
    re.compile(r'(?<!\d)([2-9]\d)[\s./-](\d{3})[\s./-](\d{3})(?!\d)'),
    re.compile(r'(?<!\d)([2-9]\d)(\d{3})(\d{3})(?!\d)'),
]

def extract_phones(text: str) -> list[str]:
    """Extract valid 8-digit Tunisian phone numbers. Returns formatted strings."""
    if not text:
        return []
    phones = set()
    for pat in PHONE_PATTERNS:
        for m in pat.finditer(text):
            a, b, c = m.groups()
            if a[0] in '23456789':
                phones.add(f"{a} {b} {c}")
    return sorted(phones)


def clean_html(html: str) -> str:
    """Strip HTML to plain text."""
    t = re.sub(r'<script[^>]*>.*?</script>', ' ', html, flags=re.DOTALL | re.I)
    t = re.sub(r'<style[^>]*>.*?</style>', ' ', t, flags=re.DOTALL | re.I)
    t = re.sub(r'<!--.*?-->', ' ', t, flags=re.DOTALL)
    t = re.sub(r'<[^>]+>', ' ', t)
    for ent, ch in [('&amp;','&'),('&lt;','<'),('&gt;','>'),
                    ('&nbsp;',' '),('&#160;',' '),('&quot;','"'),('&#39;',"'")]:
        t = t.replace(ent, ch)
    return re.sub(r'\s+', ' ', t).strip()


# ── Skip-list for irrelevant domains ────────────────────────
SKIP_DOMAINS = {
    'facebook.com','twitter.com','linkedin.com','wikipedia.org',
    'youtube.com','instagram.com','tiktok.com','x.com',
    'pinterest.com','reddit.com','amazon.com',
    'gov.sa','hrsd.gov.sa','aawan.org.sa','mqalaty.net',
    'mosoah.com','almrsal.com','mawdoo3.com','arageek.com',
    'google.com','bing.com','yahoo.com',
}

CONTACT_RE = re.compile(
    r'contact|nous.contacter|اتصل|contactez|about|a-propos|qui.sommes|من.نحن|siege',
    re.I,
)

def _is_skip(url: str) -> bool:
    host = urllib.parse.urlparse(url).netloc.lower()
    return any(sd in host for sd in SKIP_DOMAINS)


def _find_contact_links(html: str, base_url: str) -> list[str]:
    base_host = urllib.parse.urlparse(base_url).netloc.lower()
    links = set()
    for m in re.finditer(r'<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>', html, re.I | re.DOTALL):
        href, anchor = m.group(1), m.group(2)
        if CONTACT_RE.search(href) or CONTACT_RE.search(anchor):
            try:
                full = urllib.parse.urljoin(base_url, href)
                if urllib.parse.urlparse(full).netloc.lower() == base_host:
                    links.add(full)
            except Exception:
                pass
        if len(links) >= 5:
            break
    return list(links)


# ── Scraper engine ───────────────────────────────────────────
class PhoneScraper:
    """Multi-source async phone number finder for Tunisian companies."""

    SEARCH_DELAY = 2.0
    SCRAPE_DELAY = 0.8
    TIMEOUT = aiohttp.ClientTimeout(total=12)
    HEADERS = {
        'User-Agent': ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                       'AppleWebKit/537.36 (KHTML, like Gecko) '
                       'Chrome/120.0.0.0 Safari/537.36'),
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
        'Accept-Language': 'en-US,en;q=0.9,fr;q=0.8,ar;q=0.7',
    }

    def __init__(self):
        self.session: aiohttp.ClientSession | None = None

    async def start(self):
        if not self.session:
            self.session = aiohttp.ClientSession(
                headers=self.HEADERS, timeout=self.TIMEOUT,
            )

    async def stop(self):
        if self.session:
            await self.session.close()
            self.session = None

    # ── low-level helpers ────────────────────────────────────
    async def _get(self, url: str) -> str:
        try:
            async with self.session.get(url, allow_redirects=True) as r:
                ct = r.headers.get('content-type', '')
                if r.status < 400 and ('html' in ct or 'plain' in ct):
                    return await r.text(errors='replace')
        except Exception:
            pass
        return ''

    async def _head_ok(self, url: str) -> bool:
        try:
            async with self.session.head(url, allow_redirects=True) as r:
                return r.status < 400
        except Exception:
            return False

    async def _scrape_url(self, url: str) -> tuple[list[dict], list[str]]:
        """Scrape a page → (phone_results, contact_page_links)."""
        html = await self._get(url)
        if not html:
            return [], []
        text = clean_html(html)
        phones = extract_phones(text)
        raw = extract_phones(html)
        all_nums = sorted(set(phones + raw))
        results = [{'number': n, 'source_url': url, 'source_name': urllib.parse.urlparse(url).netloc}
                    for n in all_nums]
        contacts = _find_contact_links(html, url)
        return results, contacts

    async def _search_ddg(self, query: str) -> list[str]:
        """DuckDuckGo Lite search → list of result URLs."""
        try:
            async with self.session.post(
                'https://lite.duckduckgo.com/lite/',
                data={'q': query},
            ) as r:
                if r.status != 200:
                    return []
                html = await r.text(errors='replace')
            raw = re.findall(r'href="(https?://[^"]+)"', html)
            seen, out = set(), []
            for u in raw:
                if 'duckduckgo.com' in u:
                    continue
                if u not in seen:
                    seen.add(u)
                    out.append(u)
                if len(out) >= 6:
                    break
            # prioritise .tn domains
            tn = [u for u in out if '.tn' in urllib.parse.urlparse(u).netloc]
            rest = [u for u in out if '.tn' not in urllib.parse.urlparse(u).netloc]
            return (tn + rest)[:5]
        except Exception:
            return []

    # ── Source implementations ───────────────────────────────
    async def _source_direct_url(self, parsed: dict) -> list[dict]:
        """Try to guess company websites from abbreviations."""
        for ab in parsed['abbreviations']:
            a = ab.lower()
            for tpl in ['https://www.{}.com.tn', 'https://{}.com.tn',
                        'https://www.{}.tn', 'https://{}.tn']:
                url = tpl.format(a)
                if await self._head_ok(url):
                    results, contacts = await self._scrape_url(url)
                    for cl in contacts[:3]:
                        await asyncio.sleep(0.4)
                        more, _ = await self._scrape_url(cl)
                        results += more
                    if results:
                        return results
        return []

    async def _source_ddg(self, parsed: dict) -> list[dict]:
        """Search DuckDuckGo, follow top results, scrape phones."""
        queries = []
        eng = parsed['english']
        clean = parsed['clean']
        if eng:
            queries.append(f'{eng} site:.tn contact téléphone')
            queries.append(f'{eng} Tunisie téléphone contact')
        queries.append(f'{clean} تونس هاتف')
        queries.append(f'{clean} contact téléphone site:.tn')

        for q in queries[:3]:
            await asyncio.sleep(self.SEARCH_DELAY)
            urls = await self._search_ddg(q)
            for surl in urls[:3]:
                if _is_skip(surl):
                    continue
                await asyncio.sleep(self.SCRAPE_DELAY)
                results, contacts = await self._scrape_url(surl)
                for cl in contacts[:2]:
                    await asyncio.sleep(0.4)
                    more, _ = await self._scrape_url(cl)
                    results += more
                if results:
                    return results
        return []

    async def _source_pagesjaunes(self, parsed: dict) -> list[dict]:
        """Search PagesJaunes.tn."""
        keyword = parsed['english'] or parsed['clean']
        encoded = urllib.parse.quote(keyword)
        for url_tpl in [
            f'https://www.pagesjaunes.com.tn/recherche/q-{encoded}',
            f'https://www.pagesjaunes.com.tn/recherche?que={encoded}',
        ]:
            await asyncio.sleep(self.SCRAPE_DELAY)
            html = await self._get(url_tpl)
            if html:
                phones = extract_phones(clean_html(html))
                if phones:
                    return [{'number': n, 'source_url': url_tpl, 'source_name': 'PagesJaunes.tn'}
                            for n in phones]
        return []

    async def _source_ween(self, parsed: dict) -> list[dict]:
        """Search Ween.tn (Tunisian business directory)."""
        keyword = parsed['english'] or parsed['clean']
        encoded = urllib.parse.quote(keyword)
        url = f'https://ween.tn/recherche?q={encoded}'
        await asyncio.sleep(self.SCRAPE_DELAY)
        html = await self._get(url)
        if html:
            phones = extract_phones(clean_html(html))
            if phones:
                return [{'number': n, 'source_url': url, 'source_name': 'Ween.tn'} for n in phones]
        return []

    async def _source_facebook_ddg(self, parsed: dict) -> list[dict]:
        """Search DDG for Facebook pages, follow them, extract phones."""
        keyword = parsed['english'] or parsed['clean']
        await asyncio.sleep(self.SEARCH_DELAY)
        urls = await self._search_ddg(f'site:facebook.com "{keyword}" Tunisia')
        for u in urls[:2]:
            await asyncio.sleep(self.SCRAPE_DELAY)
            html = await self._get(u)
            if html:
                phones = extract_phones(clean_html(html))
                if phones:
                    return [{'number': n, 'source_url': u, 'source_name': 'Facebook'} for n in phones]
        return []

    # ── Main entry point ─────────────────────────────────────
    async def find_phones(self, company_name: str, category: str = '') -> list[dict]:
        """
        Search all sources for phone numbers.
        Returns list of {number, source_url, source_name}.
        Stops as soon as any source returns results.
        """
        parsed = parse_company_name(company_name)

        for source_fn in [
            self._source_direct_url,
            self._source_ddg,
            self._source_pagesjaunes,
            self._source_ween,
            self._source_facebook_ddg,
        ]:
            try:
                results = await source_fn(parsed)
                if results:
                    # de-duplicate by number
                    seen = set()
                    unique = []
                    for r in results:
                        if r['number'] not in seen:
                            seen.add(r['number'])
                            unique.append(r)
                    return unique[:5]      # max 5 numbers
            except Exception:
                continue

        return []
