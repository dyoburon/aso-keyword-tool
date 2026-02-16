#!/usr/bin/env python3
"""App Store keyword difficulty & opportunity scorer.

Queries the free Apple iTunes Search API to estimate how hard it would be
to rank for a given keyword, how much traffic the keyword gets, and
computes an opportunity score (traffic / difficulty).

Usage:
    python aso.py "virtual pet" --detailed
    python aso.py "virtual pet" "spirit pet" "ai companion" "pet game"
    python aso.py -f keywords.txt
    python aso.py "virtual pet" --json
"""

import argparse
import json
import plistlib
import sys
import time
from datetime import datetime, timezone

import requests
from tabulate import tabulate

# ── API Configuration ──────────────────────────────────────────────────────

SEARCH_URL = "https://itunes.apple.com/search"
HINTS_URL = "https://search.itunes.apple.com/WebObjects/MZSearchHints.woa/wa/hints"
RATE_LIMIT_DELAY = 3.5  # seconds between API calls (~17/min, under 20/min limit)
MAX_RESULTS = 200
TOP_N = 10  # top apps for detailed scoring

# ── Scoring Weights ────────────────────────────────────────────────────────

# Difficulty (higher weight = more influence on difficulty score)
TITLE_MATCH_WEIGHT = 4     # Do top apps target this keyword in their name?
RATING_COUNT_WEIGHT = 5    # How many ratings do top apps have? (proxy for installs)
SATURATION_WEIGHT = 3      # What % of results have the keyword in their title?
FRESHNESS_WEIGHT = 1       # Are top apps recently updated?

# Traffic (higher weight = more influence on traffic score)
SUGGEST_COUNT_WEIGHT = 6   # How many autocomplete suggestions? (0-10, strongest signal)
SUGGEST_MATCH_WEIGHT = 2   # Does our exact keyword appear in suggestions?
RESULT_COUNT_WEIGHT = 1    # How many total results? (weak signal - loose matching)
RATING_SPREAD_WEIGHT = 1   # Do mid-tier apps also have ratings?

# ── API Client ─────────────────────────────────────────────────────────────


class iTunesAPI:
    """Handles Apple API calls with rate limiting and retry."""

    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "ASO-Tool/1.0"})
        self.last_request_time = 0.0

    def _rate_limit(self):
        elapsed = time.time() - self.last_request_time
        if elapsed < RATE_LIMIT_DELAY:
            time.sleep(RATE_LIMIT_DELAY - elapsed)
        self.last_request_time = time.time()

    def _request(self, url: str, params: dict, max_retries: int = 3,
                 extra_headers: dict | None = None) -> requests.Response | None:
        for attempt in range(max_retries):
            try:
                self._rate_limit()
                resp = self.session.get(url, params=params, timeout=15,
                                        headers=extra_headers)
                if resp.status_code == 403 or resp.status_code == 429:
                    wait = 10 * (2 ** attempt)
                    print(f"\n  Rate limited. Waiting {wait}s...", flush=True)
                    time.sleep(wait)
                    continue
                resp.raise_for_status()
                return resp
            except requests.RequestException:
                if attempt == max_retries - 1:
                    return None
                time.sleep(5)
        return None

    def search(self, term: str, limit: int = MAX_RESULTS, country: str = "us") -> list[dict]:
        """Search the App Store for apps matching a term."""
        params = {
            "term": term,
            "entity": "software",
            "country": country,
            "limit": min(limit, MAX_RESULTS),
        }
        resp = self._request(SEARCH_URL, params)
        if resp is None:
            return []
        try:
            return resp.json().get("results", [])
        except (json.JSONDecodeError, ValueError):
            return []

    def get_suggestions(self, term: str) -> list[str]:
        """Get Apple App Store search autocomplete suggestions.

        Uses clientApplication=Software and the US store front header
        to get app-specific suggestions (up to 10 results vs 1 without).
        """
        params = {"term": term, "clientApplication": "Software"}
        resp = self._request(
            HINTS_URL, params,
            extra_headers={"X-Apple-Store-Front": "143441-1,29"},
        )
        if resp is None:
            return []
        try:
            data = plistlib.loads(resp.content)
            hints = data.get("hints", [])
            return [h["term"] for h in hints if isinstance(h, dict) and "term" in h]
        except Exception:
            return []


# ── Title Match Classification ─────────────────────────────────────────────


def classify_title_match(keyword: str, title: str) -> str:
    """Classify how well an app title matches a keyword.

    Returns: "exact", "broad", "partial", or "none"
    """
    kw = keyword.lower().strip()
    t = title.lower()

    # Exact: keyword appears as contiguous substring
    if kw in t:
        return "exact"

    # For multi-word keywords, check word-level matching
    kw_words = kw.split()
    if len(kw_words) > 1:
        t_words = t.split()
        # Broad: all keyword words present in title (any order)
        if all(any(kw_w in tw for tw in t_words) for kw_w in kw_words):
            return "broad"
        # Partial: some keyword words present
        if any(any(kw_w in tw for tw in t_words) for kw_w in kw_words):
            return "partial"

    return "none"


# ── Difficulty Sub-Scores ──────────────────────────────────────────────────


def score_title_matches(keyword: str, apps: list[dict]) -> dict:
    """Score how precisely top apps target this keyword in their titles."""
    top = apps[:TOP_N]
    if not top:
        return {"counts": {"exact": 0, "broad": 0, "partial": 0, "none": 0}, "score": 1.0}

    matches = [classify_title_match(keyword, app.get("trackName", "")) for app in top]
    counts = {
        "exact": matches.count("exact"),
        "broad": matches.count("broad"),
        "partial": matches.count("partial"),
        "none": matches.count("none"),
    }
    # Weighted: exact matches = hardest, none = easiest
    raw = 10 * counts["exact"] + 5 * counts["broad"] + 2.5 * counts["partial"]
    score = max(1.0, min(10.0, raw / len(top)))
    return {"counts": counts, "score": round(score, 2)}


def score_rating_counts(apps: list[dict]) -> dict:
    """Score average userRatingCount of top apps (proxy for installs)."""
    top = apps[:TOP_N]
    if not top:
        return {"avg_ratings": 0, "max_ratings": 0, "min_ratings": 0, "score": 1.0}

    counts = [app.get("userRatingCount", 0) for app in top]
    avg = sum(counts) / len(counts)

    # Scale: 0 -> 1, 100k+ -> 10
    max_threshold = 100_000
    score = 1 + 9 * min(avg, max_threshold) / max_threshold

    return {
        "avg_ratings": round(avg),
        "max_ratings": max(counts),
        "min_ratings": min(counts),
        "score": round(score, 2),
    }


def score_saturation(keyword: str, apps: list[dict]) -> dict:
    """What % of top 25 results have the keyword in their title?"""
    top25 = apps[:25]
    if not top25:
        return {"title_match_count": 0, "total_checked": 0, "percentage": 0, "score": 1.0}

    has_keyword = sum(
        1 for app in top25
        if keyword.lower() in app.get("trackName", "").lower()
    )
    pct = has_keyword / len(top25)
    score = 1 + 9 * pct  # 0% -> 1, 100% -> 10

    return {
        "title_match_count": has_keyword,
        "total_checked": len(top25),
        "percentage": round(pct * 100, 1),
        "score": round(score, 2),
    }


def score_freshness(apps: list[dict]) -> dict:
    """Are top apps recently updated? Fresh = active = harder to displace."""
    top = apps[:TOP_N]
    if not top:
        return {"avg_days_since_update": 999, "score": 1.0}

    days_list = []
    for app in top:
        updated = app.get("currentVersionReleaseDate", app.get("releaseDate", ""))
        if updated:
            try:
                dt = datetime.fromisoformat(updated.replace("Z", "+00:00"))
                days = (datetime.now(timezone.utc) - dt).days
                days_list.append(max(0, days))
            except (ValueError, TypeError):
                pass

    if not days_list:
        return {"avg_days_since_update": 999, "score": 1.0}

    avg_days = sum(days_list) / len(days_list)

    # Recently updated = harder. 0 days -> 10, 500+ days -> 1
    max_days = 500
    score = 1 + 9 * (max_days - min(avg_days, max_days)) / max_days

    return {
        "avg_days_since_update": round(avg_days),
        "score": round(score, 2),
    }


# ── Traffic Sub-Scores ─────────────────────────────────────────────────────


def score_suggestion_count(suggestions: list[str]) -> dict:
    """How many autocomplete suggestions does Apple return?

    This is the strongest free traffic signal. Apple returns 0-10 suggestions
    via the MZSearchHints endpoint with clientApplication=Software.
    0 = nobody searches this. 10 = active keyword niche.
    """
    count = len(suggestions)
    # Linear scale: 0 suggestions -> 1, 10 suggestions -> 10
    score = 1 + 9 * min(count, 10) / 10

    return {
        "suggestion_count": count,
        "suggestions": suggestions[:5],  # show top 5 for context
        "score": round(score, 2),
    }


def score_suggestion_match(keyword: str, suggestions: list[str]) -> dict:
    """Does our exact keyword appear in the suggestions?

    If Apple suggests the exact keyword back, people are searching for it.
    If only variations appear, the keyword itself may not be searched directly.
    """
    kw_lower = keyword.lower().strip()
    exact_match = any(kw_lower == s.lower() for s in suggestions)
    prefix_match = any(s.lower().startswith(kw_lower) for s in suggestions)

    if exact_match:
        score = 10.0
    elif prefix_match:
        score = 6.0
    elif len(suggestions) > 0:
        score = 3.0  # suggestions exist but don't match our keyword
    else:
        score = 1.0

    return {
        "exact_match": exact_match,
        "prefix_match": prefix_match,
        "score": score,
    }


def score_result_count(apps: list[dict]) -> dict:
    """How many apps returned? More = keyword has broader relevance."""
    count = len(apps)
    score = 1 + 9 * min(count, MAX_RESULTS) / MAX_RESULTS
    return {
        "result_count": count,
        "hit_max": count >= MAX_RESULTS,
        "score": round(score, 2),
    }


def score_rating_spread(apps: list[dict]) -> dict:
    """Do mid-tier apps (rank 10-25) also have ratings? Indicates broad traffic."""
    mid_tier = apps[10:25] if len(apps) > 10 else apps
    if not mid_tier:
        return {"mid_tier_avg_ratings": 0, "score": 1.0}

    mid_ratings = [app.get("userRatingCount", 0) for app in mid_tier]
    avg_mid = sum(mid_ratings) / len(mid_ratings)

    # If mid-tier apps average 10k+ ratings, there's real traffic
    score = 1 + 9 * min(avg_mid, 10_000) / 10_000

    return {
        "mid_tier_avg_ratings": round(avg_mid),
        "score": round(score, 2),
    }


# ── Composite Scores ───────────────────────────────────────────────────────


def _weighted_aggregate(weights: list[float], scores: list[float]) -> int:
    """Compute weighted aggregate of 1-10 scores, normalized to 0-100."""
    total_weight = sum(weights)
    weighted_sum = sum(w * s for w, s in zip(weights, scores))
    min_possible = 1 * total_weight
    max_possible = 10 * total_weight
    normalized = (weighted_sum - min_possible) / (max_possible - min_possible)
    return round(normalized * 100)


def compute_difficulty(keyword: str, apps: list[dict]) -> dict:
    """Compute overall difficulty score (0-100). Lower = easier to rank."""
    title = score_title_matches(keyword, apps)
    ratings = score_rating_counts(apps)
    saturation = score_saturation(keyword, apps)
    freshness = score_freshness(apps)

    weights = [TITLE_MATCH_WEIGHT, RATING_COUNT_WEIGHT, SATURATION_WEIGHT, FRESHNESS_WEIGHT]
    scores = [title["score"], ratings["score"], saturation["score"], freshness["score"]]
    final = _weighted_aggregate(weights, scores)

    return {
        "score": final,
        "title_matches": title,
        "rating_counts": ratings,
        "saturation": saturation,
        "freshness": freshness,
    }


def compute_traffic(keyword: str, apps: list[dict], suggestions: list[str]) -> dict:
    """Estimate keyword traffic/demand (0-100). Higher = more traffic.

    Primary signal: suggestion count (0-10 from Apple's autocomplete).
    Secondary signals: exact match in suggestions, result count, rating spread.
    """
    suggest_count = score_suggestion_count(suggestions)
    suggest_match = score_suggestion_match(keyword, suggestions)
    result_count = score_result_count(apps)
    spread = score_rating_spread(apps)

    weights = [SUGGEST_COUNT_WEIGHT, SUGGEST_MATCH_WEIGHT,
               RESULT_COUNT_WEIGHT, RATING_SPREAD_WEIGHT]
    scores = [suggest_count["score"], suggest_match["score"],
              result_count["score"], spread["score"]]
    final = _weighted_aggregate(weights, scores)

    return {
        "score": final,
        "suggestion_count": suggest_count,
        "suggestion_match": suggest_match,
        "result_count": result_count,
        "rating_spread": spread,
    }


def compute_opportunity(traffic_score: int, difficulty_score: int) -> float:
    """The golden metric: traffic / difficulty. Higher = better opportunity."""
    if difficulty_score == 0:
        return float(traffic_score)
    return round(traffic_score / max(difficulty_score, 1), 2)


# ── Analysis ───────────────────────────────────────────────────────────────


def analyze_keyword(api: iTunesAPI, keyword: str, country: str = "us") -> dict:
    """Run full analysis on a single keyword."""
    apps = api.search(keyword, country=country)
    suggestions = api.get_suggestions(keyword)

    difficulty = compute_difficulty(keyword, apps)
    traffic = compute_traffic(keyword, apps, suggestions)
    opportunity = compute_opportunity(traffic["score"], difficulty["score"])

    # Top 5 competitors for display
    top5 = []
    for app in apps[:5]:
        top5.append({
            "name": app.get("trackName", "Unknown"),
            "developer": app.get("artistName", "Unknown"),
            "ratings": app.get("userRatingCount", 0),
            "rating": app.get("averageUserRating", 0),
            "genre": app.get("primaryGenreName", ""),
        })

    return {
        "keyword": keyword,
        "difficulty": difficulty,
        "traffic": traffic,
        "opportunity": opportunity,
        "result_count": len(apps),
        "top_competitors": top5,
    }


# ── Display ────────────────────────────────────────────────────────────────


def format_title_matches(counts: dict) -> str:
    """Format title match counts as a compact string."""
    parts = []
    for kind in ("exact", "broad", "partial", "none"):
        n = counts.get(kind, 0)
        if n > 0:
            parts.append(f"{n} {kind}")
    return " / ".join(parts) if parts else "0"


def difficulty_label(score: int) -> str:
    if score <= 20:
        return "Very Easy"
    elif score <= 40:
        return "Easy"
    elif score <= 60:
        return "Moderate"
    elif score <= 80:
        return "Hard"
    else:
        return "Very Hard"


def print_summary_table(results: list[dict]):
    """Print a sorted summary table of all analyzed keywords."""
    print()
    print("  App Store Keyword Analysis")
    print("  " + "=" * 80)
    print()

    rows = []
    for r in results:
        d = r["difficulty"]
        t = r["traffic"]
        rows.append([
            r["keyword"],
            f"{d['score']}",
            difficulty_label(d["score"]),
            f"{t['score']}",
            f"{r['opportunity']:.2f}",
            f"{d['rating_counts']['avg_ratings']:,}",
            format_title_matches(d["title_matches"]["counts"]),
        ])

    headers = ["Keyword", "Diff", "Level", "Traffic", "Opportunity", "Avg Ratings", "Title Matches"]
    print(tabulate(rows, headers=headers, tablefmt="simple", numalign="right", stralign="left"))

    print()
    print("  Sorted by: Opportunity (higher = better keyword to target)")
    print("  Difficulty: 0-100 (lower = easier) | Traffic: 0-100 (higher = more searches)")
    print()


def print_detailed(result: dict):
    """Print detailed breakdown for a single keyword."""
    r = result
    d = r["difficulty"]
    t = r["traffic"]

    print()
    print(f"  {'=' * 50}")
    print(f"  KEYWORD: \"{r['keyword']}\"")
    print(f"  {'=' * 50}")
    print()

    # Difficulty
    print(f"  DIFFICULTY: {d['score']}/100 ({difficulty_label(d['score'])})")
    print(f"  |")
    tm = d["title_matches"]
    print(f"  +-- Title Match Score: {tm['score']}/10")
    print(f"  |   {format_title_matches(tm['counts'])}")
    rc = d["rating_counts"]
    print(f"  +-- Competitor Strength: {rc['score']}/10")
    print(f"  |   Avg ratings: {rc['avg_ratings']:,}")
    print(f"  |   Range: {rc['min_ratings']:,} - {rc['max_ratings']:,}")
    sat = d["saturation"]
    print(f"  +-- Saturation: {sat['score']}/10")
    print(f"  |   {sat['title_match_count']}/{sat['total_checked']} top results have keyword in title ({sat['percentage']}%)")
    fr = d["freshness"]
    print(f"  +-- Freshness: {fr['score']}/10")
    print(f"      Avg days since update: {fr['avg_days_since_update']}")
    print()

    # Traffic
    print(f"  TRAFFIC: {t['score']}/100")
    print(f"  |")
    sc = t["suggestion_count"]
    print(f"  +-- Suggestion Count: {sc['suggestion_count']}/10 (score: {sc['score']}/10)")
    if sc["suggestions"]:
        for s in sc["suggestions"]:
            print(f"  |   - \"{s}\"")
    sm = t["suggestion_match"]
    match_status = "exact" if sm["exact_match"] else ("prefix" if sm["prefix_match"] else "none")
    print(f"  +-- Keyword Match: {match_status} (score: {sm['score']}/10)")
    rc2 = t["result_count"]
    print(f"  +-- Result count: {rc2['result_count']}/{MAX_RESULTS}{'+ (max)' if rc2['hit_max'] else ''}")
    rs = t["rating_spread"]
    print(f"  +-- Mid-tier avg ratings: {rs['mid_tier_avg_ratings']:,}")
    print()

    # Opportunity
    print(f"  OPPORTUNITY: {r['opportunity']:.2f}")
    print()

    # Top competitors
    if r["top_competitors"]:
        print(f"  TOP COMPETITORS:")
        for i, comp in enumerate(r["top_competitors"], 1):
            stars = f"{comp['rating']:.1f}" if comp["rating"] else "N/A"
            print(f"  {i}. {comp['name']}")
            print(f"     {comp['ratings']:,} ratings | {stars} stars | {comp['genre']} | {comp['developer']}")
        print()


# ── CLI ────────────────────────────────────────────────────────────────────


def main():
    parser = argparse.ArgumentParser(
        description="App Store keyword difficulty & opportunity scorer",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python aso.py "virtual pet" --detailed
  python aso.py "virtual pet" "spirit pet" "ai companion" "pet game"
  python aso.py -f keywords.txt
  python aso.py "virtual pet" --json
        """,
    )
    parser.add_argument("keywords", nargs="*", help="Keywords to analyze")
    parser.add_argument("-f", "--file", help="File with keywords (one per line)")
    parser.add_argument("-c", "--country", default="us", help="Country code (default: us)")
    parser.add_argument("-d", "--detailed", action="store_true",
                        help="Show detailed breakdown for each keyword")
    parser.add_argument("--json", action="store_true", help="Output as JSON")

    args = parser.parse_args()

    # Collect keywords
    keywords = list(args.keywords)
    if args.file:
        try:
            with open(args.file) as f:
                keywords.extend(line.strip() for line in f if line.strip() and not line.startswith("#"))
        except FileNotFoundError:
            print(f"  Error: File not found: {args.file}", file=sys.stderr)
            sys.exit(1)

    if not keywords:
        parser.error("Provide keywords as arguments or via -f/--file")

    # Deduplicate while preserving order
    seen = set()
    unique_keywords = []
    for kw in keywords:
        kw_lower = kw.lower().strip()
        if kw_lower not in seen:
            seen.add(kw_lower)
            unique_keywords.append(kw.strip())
    keywords = unique_keywords

    # Send progress to stderr so --json output stays clean on stdout
    log = sys.stderr if args.json else sys.stdout

    print(file=log)
    print(f"  Analyzing {len(keywords)} keyword(s)...", file=log)
    print(f"  (~{len(keywords) * 7}s estimated, {RATE_LIMIT_DELAY}s between API calls)", file=log)
    print(file=log)

    api = iTunesAPI()
    results = []

    for i, kw in enumerate(keywords, 1):
        print(f"  [{i}/{len(keywords)}] {kw}...", end="", flush=True, file=log)
        result = analyze_keyword(api, kw, country=args.country)
        d = result["difficulty"]["score"]
        t = result["traffic"]["score"]
        o = result["opportunity"]
        print(f" D:{d} T:{t} O:{o:.2f}", file=log)
        results.append(result)

    # Sort by opportunity (descending)
    results.sort(key=lambda r: r["opportunity"], reverse=True)

    if args.json:
        print(json.dumps(results, indent=2))
    else:
        print_summary_table(results)
        if args.detailed:
            for r in results:
                print_detailed(r)


if __name__ == "__main__":
    main()
