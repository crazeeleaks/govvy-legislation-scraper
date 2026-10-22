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
--scan-ids A B      check DocumentCenter ids A through B for unlinked pdfs
--summarize         extract structured json for each bill with gemini
--model NAME        gemini model (default: gemini-2.5-flash)
```

`--summarize` needs a Gemini API key:

```bash
export GEMINI_API_KEY=your-key
python civic_scraper.py --year 2026 --summarize
```

First run for a full year downloads around 850 PDFs, so it takes a while. Files are cached after that.

## Output

```
data/
├── legislation.json
├── documents.jsonl
├── events.jsonl
├── manifest.json
├── unlinked_matches.csv
├── summaries/
├── context/
├── files/
└── text/
```

- `legislation.json` - every ordinance/resolution with its title, status, agenda history, votes, and what it cites
- `documents.jsonl` - every PDF with tags (`leg:147-2026`, `event:583`, `kind:minutes`, etc.)
- `events.jsonl` - meetings and agenda items
- `context/` - one markdown file per ordinance/resolution with everything above plus the full text
- `unlinked_matches.csv` - pdfs found with `--scan-ids` and the legislation they were matched to (regex on the number, or rapidfuzz on the title)
- `summaries/` - gemini output per bill. Entities, dates and dollar amounts that don't appear in the bill text get dropped
- `files/`, `text/` - PDFs and extracted text, named by sha256

## Notes

- Ordinances and resolutions share the same numbering each year, so the number (e.g. `147-2026`) works as a unique ID.
- PDFs linked through docaccess.com are the same files as the originals. The `v` parameter in their URLs is the sha256 of the file.
- CivicClerk has a public API at `https://clevelandheightsoh.api.civicclerk.com/v1`, so no browser automation is needed.
- The city's listing page URLs change every year. Add new ones to `LISTING_PAGES` in `civic_scraper.py`.
- Unlinked pdfs are only kept if the server says they're `application/pdf`.
- Vote `passFail` from CivicClerk isn't reliable, use the vote tallies instead.
