# Govvy Legislation Scraper

Scraper for Cleveland Heights, OH city council records. Pulls ordinances and resolutions from the city website and agendas, minutes and votes from CivicClerk, then links everything together by legislation number. Output is set up to be used as context for an LLM.

## Install

```bash
git clone <repo-url>
cd govvy-legislation-scraper
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Usage

```bash
python civic_scraper.py --year 2026
```

Other options:

```
--out DIR           output folder (default: data)
--attachments MODE  legislation (default), all, none
--packets           also download agenda packets
--max-events N      only scrape the first N meetings
--skip-city         skip the city website
--skip-civicclerk   skip CivicClerk
--refresh           re-download everything
```

First run for a full year downloads around 850 PDFs, so it takes a while. Files are cached after that.

## Output

```
data/
├── legislation.json
├── documents.jsonl
├── events.jsonl
├── manifest.json
├── context/
├── files/
└── text/
```

- `legislation.json` - every ordinance/resolution with its title, status, agenda history, votes, and what it cites
- `documents.jsonl` - every PDF with tags (`leg:147-2026`, `event:583`, `kind:minutes`, etc.)
- `events.jsonl` - meetings and agenda items
- `context/` - one markdown file per ordinance/resolution with everything above plus the full text
- `files/`, `text/` - PDFs and extracted text, named by sha256

## Notes

- Ordinances and resolutions share the same numbering each year, so the number (e.g. `147-2026`) works as a unique ID.
- PDFs linked through docaccess.com are the same files as the originals. The `v` parameter in their URLs is the sha256 of the file.
- CivicClerk has a public API at `https://clevelandheightsoh.api.civicclerk.com/v1`, so no browser automation is needed.
- The city's listing page URLs change every year. Add new ones to `LISTING_PAGES` in `civic_scraper.py`.
- Vote `passFail` from CivicClerk isn't reliable, use the vote tallies instead.
