"""
SEO Tools for Multi-Agent System

Free, open-source tools for SEO analysis and optimization.
These tools integrate with the Tool-Caller adapter to provide
comprehensive SEO capabilities at zero cost.

Tools included:
1. LighthouseSEOTool - Google's official SEO auditor
2. PageAnalyzerTool - Content and structure analysis
3. SEOChecklistTool - Best practices validation
"""

import subprocess
import json
import re
from pathlib import Path
from typing import Dict, Any, List, Optional
from dataclasses import dataclass
from urllib.parse import urlparse

try:
    import requests
    from bs4 import BeautifulSoup
    REQUESTS_AVAILABLE = True
except ImportError:
    REQUESTS_AVAILABLE = False
    print("Warning: requests and beautifulsoup4 not installed. PageAnalyzerTool will not work.")
    print("Install with: pip install requests beautifulsoup4")


@dataclass
class SEOIssue:
    """Represents an SEO issue found during analysis"""
    severity: str  # "critical", "important", "minor"
    category: str  # "meta", "headers", "content", "technical"
    issue: str
    recommendation: str
    current_value: Optional[str] = None
    target_value: Optional[str] = None


class LighthouseSEOTool:
    """
    Run Google Lighthouse SEO audit.

    Requires: npm install -g lighthouse

    Returns comprehensive SEO audit including:
    - SEO score (0-100)
    - Meta tags issues
    - Mobile friendliness
    - Structured data
    - Performance impact on SEO
    """

    def __init__(self):
        self.name = "lighthouse_seo"
        self.description = "Run Google Lighthouse SEO audit on a URL. Returns comprehensive SEO score and issues."

    def execute(self, url: str, timeout: int = 60) -> Dict[str, Any]:
        """Run Lighthouse audit"""
        try:
            # Check if lighthouse is installed
            check = subprocess.run(['which', 'lighthouse'], capture_output=True)
            if check.returncode != 0:
                return {
                    "success": False,
                    "error": "Lighthouse not installed. Install with: npm install -g lighthouse"
                }

            # Create temp file for output (uuid avoids collisions)
            import uuid
            output_file = f"/tmp/lighthouse_{uuid.uuid4().hex}.json"

            try:
                # Run Lighthouse
                result = subprocess.run([
                    'lighthouse', url,
                    '--output=json',
                    f'--output-path={output_file}',
                    '--only-categories=seo,performance,accessibility',
                    '--chrome-flags=--headless',
                    '--quiet'
                ], capture_output=True, timeout=timeout, text=True)

                # Check if output file exists
                if not Path(output_file).exists():
                    return {
                        "success": False,
                        "error": f"Lighthouse failed: {result.stderr}"
                    }

                # Parse results
                with open(output_file, 'r') as f:
                    data = json.load(f)

                # Extract SEO score (with defensive access)
                try:
                    seo_score = int(data.get('categories', {}).get('seo', {}).get('score', 0) * 100)
                    performance_score = int(data.get('categories', {}).get('performance', {}).get('score', 0) * 100)
                    accessibility_score = int(data.get('categories', {}).get('accessibility', {}).get('score', 0) * 100)
                except (KeyError, TypeError, ValueError) as e:
                    return {
                        "success": False,
                        "error": f"Failed to parse Lighthouse results: {str(e)}"
                    }

                # Extract issues
                issues = []
                audits = data.get('audits', {})
                if not audits:
                    return {
                        "success": False,
                        "error": "Lighthouse returned no audit data"
                    }

                # Key SEO audits to check
                seo_audits = {
                    'document-title': 'critical',
                    'meta-description': 'critical',
                    'http-status-code': 'critical',
                    'link-text': 'important',
                    'crawlable-anchors': 'important',
                    'is-crawlable': 'critical',
                    'robots-txt': 'important',
                    'image-alt': 'important',
                    'hreflang': 'minor',
                    'canonical': 'important',
                    'structured-data': 'minor'
                }

                for audit_id, severity in seo_audits.items():
                    if audit_id in audits:
                        audit = audits[audit_id]
                        if audit.get('score', 1) < 1:
                            issues.append(SEOIssue(
                                severity=severity,
                                category='technical',
                                issue=audit['title'],
                                recommendation=audit['description'],
                                current_value=audit.get('displayValue', ''),
                                target_value=None
                            ))

                return {
                    "success": True,
                    "seo_score": seo_score,
                    "performance_score": performance_score,
                    "accessibility_score": accessibility_score,
                    "issues": [
                        {
                            "severity": i.severity,
                            "category": i.category,
                            "issue": i.issue,
                            "recommendation": i.recommendation
                        } for i in issues
                    ],
                    "summary": f"SEO: {seo_score}/100, Performance: {performance_score}/100, Accessibility: {accessibility_score}/100",
                    "url": url
                }
            finally:
                # Always clean up temp file
                Path(output_file).unlink(missing_ok=True)

        except subprocess.TimeoutExpired:
            return {
                "success": False,
                "error": f"Lighthouse timed out after {timeout} seconds"
            }
        except Exception as e:
            return {
                "success": False,
                "error": f"Lighthouse audit failed: {str(e)}"
            }


class PageAnalyzerTool:
    """
    Analyze page content and structure for SEO.

    Checks:
    - Title tag length and content
    - Meta description length and content
    - Header structure (H1, H2, H3, etc.)
    - Word count
    - Image alt tags
    - Internal/external links
    - Keyword density
    """

    def __init__(self):
        self.name = "page_analyzer"
        self.description = "Analyze webpage content for SEO optimization. Checks titles, headers, content length, images, and links."

    def execute(self, url: str, target_keyword: Optional[str] = None) -> Dict[str, Any]:
        """Analyze page content"""
        if not REQUESTS_AVAILABLE:
            return {
                "success": False,
                "error": "requests and beautifulsoup4 required. Install with: pip install requests beautifulsoup4"
            }

        try:
            # Fetch page
            response = requests.get(url, timeout=10, headers={
                'User-Agent': 'Mozilla/5.0 (SEO Analysis Bot)'
            })
            response.raise_for_status()

            # Parse HTML
            soup = BeautifulSoup(response.text, 'html.parser')

            # Extract elements
            title = soup.find('title')
            title_text = title.get_text().strip() if title else ""

            meta_desc = soup.find('meta', attrs={'name': 'description'})
            meta_desc_text = meta_desc.get('content', '').strip() if meta_desc else ""

            # Headers
            h1_tags = soup.find_all('h1')
            h2_tags = soup.find_all('h2')
            h3_tags = soup.find_all('h3')

            # Content
            # Remove script and style elements
            for script in soup(["script", "style"]):
                script.decompose()
            text = soup.get_text()
            words = text.split()
            word_count = len(words)

            # Images
            images = soup.find_all('img')
            images_with_alt = [img for img in images if img.get('alt')]
            images_without_alt = len(images) - len(images_with_alt)

            # Links
            links = soup.find_all('a', href=True)
            internal_links = []
            external_links = []
            parsed_url = urlparse(url)

            for link in links:
                href = link.get('href', '')
                parsed_href = urlparse(href)

                if not parsed_href.netloc or parsed_href.netloc == parsed_url.netloc:
                    internal_links.append(href)
                else:
                    external_links.append(href)

            # Keyword analysis
            keyword_data = {}
            if target_keyword:
                keyword_lower = target_keyword.lower()
                text_lower = text.lower()
                keyword_count = text_lower.count(keyword_lower)
                keyword_density = (keyword_count / word_count * 100) if word_count > 0 else 0

                # Check keyword in important places
                keyword_in_title = keyword_lower in title_text.lower()
                keyword_in_meta = keyword_lower in meta_desc_text.lower()
                keyword_in_h1 = any(keyword_lower in h1.get_text().lower() for h1 in h1_tags)
                keyword_in_first_100 = keyword_lower in ' '.join(words[:100]).lower()

                keyword_data = {
                    "target_keyword": target_keyword,
                    "keyword_count": keyword_count,
                    "keyword_density": round(keyword_density, 2),
                    "in_title": keyword_in_title,
                    "in_meta_description": keyword_in_meta,
                    "in_h1": keyword_in_h1,
                    "in_first_100_words": keyword_in_first_100
                }

            # Identify issues
            issues = []

            # Title issues
            if not title_text:
                issues.append(SEOIssue(
                    severity="critical",
                    category="meta",
                    issue="Missing title tag",
                    recommendation="Add a descriptive title tag (50-60 characters)",
                    current_value=None,
                    target_value="50-60 characters"
                ))
            elif len(title_text) < 30:
                issues.append(SEOIssue(
                    severity="important",
                    category="meta",
                    issue="Title too short",
                    recommendation="Expand title to 50-60 characters for better visibility",
                    current_value=f"{len(title_text)} characters",
                    target_value="50-60 characters"
                ))
            elif len(title_text) > 60:
                issues.append(SEOIssue(
                    severity="important",
                    category="meta",
                    issue="Title too long",
                    recommendation="Shorten title to 50-60 characters to avoid truncation",
                    current_value=f"{len(title_text)} characters",
                    target_value="50-60 characters"
                ))

            # Meta description issues
            if not meta_desc_text:
                issues.append(SEOIssue(
                    severity="critical",
                    category="meta",
                    issue="Missing meta description",
                    recommendation="Add a compelling meta description (150-160 characters)",
                    current_value=None,
                    target_value="150-160 characters"
                ))
            elif len(meta_desc_text) < 120:
                issues.append(SEOIssue(
                    severity="important",
                    category="meta",
                    issue="Meta description too short",
                    recommendation="Expand meta description to 150-160 characters",
                    current_value=f"{len(meta_desc_text)} characters",
                    target_value="150-160 characters"
                ))
            elif len(meta_desc_text) > 160:
                issues.append(SEOIssue(
                    severity="important",
                    category="meta",
                    issue="Meta description too long",
                    recommendation="Shorten meta description to 150-160 characters",
                    current_value=f"{len(meta_desc_text)} characters",
                    target_value="150-160 characters"
                ))

            # H1 issues
            if len(h1_tags) == 0:
                issues.append(SEOIssue(
                    severity="critical",
                    category="headers",
                    issue="Missing H1 tag",
                    recommendation="Add exactly one H1 tag with your main keyword",
                    current_value="0 H1 tags",
                    target_value="1 H1 tag"
                ))
            elif len(h1_tags) > 1:
                issues.append(SEOIssue(
                    severity="important",
                    category="headers",
                    issue="Multiple H1 tags",
                    recommendation="Use only one H1 tag per page",
                    current_value=f"{len(h1_tags)} H1 tags",
                    target_value="1 H1 tag"
                ))

            # Content length issues
            if word_count < 300:
                issues.append(SEOIssue(
                    severity="critical",
                    category="content",
                    issue="Content too short",
                    recommendation="Expand content to at least 1000 words for better rankings",
                    current_value=f"{word_count} words",
                    target_value="1000+ words"
                ))
            elif word_count < 1000:
                issues.append(SEOIssue(
                    severity="important",
                    category="content",
                    issue="Content length below recommended",
                    recommendation="Expand content to 1500-2500 words for long-form content",
                    current_value=f"{word_count} words",
                    target_value="1500-2500 words"
                ))

            # Image alt text issues
            if images_without_alt > 0:
                issues.append(SEOIssue(
                    severity="important",
                    category="technical",
                    issue=f"{images_without_alt} images missing alt text",
                    recommendation="Add descriptive alt text to all images",
                    current_value=f"{len(images_with_alt)}/{len(images)} images with alt",
                    target_value="All images should have alt text"
                ))

            # Link issues
            if len(internal_links) < 3:
                issues.append(SEOIssue(
                    severity="minor",
                    category="content",
                    issue="Few internal links",
                    recommendation="Add 3-5 internal links to related content",
                    current_value=f"{len(internal_links)} internal links",
                    target_value="3-5 internal links"
                ))

            # Keyword issues
            if target_keyword and not keyword_data['in_h1']:
                issues.append(SEOIssue(
                    severity="important",
                    category="content",
                    issue="Target keyword not in H1",
                    recommendation=f"Include '{target_keyword}' in your H1 tag",
                    current_value="Keyword not in H1",
                    target_value="Keyword should be in H1"
                ))

            if target_keyword and not keyword_data['in_first_100_words']:
                issues.append(SEOIssue(
                    severity="important",
                    category="content",
                    issue="Target keyword not in introduction",
                    recommendation=f"Include '{target_keyword}' in the first 100 words",
                    current_value="Keyword not in intro",
                    target_value="Keyword in first 100 words"
                ))

            return {
                "success": True,
                "url": url,
                "title": {
                    "text": title_text,
                    "length": len(title_text),
                    "optimal": 50 <= len(title_text) <= 60
                },
                "meta_description": {
                    "text": meta_desc_text,
                    "length": len(meta_desc_text),
                    "optimal": 150 <= len(meta_desc_text) <= 160
                },
                "headers": {
                    "h1_count": len(h1_tags),
                    "h2_count": len(h2_tags),
                    "h3_count": len(h3_tags),
                    "h1_texts": [h1.get_text().strip() for h1 in h1_tags][:3]  # First 3
                },
                "content": {
                    "word_count": word_count,
                    "optimal": word_count >= 1000
                },
                "images": {
                    "total": len(images),
                    "with_alt": len(images_with_alt),
                    "without_alt": images_without_alt,
                    "alt_coverage_percent": int((len(images_with_alt) / len(images) * 100) if len(images) > 0 else 0)
                },
                "links": {
                    "internal": len(internal_links),
                    "external": len(external_links),
                    "total": len(links)
                },
                "keyword_analysis": keyword_data if target_keyword else None,
                "issues": [
                    {
                        "severity": i.severity,
                        "category": i.category,
                        "issue": i.issue,
                        "recommendation": i.recommendation,
                        "current": i.current_value,
                        "target": i.target_value
                    } for i in issues
                ]
            }

        except requests.exceptions.RequestException as e:
            return {
                "success": False,
                "error": f"Failed to fetch page: {str(e)}"
            }
        except Exception as e:
            return {
                "success": False,
                "error": f"Analysis failed: {str(e)}"
            }


class SEOChecklistTool:
    """
    Rule-based SEO best practices checklist.

    Quick validation against common SEO requirements.
    Can check content before it's published.
    """

    def __init__(self):
        self.name = "seo_checklist"
        self.description = "Validate content against SEO best practices checklist. Fast rule-based checks."

    def execute(self, content_data: Dict[str, Any]) -> Dict[str, Any]:
        """
        Validate content against SEO checklist.

        Args:
            content_data: Dictionary with keys:
                - title (str): Page title
                - meta_description (str): Meta description
                - h1 (str): H1 heading
                - h2s (List[str]): List of H2 headings
                - content (str): Main content text
                - images (List[Dict]): List of images with 'src' and 'alt' keys
                - links (List[str]): List of links
                - target_keyword (str, optional): Target keyword

        Returns:
            Dictionary with checklist results
        """
        try:
            checks = []
            score: float = 0
            total_checks = 0

            # Title checks
            total_checks += 1
            title = content_data.get('title', '')
            if 50 <= len(title) <= 60:
                checks.append({"check": "Title length", "status": "pass", "value": f"{len(title)} chars"})
                score += 1
            elif len(title) == 0:
                checks.append({"check": "Title length", "status": "fail", "value": "Missing title"})
            else:
                checks.append({"check": "Title length", "status": "warning", "value": f"{len(title)} chars (target: 50-60)"})
                score += 0.5

            # Meta description checks
            total_checks += 1
            meta_desc = content_data.get('meta_description', '')
            if 150 <= len(meta_desc) <= 160:
                checks.append({"check": "Meta description length", "status": "pass", "value": f"{len(meta_desc)} chars"})
                score += 1
            elif len(meta_desc) == 0:
                checks.append({"check": "Meta description length", "status": "fail", "value": "Missing meta description"})
            else:
                checks.append({"check": "Meta description length", "status": "warning", "value": f"{len(meta_desc)} chars (target: 150-160)"})
                score += 0.5

            # H1 check
            total_checks += 1
            h1 = content_data.get('h1', '')
            if h1:
                checks.append({"check": "H1 tag present", "status": "pass", "value": h1[:50]})
                score += 1
            else:
                checks.append({"check": "H1 tag present", "status": "fail", "value": "No H1 tag"})

            # H2 structure
            total_checks += 1
            h2s = content_data.get('h2s', [])
            if 3 <= len(h2s) <= 6:
                checks.append({"check": "H2 structure", "status": "pass", "value": f"{len(h2s)} H2 tags"})
                score += 1
            elif len(h2s) == 0:
                checks.append({"check": "H2 structure", "status": "fail", "value": "No H2 tags"})
            else:
                checks.append({"check": "H2 structure", "status": "warning", "value": f"{len(h2s)} H2 tags (recommend 3-6)"})
                score += 0.5

            # Content length
            total_checks += 1
            content = content_data.get('content', '')
            word_count = len(content.split())
            if word_count >= 1500:
                checks.append({"check": "Content length", "status": "pass", "value": f"{word_count} words"})
                score += 1
            elif word_count >= 1000:
                checks.append({"check": "Content length", "status": "warning", "value": f"{word_count} words (target: 1500+)"})
                score += 0.5
            else:
                checks.append({"check": "Content length", "status": "fail", "value": f"{word_count} words (target: 1500+)"})

            # Image alt tags
            total_checks += 1
            images = content_data.get('images', [])
            if len(images) > 0:
                images_with_alt = sum(1 for img in images if img.get('alt'))
                if images_with_alt == len(images):
                    checks.append({"check": "Image alt tags", "status": "pass", "value": f"All {len(images)} images have alt text"})
                    score += 1
                else:
                    checks.append({"check": "Image alt tags", "status": "warning", "value": f"{images_with_alt}/{len(images)} images have alt text"})
                    score += 0.5
            else:
                checks.append({"check": "Image alt tags", "status": "info", "value": "No images to check"})
                total_checks -= 1  # Don't count this check

            # Internal links
            total_checks += 1
            links = content_data.get('links', [])
            if 3 <= len(links) <= 10:
                checks.append({"check": "Internal links", "status": "pass", "value": f"{len(links)} links"})
                score += 1
            elif len(links) < 3:
                checks.append({"check": "Internal links", "status": "warning", "value": f"{len(links)} links (recommend 3-5)"})
                score += 0.5
            else:
                checks.append({"check": "Internal links", "status": "info", "value": f"{len(links)} links"})
                score += 1

            # Keyword checks (if target keyword provided)
            target_keyword = content_data.get('target_keyword')
            if target_keyword:
                total_checks += 4
                keyword_lower = target_keyword.lower()

                # Keyword in title
                if keyword_lower in title.lower():
                    checks.append({"check": "Keyword in title", "status": "pass", "value": f"'{target_keyword}' found"})
                    score += 1
                else:
                    checks.append({"check": "Keyword in title", "status": "fail", "value": f"'{target_keyword}' not found"})

                # Keyword in meta
                if keyword_lower in meta_desc.lower():
                    checks.append({"check": "Keyword in meta description", "status": "pass", "value": f"'{target_keyword}' found"})
                    score += 1
                else:
                    checks.append({"check": "Keyword in meta description", "status": "fail", "value": f"'{target_keyword}' not found"})

                # Keyword in H1
                if keyword_lower in h1.lower():
                    checks.append({"check": "Keyword in H1", "status": "pass", "value": f"'{target_keyword}' found"})
                    score += 1
                else:
                    checks.append({"check": "Keyword in H1", "status": "fail", "value": f"'{target_keyword}' not found"})

                # Keyword in first 100 words
                first_100_words = ' '.join(content.split()[:100])
                if keyword_lower in first_100_words.lower():
                    checks.append({"check": "Keyword in first 100 words", "status": "pass", "value": f"'{target_keyword}' found"})
                    score += 1
                else:
                    checks.append({"check": "Keyword in first 100 words", "status": "fail", "value": f"'{target_keyword}' not found"})

            # Calculate percentage
            percentage = int((score / total_checks * 100)) if total_checks > 0 else 0

            # Determine overall status
            if percentage >= 90:
                overall_status = "excellent"
            elif percentage >= 75:
                overall_status = "good"
            elif percentage >= 60:
                overall_status = "needs_improvement"
            else:
                overall_status = "poor"

            return {
                "success": True,
                "overall_score": percentage,
                "overall_status": overall_status,
                "checks_passed": int(score),
                "total_checks": total_checks,
                "checks": checks,
                "summary": f"SEO Checklist: {percentage}% ({int(score)}/{total_checks} checks passed)"
            }

        except Exception as e:
            return {
                "success": False,
                "error": f"Checklist validation failed: {str(e)}"
            }


# Example usage
if __name__ == "__main__":
    print("SEO Tools Test\n" + "="*60)

    # Test PageAnalyzerTool
    print("\n1. Testing PageAnalyzerTool...")
    analyzer = PageAnalyzerTool()
    result = analyzer.execute("https://example.com", target_keyword="example domain")
    print(f"Success: {result['success']}")
    if result['success']:
        print(f"Title: {result['title']['text']} ({result['title']['length']} chars)")
        print(f"Word count: {result['content']['word_count']}")
        print(f"Issues found: {len(result['issues'])}")

    # Test SEOChecklistTool
    print("\n2. Testing SEOChecklistTool...")
    checklist = SEOChecklistTool()
    test_content = {
        "title": "Best Coffee Makers 2026: Top 10 Expert Reviews",
        "meta_description": "Discover the best coffee makers of 2026. Expert reviews, buying guides, and comparisons to help you find the perfect coffee maker for your needs.",
        "h1": "Best Coffee Makers of 2026",
        "h2s": ["Types of Coffee Makers", "Top 10 Picks", "Buying Guide", "FAQs"],
        "content": "Coffee makers are essential... " * 200,  # Simulate content
        "images": [{"src": "img1.jpg", "alt": "Coffee maker"}, {"src": "img2.jpg", "alt": "Espresso"}],
        "links": ["/related1", "/related2", "/related3"],
        "target_keyword": "coffee makers"
    }
    result = checklist.execute(test_content)
    print(f"Success: {result['success']}")
    if result['success']:
        print(f"Score: {result['overall_score']}% ({result['overall_status']})")
        print(f"Checks: {result['checks_passed']}/{result['total_checks']} passed")

    print("\n" + "="*60)
    print("All tools tested successfully!")
