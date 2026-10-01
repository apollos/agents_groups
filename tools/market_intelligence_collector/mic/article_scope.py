"""Conservative HTML article scoping; no whole-page fallback in strict mode.

DOM containers are necessary extraction hints, not proof of factual relevance.
The rendered HTML stays transient. Reports contain selectors/counts only.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import re

from bs4 import BeautifulSoup
from mic.utils import normalize_ws


ROOT_SELECTORS = (
    '[itemprop~="articleBody"]',
    '#article_cont .cc-article',
    '.article-content, .article_content, #article-content, #article_content, '
    '.article-body, .article_body, #article-body, #article_body, '
    '.news-content, .news_content, #news-content, #news_content',
    'article',
)
CHROME = re.compile(
    r'(?:^|[\s_\-])(?:related|recommend(?:ed|ation|ations)?|sidebar|'
    r'comments?|share|social|ranking|hot-news|advertisement|advert|ads|'
    r'pagination|breadcrumb|footer|navigation)(?:$|[\s_\-])', re.I)
BOUNDARY_LABELS = {'相关阅读', '相关推荐', '相关新闻', '推荐阅读', '热门新闻',
                   '延伸阅读', '猜你喜欢', 'Related articles', 'Related news',
                   'Recommended reading'}


@dataclass
class ArticleExtraction:
    title: str = ''
    publish_time: str | None = None
    body: str = ''
    tables: list[str] = field(default_factory=list)
    image_urls: list[str] = field(default_factory=list)
    report: dict = field(default_factory=dict)


def _signature(node):
    return ' '.join([str(node.get('id', '')), *node.get('class', [])])


def _chain_root(nodes):
    """Return the narrowest candidate only if all candidates are nested."""
    nodes = list({id(n): n for n in nodes}.values())
    if not nodes:
        return None
    for node in nodes:
        parents = {id(p) for p in node.parents}
        if all(id(other) == id(node) or id(other) in parents for other in nodes):
            return node
    return None


def _is_related_boundary(text):
    """Recognize a leading section label, optionally followed by colon/title.

    A mid-sentence mention or a longer prose word is not a boundary.
    """
    if text in BOUNDARY_LABELS:
        return True
    return any(re.match(r'\A' + re.escape(label) + r'\s*[:：]', text)
               for label in BOUNDARY_LABELS)


def extract_article(html, reader):
    soup = BeautifulSoup(html, 'lxml')
    page_title = normalize_ws(soup.title.get_text(' ', strip=True)) if soup.title else ''
    published = None
    for name in ('article:published_time', 'publishdate', 'pubdate', 'date'):
        tag = soup.find('meta', attrs={'property': name}) or soup.find('meta', attrs={'name': name})
        if tag and tag.get('content'):
            published = tag['content']
            break
    removed = 0
    for node in list(soup.find_all(True)):
        if node.attrs is None:
            continue
        excluded = node.name in {'script', 'style', 'nav', 'footer', 'aside', 'noscript', 'form'}
        excluded |= node.get('role') in {'navigation', 'complementary', 'dialog'}
        excluded |= node.has_attr('hidden') or node.get('aria-hidden') == 'true'
        excluded |= bool(CHROME.search(_signature(node)))
        if excluded:
            node.decompose()
            removed += 1
    title = page_title or (normalize_ws(soup.h1.get_text(' ', strip=True)) if soup.h1 else '')
    result = ArticleExtraction(title=title, publish_time=published,
                               report={'status': 'unresolved', 'selector': None,
                                       'removed_containers': removed})
    root = None
    for selector in ROOT_SELECTORS:
        candidates = soup.select(selector)
        if candidates:
            root = _chain_root(candidates)
            result.report.update(selector=selector, candidate_count=len(candidates))
            if root is None:
                result.report['reason'] = 'ambiguous_article_containers'
                return result
            break
    if root is None:
        result.report['reason'] = 'article_container_not_found'
        # A small visible excerpt is used only to classify challenge pages.
        result.report['challenge_excerpt'] = normalize_ws(soup.get_text(' ', strip=True))[:600]
        return result

    # A boundary heading marks the end of the article, even without a class.
    # Snapshot the document-order tags first; remove backwards to preserve nodes.
    nodes = list(root.find_all(True))
    boundary = next((i for i, node in enumerate(nodes)
                     if node.name in {'h2', 'h3', 'h4', 'div', 'p', 'strong'}
                     and _is_related_boundary(normalize_ws(node.get_text(' ', strip=True)))), None)
    if boundary is not None:
        for node in reversed(nodes[boundary:]):
            if node.attrs is not None:
                node.decompose()
        result.report['truncated_at_related_heading'] = True
    heading = root.find('h1')
    if heading:
        result.title = normalize_ws(heading.get_text(' ', strip=True)) or title
    # A page-level article header may be outside the body container. Never use
    # a recommendation date from elsewhere on the page as publication time.
    result.publish_time = published or reader._guess_publish_time(root)
    text = normalize_ws(root.get_text(' ', strip=True))
    links = root.find_all('a')
    linked_chars = sum(len(normalize_ws(n.get_text(' ', strip=True))) for n in links)
    if len(links) >= 3 and linked_chars / max(len(text), 1) > 0.45:
        result.report['reason'] = 'link_list_not_article'
        return result

    result.image_urls = reader._collect_image_urls(root)
    for table in list(root.find_all('table')):
        rows = []
        for tr in table.find_all('tr')[:30]:
            cells = [normalize_ws(c.get_text(' ', strip=True)) for c in tr.find_all(['th', 'td'])]
            cells = [c for c in cells if c]
            if cells:
                rows.append(' | '.join(cells))
        if rows and len(result.tables) < 5:
            result.tables.append('\n'.join(rows))
        table.decompose()
    # The h1 is already a dedicated title passage, not independent evidence.
    for node in list(root.find_all('h1')):
        node.decompose()
    blocks, seen = [], set()
    tags = ['p', 'li', 'h2', 'h3', 'blockquote']
    for node in root.find_all(tags):
        if node.find(tags):
            continue  # do not repeat nested paragraphs through their parent li
        line = normalize_ws(node.get_text(' ', strip=True))
        if len(line) >= 8 and line not in seen and line != result.title:
            seen.add(line)
            blocks.append(line)
    if not blocks:
        # Plain div/br markup is allowed only INSIDE the chosen article root.
        for line in root.get_text('\n', strip=True).splitlines():
            line = normalize_ws(line)
            if len(line) >= 8 and line not in seen and line != result.title:
                seen.add(line)
                blocks.append(line)
    result.body = '\n'.join(blocks)
    result.report.update(status='scoped', body_blocks=len(blocks), tables=len(result.tables),
                         images=len(result.image_urls),
                         limitation='DOM范围筛选；不证明内容完整、主体归属或语义真实')
    return result
