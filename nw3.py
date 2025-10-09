# server.py
from flask import Flask, request, jsonify, send_from_directory, abort
import os
import requests
from xml.etree import ElementTree as ET
import time
from urllib.parse import quote_plus

app = Flask(__name__, static_folder='static', static_url_path='')

ENTREZ_BASE = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
API_KEY = os.environ.get("NCBI_API_KEY")
USER_EMAIL = os.environ.get("USER_EMAIL", "you@example.com")
SESSION = requests.Session()
SESSION.headers.update({"User-Agent": f"NCBIEntrezGraph/2.0 ({USER_EMAIL})"})

# small delay between requests to be polite to NCBI
DELAY = 0.35  # ~3 requests/sec

def _get(endpoint, params):
    params = params.copy() if params else {}
    if API_KEY:
        params["api_key"] = API_KEY
    r = SESSION.get(f"{ENTREZ_BASE}/{endpoint}.fcgi", params=params, timeout=30)
    r.raise_for_status()
    time.sleep(DELAY)
    return r

def search_term_to_pmids(term, retmax=20):
    if not term:
        return []
    try:
        r = _get("esearch", {"db":"pubmed","term":term,"retmode":"json","retmax":retmax})
        j = r.json()
        ids = j.get("esearchresult", {}).get("idlist", [])
        return [str(x) for x in ids]
    except Exception:
        return []

def get_article_summary_by_pmid(pmid):
    # safe fetch of summary + efetch for abstract + MeSH
    title = ""
    journal = ""
    pubdate = ""
    abstract = ""
    mesh_data = []

    try:
        r = _get("esummary", {"db":"pubmed","id":pmid,"retmode":"json"})
        j = r.json()
        res = j.get("result", {}).get(str(pmid), {})
        title = (res.get("title") or "").strip()
        journal = (res.get("fulljournalname") or "").strip()
        pubdate = (res.get("pubdate") or "").strip()
    except Exception:
        pass

    try:
        r2 = _get("efetch", {"db":"pubmed","id":pmid,"retmode":"xml"})
        root = ET.fromstring(r2.text)
        if not title:
            art_title_el = root.find(".//ArticleTitle") or root.find(".//Title")
            if art_title_el is not None:
                t = "".join(art_title_el.itertext()).strip()
                if t:
                    title = t
        abs_parts = [a.text.strip() for a in root.findall(".//AbstractText") if a.text]
        if abs_parts:
            abstract = "\n\n".join(abs_parts)
        else:
            abs_el = root.find(".//Abstract")
            if abs_el is not None:
                abstract = "".join(abs_el.itertext()).strip()
        # Mesh
        for mh in root.findall(".//MeshHeading"):
            descriptor = mh.find("DescriptorName")
            if descriptor is None: continue
            desc_name = descriptor.text.strip()
            desc_id = descriptor.attrib.get("UI")
            desc_major = descriptor.attrib.get("MajorTopicYN") == "Y"
            qualifiers = []
            for q in mh.findall("QualifierName"):
                qualifiers.append({
                    "name": q.text.strip(),
                    "id": q.attrib.get("UI"),
                    "major": q.attrib.get("MajorTopicYN") == "Y"
                })
            mesh_data.append({
                "descriptor": desc_name,
                "descriptor_id": desc_id,
                "major": desc_major,
                "qualifiers": qualifiers
            })
    except Exception:
        pass

    if not title:
        title = f"(Untitled article, PMID {pmid})"
    else:
        if len(title) > 800:
            title = title[:200].rstrip() + "..."
    if not abstract:
        abstract = "(No abstract available.)"

    return {
        "pmid": str(pmid),
        "title": title,
        "abstract": abstract,
        "journal": journal,
        "pubdate": pubdate,
        "mesh": mesh_data
    }

def get_citations_of(pmid, direction="refs", limit=200):
    if not pmid:
        return []
    linkname = "pubmed_pubmed_refs" if direction == "refs" else "pubmed_pubmed_citedin"
    try:
        r = _get("elink", {"dbfrom":"pubmed","db":"pubmed","id":pmid,"linkname":linkname,"retmode":"json"})
        j = r.json()
        out = []
        for ls in j.get("linksets", []):
            for ln in ls.get("linksetdb", []):
                out.extend([str(i) for i in ln.get("links", [])])
        # unique preserve order
        seen = []
        for i in out:
            if i not in seen:
                seen.append(i)
            if len(seen) >= limit:
                break
        return seen
    except Exception:
        return []

@app.route('/api/graph')
def api_graph():
    """
    Query parameters:
      seeds (comma-separated terms or PMIDs)
      limit (number of search results per seed, default 20)
      connector_limit (how many refs to fetch per PMID)
    """
    seeds_raw = request.args.get('seeds', '')
    seeds = [s.strip() for s in seeds_raw.split(',') if s.strip()]
    if not seeds:
        return jsonify({"error":"no seeds provided"}), 400
    try:
        limit = int(request.args.get('limit', 20))
    except:
        limit = 20
    try:
        connector_limit = int(request.args.get('connector_limit', 20))
    except:
        connector_limit = 20

    all_nodes = {}
    all_links = set()

    def edge_key(a,b):
        return f"{a}->{b}"

    for s_index, seed in enumerate(seeds):
        # resolve seed to pmids
        if seed.isdigit():
            pmids = [seed]
        else:
            pmids = search_term_to_pmids(seed, retmax=limit)
        if not pmids:
            continue

        local_pmids = set(pmids)
        # gather connectors (refs)
        for p in pmids:
            refs = get_citations_of(p, direction="refs", limit=connector_limit)
            for r in refs:
                local_pmids.add(r)

        # fetch metadata for local PMIDs
        for pmid in list(local_pmids):
            if pmid in all_nodes:
                # mark shared
                all_nodes[pmid]['shared'] = True
                if seed not in all_nodes[pmid]['seedGroups']:
                    all_nodes[pmid]['seedGroups'].append(seed)
                all_nodes[pmid]['color'] = "#ffffff"
            else:
                meta = get_article_summary_by_pmid(pmid)
                mesh_list = [m['descriptor'] for m in meta.get('mesh', [])] if meta.get('mesh') else []
                node = {
                    "id": pmid,
                    "name": meta['title'],
                    "title_full": meta['title'],
                    "abstract": meta['abstract'],
                    "journal": meta['journal'],
                    "pubdate": meta['pubdate'],
                    "mesh": mesh_list,
                    "mesh_detail": meta.get('mesh', []),
                    "val": 12,
                    "color": ["#00bcd4","#ff9800","#8bc34a","#e91e63","#9c27b0","#ff5722","#03a9f4","#cddc39"][s_index % 8],
                    "seedGroup": seed,
                    "seedGroups": [seed],
                    "shared": False
                }
                all_nodes[pmid] = node

        # add citation edges (directed)
        for a in pmids:
            refs = get_citations_of(a, direction="refs", limit=connector_limit)
            for b in refs:
                all_links.add(edge_key(a,b))

        # intra-seed linear edges to keep cluster together (optional)
        local_list = list(local_pmids)
        for i in range(len(local_list)-1):
            a = local_list[i]; b = local_list[i+1]
            all_links.add(edge_key(a,b))

    # compute semantic color if shared MeSH
    nodes = list(all_nodes.values())
    node_map = {n['id']: n for n in nodes}
    links = []
    for edge in all_links:
        try:
            a,b = edge.split('->')
        except:
            continue
        src = node_map.get(a)
        tgt = node_map.get(b)
        if not src or not tgt:
            # skip edges to nodes we don't have metadata for
            continue
        color = "#cccccc"
        semantic = False
        if src.get('mesh') and tgt.get('mesh'):
            shared = set(src['mesh']).intersection(set(tgt['mesh']))
            if shared:
                color = "#66bb6a"
                semantic = True
        links.append({
            "source": a,
            "target": b,
            "color": color,
            "semantic": semantic
        })

    return jsonify({"nodes": nodes, "links": links})

# serve frontend
@app.route('/')
def index():
    return send_from_directory('static', 'interactive_graph_pubmed.html')

@app.route('/<path:p>')
def static_proxy(p):
    # serve static files under static/
    return send_from_directory('static', p)

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    app.run(host="0.0.0.0", port=port, debug=True)
