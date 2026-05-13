"""
Scrape Cleveland Heights council legislation, agendas and minutes and link
them by legislation number.

    python civic_scraper.py --year 2026 --out data
"""

import argparse
import hashlib
import json
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
from pypdf import PdfReader

CITY_URL = "https://www.clevelandheights.gov"
API_URL = "https://clevelandheightsoh.api.civicclerk.com/v1"
PORTAL_URL = "https://clevelandheightsoh.portal.civicclerk.com"

# page ids are different every year
LISTING_PAGES = {
    2026: [
        ("ordinance", CITY_URL + "/1852/2026-Ordinances"),
        ("resolution", CITY_URL + "/1851/2026-Resolutions"),
    ],
}

DELAY = 0.4

# matches things like "ORDINANCE NO. 147-2026(F)", "RES 144-2026(AS)", "Ord. 241-2025"
LEG_RE = re.compile(
    r"\b(ORD(?:INANCE)?S?|RES(?:OLUTION)?S?)\.?\s*(?:NOS?\.?\s*)?"
    r"(\d{1,3})\s*-\s*((?:19|20)\d{2})(?!\d)(?:\s*\(([A-Z]{1,6})\))?",
    re.I,
)
NUM_RE = re.compile(r"(\d{1,3})\s*-\s*((?:19|20)\d{2})(?!\d)")
READING_RE = re.compile(r"\b(First|Second|Third|Fourth|Fifth|Sixth)\s+Reading\b", re.I)


def make_key(num, year):
    return "%03d-%s" % (int(num), year)


def clean(s):
    return re.sub(r"\s+", " ", s or "").strip()


def strip_html(html):
    return clean(BeautifulSoup(html or "", "html.parser").get_text(" "))


def get_reading(s):
    m = READING_RE.search(s or "")
    return m.group(0).title() if m else None


def find_refs(text):
    refs = {}
    for m in LEG_RE.finditer(text or ""):
        key = make_key(m.group(2), m.group(3))
        if key not in refs:
            kind = "ordinance" if m.group(1).upper().startswith("ORD") else "resolution"
            refs[key] = {"type": kind, "committee": None}
        if not refs[key]["committee"]:
            refs[key]["committee"] = m.group(4)
    return refs


def get_snippets(text, key, limit=3):
    num, year = key.split("-")
    pat = re.compile(r"(?<!\d)0*%d\s*-\s*%s(?!\d)" % (int(num), year))
    out = []
    for m in pat.finditer(text):
        out.append(clean(text[max(0, m.start() - 80) : m.end() + 320]))
        if len(out) == limit:
            break
    return out


def pdf_text(path):
    try:
        reader = PdfReader(path)
        pages = [p.extract_text() or "" for p in reader.pages]
    except Exception as e:
        print("  couldn't read %s: %s" % (path.name, e), file=sys.stderr)
        return ""
    return "\n\n".join("[page %d]\n%s" % (i + 1, t) for i, t in enumerate(pages))


def to_json(obj):
    if isinstance(obj, set):
        return sorted(obj)
    raise TypeError(type(obj))


class Scraper:
    def __init__(self, out_dir, refresh=False):
        self.out = Path(out_dir)
        self.files_dir = self.out / "files"
        self.text_dir = self.out / "text"
        self.files_dir.mkdir(parents=True, exist_ok=True)
        self.text_dir.mkdir(parents=True, exist_ok=True)

        self.cache_file = self.out / "download_cache.json"
        self.cache = {}
        if self.cache_file.exists() and not refresh:
            self.cache = json.loads(self.cache_file.read_text())

        self.session = requests.Session()
        self.session.headers["User-Agent"] = "Mozilla/5.0"

        self.documents = {}
        self.legislation = {}
        self.events = []

    def get(self, url, params=None):
        for i in range(3):
            try:
                time.sleep(DELAY)
                r = self.session.get(url, params=params, timeout=60)
                r.raise_for_status()
                return r
            except requests.RequestException as e:
                if i == 2:
                    raise
                print("  retrying %s (%s)" % (url, e), file=sys.stderr)
                time.sleep(2 * (i + 1))

    def read_text(self, doc_id):
        return (self.text_dir / (doc_id + ".txt")).read_text(encoding="utf-8")

    def download(self, cache_key, file_url, **info):
        """Download a pdf (if we don't have it yet) and add it to self.documents."""
        sha = self.cache.get(cache_key)
        if not sha or not (self.files_dir / (sha + ".pdf")).exists():
            try:
                r = self.get(file_url)
            except requests.RequestException as e:
                print("  failed %s: %s" % (cache_key, e), file=sys.stderr)
                return None
            if not r.content.startswith(b"%PDF"):
                print("  not a pdf: %s" % cache_key, file=sys.stderr)
                return None
            sha = hashlib.sha256(r.content).hexdigest()
            (self.files_dir / (sha + ".pdf")).write_bytes(r.content)
            self.cache[cache_key] = sha
            self.cache_file.write_text(json.dumps(self.cache, indent=1))

        txt = self.text_dir / (sha + ".txt")
        if not txt.exists():
            txt.write_text(pdf_text(self.files_dir / (sha + ".pdf")), encoding="utf-8")
        text = self.read_text(sha)

        if sha not in self.documents:
            # sha256 of the file is the same as the ?v= param on docaccess.com links
            self.documents[sha] = {
                "doc_id": sha,
                "docaccess_v": sha,
                "chars": len(text),
                "sources": [],
                "legislation_refs": sorted(find_refs(text)),
                "tags": set(),
            }
        doc = self.documents[sha]
        doc["sources"].append(dict(cache_key=cache_key, **info))
        doc["tags"].add("kind:" + info["kind"])
        return doc

    def get_leg(self, key):
        if key not in self.legislation:
            self.legislation[key] = {
                "key": key,
                "type": None,
                "committee": None,
                "title": None,
                "city_listing": None,
                "documents": set(),
                "agenda_appearances": [],
                "mentioned_in": [],
                "cites": set(),
                "cited_by": set(),
            }
        return self.legislation[key]

    def scrape_city(self, year):
        for kind, page in LISTING_PAGES.get(year, []):
            print("city:", page)
            soup = BeautifulSoup(self.get(page).text, "html.parser")
            count = 0
            for row in soup.select("table tr"):
                cells = row.find_all("td")
                link = row.select_one('a[href*="/DocumentCenter/View/"]')
                if not link or not cells:
                    continue

                # the number is sometimes split up over two <strong> tags
                m = NUM_RE.search(link.get_text("", strip=True))
                if not m:
                    print("  skipping row:", cells[0].get_text(" ", strip=True), file=sys.stderr)
                    continue
                key = make_key(*m.groups())
                count += 1

                lines = cells[0].get_text("\n", strip=True).split("\n")[1:]
                status = clean(" ".join(l for l in lines if not NUM_RE.search(l)))
                url = urljoin(CITY_URL, link["href"])

                leg = self.get_leg(key)
                leg["type"] = kind
                if len(cells) > 1:
                    leg["title"] = clean(cells[1].get_text(" "))
                leg["city_listing"] = {
                    "page": page,
                    "document_url": url,
                    "documentcenter_id": int(re.search(r"/View/(\d+)", url).group(1)),
                    "status": status,
                    "latest_reading": get_reading(status),
                    "as_amended": "amended" in status.lower(),
                }

                doc = self.download(
                    "city:" + url,
                    url,
                    kind="legislation_text",
                    source="city_website",
                    title="%s %s" % (kind.title(), key),
                    url=url,
                    legislation=key,
                )
                if doc:
                    leg["documents"].add(doc["doc_id"])
                    doc["tags"].add("leg:" + key)
            print("  %d rows" % count)

    def get_events(self, year):
        params = {
            "$filter": "startDateTime ge %d-01-01T00:00:00Z and startDateTime lt %d-01-01T00:00:00Z"
            % (year, year + 1),
            "$orderby": "startDateTime asc",
        }
        url = API_URL + "/Events"
        events = []
        # api returns 15 at a time
        while url:
            data = self.get(url, params).json()
            events += data["value"]
            url = data.get("@odata.nextLink")
            params = None
        return events

    def scrape_civicclerk(self, year, max_events=None, attachments="legislation", packets=False):
        events = [e for e in self.get_events(year) if e.get("publishedFiles") or e.get("agendaId")]
        if max_events:
            events = events[:max_events]
        print("civicclerk: %d events" % len(events))

        file_kinds = {"Agenda": "agenda", "Minutes": "minutes", "Agenda Packet": "agenda_packet"}

        for e in events:
            # times are local even though they end in Z
            date = e["startDateTime"][:10]
            print("  %s %s %s" % (e["id"], date, e["eventName"]))

            event = {
                "event_id": e["id"],
                "name": e["eventName"],
                "category": e.get("categoryName"),
                "date": date,
                "start": e["startDateTime"],
                "agenda_id": e.get("agendaId") or None,
                "portal_url": "%s/event/%s" % (PORTAL_URL, e["id"]),
                "files": [],
                "items": [],
            }
            tags = {"event:%s" % e["id"], "date:" + date, "body:%s" % e.get("categoryName")}

            for f in e.get("publishedFiles") or []:
                kind = file_kinds.get(f["type"], f["type"].lower())
                if kind == "agenda_packet" and not packets:
                    continue
                file_id = f["fileId"]
                doc = self.download(
                    "cc-file:%s" % file_id,
                    "%s/Meetings/GetMeetingFileStream(fileId=%s,plainText=false)" % (API_URL, file_id),
                    kind=kind,
                    source="civicclerk",
                    title=f["name"],
                    url="%s/event/%s/files/%s/%s"
                    % (PORTAL_URL, e["id"], f["type"].split()[0].lower(), file_id),
                    civicclerk_file_id=file_id,
                    event_id=e["id"],
                    date=date,
                )
                if doc:
                    doc["tags"].update(tags)
                    event["files"].append({"type": f["type"], "file_id": file_id, "doc_id": doc["doc_id"]})

            if event["agenda_id"]:
                meeting = self.get("%s/Meetings/%s" % (API_URL, event["agenda_id"])).json()
                self.parse_items(meeting.get("items") or [], event, tags, attachments)

            self.events.append(event)

    def parse_items(self, items, event, tags, attachments, section=None):
        for it in items:
            name = strip_html(it.get("agendaObjectItemName"))
            refs = find_refs(name)

            votes = []
            for v in it.get("minutesItemVotes") or []:
                yes, no, abstain = (
                    v.get("yesVotes") or [],
                    v.get("noVotes") or [],
                    v.get("abstainVotes") or [],
                )
                votes.append(
                    {
                        "motionName": v.get("motionName"),
                        "initiatedBy": v.get("initiatedBy"),
                        "secondedBy": v.get("secondedBy"),
                        "yesVotes": yes,
                        "noVotes": no,
                        "abstainVotes": abstain,
                        # passFail is 0 on a lot of unanimous votes so don't rely on it
                        "pass_fail_code": v.get("passFail"),
                        "tally": "%d-%d-%d" % (len(yes), len(no), len(abstain)),
                    }
                )

            item = {
                "item_id": it["id"],
                "outline": clean(
                    it.get("agendaObjectItemOutlineNumberFull") or it.get("agendaObjectItemOutlineNumber")
                ),
                "item_number": it.get("agendaObjectItemNumber") or None,
                "section": section,
                "name": name,
                "legislation": sorted(refs),
                "reading": get_reading(name),
                "votes": votes,
                "minutes_note": strip_html(it.get("agendaObjectItemMinutesHtmlContent")) or None,
                "attachments": [],
            }

            get_files = attachments == "all" or (attachments == "legislation" and refs)
            for att in it.get("attachmentsList") or []:
                entry = {"attachment_id": att["id"], "file_name": att.get("fileName"), "doc_id": None}
                is_pdf = att.get("pdfVersionFileName") or att.get("contentType") == "application/pdf"
                if get_files and is_pdf:
                    # this endpoint gives back a pdf even for word docs
                    doc = self.download(
                        "cc-att:%s" % att["id"],
                        "%s/Meetings/GetAttachmentFile(fileId=%s)" % (API_URL, att["id"]),
                        kind="attachment",
                        source="civicclerk",
                        title=att.get("fileName"),
                        url=event["portal_url"],
                        civicclerk_attachment_id=att["id"],
                        event_id=event["event_id"],
                        date=event["date"],
                        agenda_item_id=it["id"],
                    )
                    if doc:
                        entry["doc_id"] = doc["doc_id"]
                        doc["tags"].update(tags)
                        for k in list(refs) + list(find_refs(att.get("fileName"))):
                            doc["tags"].add("leg:" + k)
                item["attachments"].append(entry)

            for key, ref in refs.items():
                leg = self.get_leg(key)
                leg["type"] = leg["type"] or ref["type"]
                leg["committee"] = leg["committee"] or ref["committee"]
                if not leg["title"]:
                    title = LEG_RE.split(name, maxsplit=1)[-1].split("Reading.", 1)[-1]
                    leg["title"] = clean(title) or None
                leg["agenda_appearances"].append(
                    {
                        "event_id": event["event_id"],
                        "date": event["date"],
                        "meeting": event["name"],
                        "portal_url": event["portal_url"],
                        "outline": item["outline"] or item["item_number"],
                        "section": section,
                        "reading": item["reading"],
                        "votes": votes,
                        "attachments": item["attachments"],
                    }
                )
                for a in item["attachments"]:
                    if a["doc_id"]:
                        leg["documents"].add(a["doc_id"])

            event["items"].append(item)

            children = it.get("childItems") or []
            if children:
                sub = name if (it.get("isSection") or it.get("hasChildItems")) else section
                self.parse_items(children, event, tags, attachments, sub)

    def link(self):
        # tag each document with the legislation it mentions, and record cites/cited_by
        for doc in self.documents.values():
            text = self.read_text(doc["doc_id"])
            refs = find_refs(text)
            own = set(t[4:] for t in doc["tags"] if t.startswith("leg:"))
            kinds = sorted(set(s["kind"] for s in doc["sources"]))
            first = doc["sources"][0]
            is_leg_text = "legislation_text" in kinds or (first.get("title") or "").upper().startswith(
                ("ORD", "RES")
            )

            for key in doc["legislation_refs"]:
                if key in own:
                    continue
                doc["tags"].add("mentions:" + key)
                leg = self.get_leg(key)
                leg["type"] = leg["type"] or refs[key]["type"]
                leg["mentioned_in"].append(
                    {
                        "doc_id": doc["doc_id"],
                        "kind": kinds,
                        "title": first.get("title"),
                        "date": first.get("date"),
                        "event_id": first.get("event_id"),
                        "snippets": get_snippets(text, key),
                    }
                )
                if is_leg_text:
                    for k in own:
                        self.get_leg(k)["cites"].add(key)
                        leg["cited_by"].add(k)

    def find_text_doc(self, leg):
        # prefer the version on the city site, otherwise the agenda attachment
        docs = [self.documents[d] for d in leg["documents"] if d in self.documents]
        for doc in docs:
            if any(s["kind"] == "legislation_text" for s in doc["sources"]):
                return doc
        for doc in docs:
            if any((s.get("title") or "").upper().startswith(("ORD", "RES")) for s in doc["sources"]):
                return doc
        return None

    def make_context(self, leg, max_chars=15000):
        out = ["# %s No. %s" % ((leg["type"] or "legislation").title(), leg["key"]), ""]
        if leg["title"]:
            out += [leg["title"], ""]

        if leg["committee"]:
            out.append("- Committee code: " + leg["committee"])
        if leg["city_listing"]:
            cl = leg["city_listing"]
            out.append("- City website status: %s (%s)" % (cl["status"] or "n/a", cl["document_url"]))
        if leg["cites"]:
            out.append("- Cites: " + ", ".join(sorted(leg["cites"])))
        if leg["cited_by"]:
            out.append("- Cited by: " + ", ".join(sorted(leg["cited_by"])))
        out.append("")

        if leg["agenda_appearances"]:
            out += ["## Council agenda history", ""]
            for a in sorted(leg["agenda_appearances"], key=lambda a: a["date"]):
                out.append(
                    "- %s %s (item %s, %s): %s [%s]"
                    % (
                        a["date"],
                        a["meeting"],
                        a["outline"],
                        a["section"] or "-",
                        a["reading"] or "-",
                        a["portal_url"],
                    )
                )
                for v in a["votes"]:
                    line = "  - Vote on '%s' (yes-no-abstain %s), moved %s, seconded %s" % (
                        v["motionName"],
                        v["tally"],
                        v["initiatedBy"],
                        v["secondedBy"],
                    )
                    if v["noVotes"]:
                        line += "; no: " + ", ".join(v["noVotes"])
                    if v["abstainVotes"]:
                        line += "; abstain: " + ", ".join(v["abstainVotes"])
                    out.append(line)
                for att in a["attachments"]:
                    out.append("  - Attachment: %s" % att["file_name"])
            out.append("")

        if leg["mentioned_in"]:
            out += ["## Mentioned in", ""]
            for m in leg["mentioned_in"]:
                out.append("- %s %s (%s)" % (m["date"] or "", m["title"], ", ".join(m["kind"])))
                for s in m["snippets"]:
                    out.append("  > " + s)
            out.append("")

        doc = self.find_text_doc(leg)
        if doc:
            text = self.read_text(doc["doc_id"])
            out += ["## Text (%s)" % doc["sources"][0]["title"], ""]
            if len(text) > max_chars:
                text = text[:max_chars] + "\n\n[truncated]"
            out += [text, ""]

        return "\n".join(out)

    def save(self, year):
        with open(self.out / "documents.jsonl", "w", encoding="utf-8") as f:
            for doc in self.documents.values():
                f.write(json.dumps(doc, default=to_json) + "\n")

        with open(self.out / "events.jsonl", "w", encoding="utf-8") as f:
            for e in self.events:
                f.write(json.dumps(e, default=to_json) + "\n")

        # sort by year then number
        legislation = dict(sorted(self.legislation.items(), key=lambda kv: (kv[0][-4:], kv[0])))
        with open(self.out / "legislation.json", "w") as f:
            json.dump(legislation, f, indent=1, default=to_json)

        context_dir = self.out / "context"
        context_dir.mkdir(exist_ok=True)
        for key, leg in legislation.items():
            (context_dir / (key + ".md")).write_text(self.make_context(leg), encoding="utf-8")

        counts = {
            "legislation": len(legislation),
            "with_city_listing": len([l for l in legislation.values() if l["city_listing"]]),
            "with_agenda_appearance": len([l for l in legislation.values() if l["agenda_appearances"]]),
            "events": len(self.events),
            "documents": len(self.documents),
        }
        manifest = {
            "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "year": year,
            "sources": {"city_listings": LISTING_PAGES.get(year, []), "civicclerk_api": API_URL},
            "counts": counts,
        }
        with open(self.out / "manifest.json", "w") as f:
            json.dump(manifest, f, indent=1)
        return counts


def main():
    parser = argparse.ArgumentParser(description="Scrape Cleveland Heights council records")
    parser.add_argument("--year", type=int, default=2026)
    parser.add_argument("--out", default="data")
    parser.add_argument("--max-events", type=int, help="only do the first N meetings")
    parser.add_argument(
        "--attachments",
        choices=["legislation", "all", "none"],
        default="legislation",
        help="which agenda item attachments to download",
    )
    parser.add_argument("--packets", action="store_true", help="download full agenda packets too")
    parser.add_argument("--skip-city", action="store_true")
    parser.add_argument("--skip-civicclerk", action="store_true")
    parser.add_argument("--refresh", action="store_true", help="ignore download cache")
    args = parser.parse_args()

    scraper = Scraper(args.out, refresh=args.refresh)
    if not args.skip_city:
        scraper.scrape_city(args.year)
    if not args.skip_civicclerk:
        scraper.scrape_civicclerk(args.year, args.max_events, args.attachments, args.packets)
    scraper.link()
    counts = scraper.save(args.year)
    print(json.dumps(counts, indent=1))


if __name__ == "__main__":
    main()
