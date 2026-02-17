# ASO Keyword Tool

Free App Store keyword research — no paid tools, no accounts, no API keys.

This tool hits two free, unauthenticated Apple APIs to score keywords on difficulty and traffic:

1. **iTunes Search API** — returns up to 200 apps per keyword with metadata (ratings, titles, update dates), used to measure how competitive a keyword is
2. **Apple MZSearchHints API** — returns autocomplete suggestions (0-10 per keyword), used as a traffic proxy

The key insight: Apple's autocomplete suggestion count **varies per keyword** (0-10). More suggestions = more people searching for that term. Most paid ASO tools charge $50-200/month for this same signal. This tool pairs that traffic estimate with a difficulty score based on competitor strength, then finds the **opportunity gap** — high-traffic keywords that nobody is competing for yet.

## Setup

```bash
/opt/homebrew/bin/python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Usage

```bash
# Single keyword
python aso.py "virtual pet" --detailed

# Compare multiple keywords
python aso.py "virtual pet" "pet game" "ai companion" "tamagotchi"

# From a file (one keyword per line, # comments supported)
python aso.py -f keywords.txt

# JSON output
python aso.py "virtual pet" --json

# Different country
python aso.py "virtual pet" -c gb
```

## How It Works

### Data Sources

- **iTunes Search API** (free, no auth) — returns app metadata, ratings, descriptions for up to 200 results per keyword
- **Apple MZSearchHints API** — returns autocomplete suggestions (0-10 per keyword) used as a traffic proxy

### Difficulty Score (0-100, lower = easier)

| Factor | Weight | Signal |
|--------|--------|--------|
| Title Match | 4 | Do top 10 apps have the keyword in their name? |
| Competitor Strength | 5 | Average rating count of top 10 (proxy for installs) |
| Saturation | 3 | % of top 25 results with keyword in title |
| Freshness | 1 | How recently top apps were updated |

### Traffic Score (0-100, higher = more demand)

| Factor | Weight | Signal |
|--------|--------|--------|
| Suggestion Count | 6 | How many autocomplete suggestions Apple returns (0-10) |
| Keyword Match | 2 | Does the exact keyword appear in suggestions? |
| Result Count | 1 | Total apps returned from search |
| Rating Spread | 1 | Do mid-tier apps (rank 10-25) also have ratings? |

### Opportunity Score

```
opportunity = traffic / difficulty
```

Higher = better keyword to target.

## Limitations

- **Traffic is estimated**, not measured. The suggestion count proxy can't differentiate between moderate and massive search volume — anything with 10/10 suggestions looks the same.
- **Prefix pollution**: A keyword like "lumie" may score high because Apple returns suggestions for "lumiere". The tool counts all suggestions, not just exact matches.
- **Rate limited** to ~20 API calls/minute. Each keyword requires 2 calls. 100 keywords takes ~12 minutes.
- **No historical data**. Results are a snapshot, not a trend.

For actual search volume numbers, use [Apple Search Ads](https://searchads.apple.com) (free account) or [Astro](https://tryastro.app) ($9/month).
