#!/usr/bin/env python3
"""
n8n nodes metadata extraction script.

Pulls the latest n8n release tarball from GitHub, walks packages/nodes-base/nodes/
and packages/@n8n/nodes-langchain/nodes/, parses each node's TypeScript descriptor
and JSON codex, and emits:
  - nodes.json      canonical record-per-node
  - nodes.parquet   columnar, HuggingFace-friendly

Run:
    python extract.py [--tag n8n@2.20.6]

Idempotent: re-running overwrites the output files from the same tag. Pass a
different --tag to switch versions.
"""

import argparse
import gzip
import hashlib
import json
import os
import re
import sys
import tarfile
import tempfile
from pathlib import Path

import requests

try:
    import pyarrow as pa
    import pyarrow.parquet as pq
except ImportError:
    sys.exit("pyarrow is required: pip install pyarrow")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
GITHUB_API = "https://api.github.com"
GITHUB_REPO = "n8n-io/n8n"
GITHUB_RAW = f"https://raw.githubusercontent.com/{GITHUB_REPO}"

NODE_PACKAGES = [
    "packages/nodes-base/nodes",
    "packages/@n8n/nodes-langchain/nodes",
]

OUT_DIR = Path(__file__).parent
CACHE_DIR = OUT_DIR / ".cache"


# ---------------------------------------------------------------------------
# GitHub helpers
# ---------------------------------------------------------------------------

def get_latest_tag() -> str:
    """Return the latest release tag (e.g. 'n8n@2.20.6')."""
    url = f"{GITHUB_API}/repos/{GITHUB_REPO}/releases/latest"
    r = requests.get(url, timeout=30)
    r.raise_for_status()
    return r.json()["tag_name"]


def download_tarball(tag: str) -> Path:
    """
    Download the source tarball for the given tag to CACHE_DIR.
    Returns the local path. Skips download if already cached.
    """
    CACHE_DIR.mkdir(exist_ok=True)
    safe_tag = tag.replace("@", "_").replace("/", "_")
    local_path = CACHE_DIR / f"n8n-{safe_tag}.tar.gz"
    if local_path.exists():
        print(f"  cache hit: {local_path.name}")
        return local_path

    url = f"https://api.github.com/repos/{GITHUB_REPO}/tarball/{tag}"
    print(f"  downloading tarball for {tag} ...")
    r = requests.get(url, stream=True, timeout=120)
    r.raise_for_status()
    with open(local_path, "wb") as f:
        for chunk in r.iter_content(chunk_size=65536):
            f.write(chunk)
    print(f"  saved: {local_path.name} ({local_path.stat().st_size // 1024} KB)")
    return local_path


def extract_tarball(tarball: Path, extract_to: Path) -> str:
    """
    Extract the tarball into extract_to. Returns the name of the root directory
    inside the archive (e.g. 'n8n-io-n8n-29d4256').
    """
    with tarfile.open(tarball, "r:gz") as tf:
        root_dirs = {m.name.split("/")[0] for m in tf.getmembers() if "/" in m.name}
        root = sorted(root_dirs)[0]
        tf.extractall(extract_to, filter="data")
    return root


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------

def _clean_ts(src: str) -> str:
    """Strip single-line and block comments from a TypeScript source string."""
    # Remove block comments (/* ... */)
    src = re.sub(r"/\*.*?\*/", " ", src, flags=re.DOTALL)
    # Remove line comments (// ...)
    src = re.sub(r"//[^\n]*", "", src)
    return src


def _extract_description_block(ts_src: str) -> str | None:
    """
    Extract the object literal assigned to `description` in a node class.

    Handles both:
      description: INodeTypeDescription = { ... };         (class property)
      const baseDescription: INodeTypeBaseDescription = { ...};  (constructor)
    Returns the raw text of the object literal (without surrounding braces),
    or None if not found.
    """
    cleaned = _clean_ts(ts_src)

    # Try class-level `description: ... = {`
    patterns = [
        r"\bdescription\s*:\s*INodeTypeDescription\s*=\s*\{",
        r"\bbaseDescription\s*:\s*INodeTypeBaseDescription\s*=\s*\{",
        r"\bconst\s+baseDescription\s*[=:][^{]*\{",
        r"\bversionDescription\s*:\s*INodeTypeDescription\s*=\s*\{",
        r"\bconst\s+\w+Description\s*:\s*INodeTypeDescription\s*=\s*\{",
    ]

    for pat in patterns:
        m = re.search(pat, cleaned)
        if not m:
            continue
        start = m.end() - 1  # points to the opening `{`
        depth = 0
        for i, ch in enumerate(cleaned[start:]):
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    return cleaned[start : start + i + 1]
    return None


def _str_val(block: str, key: str) -> str | None:
    """Extract a simple string value for a key from a TS object literal snippet."""
    # Match: key: 'value' or key: "value" (possibly with template literals - skip those)
    pat = rf"\b{re.escape(key)}\s*:\s*['\"]([^'\"]*)['\"]"
    m = re.search(pat, block)
    return m.group(1) if m else None


def _arr_str_vals(block: str, key: str) -> list[str]:
    """Extract an array of string values for a key."""
    pat = rf"\b{re.escape(key)}\s*:\s*\[([^\]]*)\]"
    m = re.search(pat, block, re.DOTALL)
    if not m:
        return []
    inner = m.group(1)
    return re.findall(r"['\"]([^'\"]+)['\"]", inner)


def _extract_group(block: str) -> list[str]:
    return _arr_str_vals(block, "group")


def _extract_version(block: str) -> str | None:
    """Return version as string (may be number or array)."""
    pat = r"\bversion\s*:\s*(\[[^\]]*\]|\d+(?:\.\d+)?)"
    m = re.search(pat, block)
    if not m:
        return None
    val = m.group(1).strip()
    if val.startswith("["):
        nums = re.findall(r"\d+(?:\.\d+)?", val)
        return nums[-1] if nums else None  # highest version
    return val


def _extract_credentials(block: str) -> list[str]:
    """Return list of credential names from the credentials array."""
    cred_pat = r"\bcredentials\s*:\s*\[([^\]]*(?:\[[^\]]*\][^\]]*)*)\]"
    m = re.search(cred_pat, block, re.DOTALL)
    if not m:
        return []
    cred_block = m.group(1)
    return re.findall(r"\bname\s*:\s*['\"]([^'\"]+)['\"]", cred_block)


def _extract_operations(block: str) -> list[str]:
    """
    Extract operation names from the 'operation' property options array.
    Falls back to resource names if no operation property found.
    """
    # Find the 'operation' options block
    op_pat = r"\bname\s*:\s*['\"]operation['\"].*?options\s*:\s*\[([^\]]*(?:\[[^\]]*\][^\]]*)*)\]"
    m = re.search(op_pat, block, re.DOTALL)
    if m:
        return re.findall(r"\bvalue\s*:\s*['\"]([^'\"]+)['\"]", m.group(1))

    # Fall back to resource options
    res_pat = r"\bname\s*:\s*['\"]resource['\"].*?options\s*:\s*\[([^\]]*(?:\[[^\]]*\][^\]]*)*)\]"
    m = re.search(res_pat, block, re.DOTALL)
    if m:
        return re.findall(r"\bvalue\s*:\s*['\"]([^'\"]+)['\"]", m.group(1))

    return []


def _extract_categories_from_ts(block: str) -> list[str]:
    """Extract categories from inline codex in .node.ts (langchain nodes)."""
    cat_pat = r"\bcategories\s*:\s*\[([^\]]*)\]"
    m = re.search(cat_pat, block, re.DOTALL)
    if not m:
        return []
    return re.findall(r"['\"]([^'\"]+)['\"]", m.group(1))


def _extract_subcategories_from_ts(block: str) -> list[str]:
    """Extract subcategory values from inline codex."""
    sub_pat = r"\bsubcategories\s*:\s*\{([^}]*(?:\{[^}]*\}[^}]*)*)\}"
    m = re.search(sub_pat, block, re.DOTALL)
    if not m:
        return []
    inner = m.group(1)
    # Each key maps to an array of strings
    return re.findall(r"['\"]([^'\"]+)['\"]", inner)


def _extract_properties_schema(block: str) -> dict | None:
    """
    Extract a compact schema of top-level properties:
    [{"name": "...", "type": "...", "displayName": "..."}, ...]
    """
    prop_pat = r"\bproperties\s*:\s*\[(.+)\]"
    m = re.search(prop_pat, block, re.DOTALL)
    if not m:
        return None
    inner = m.group(1)
    # Find individual property objects at depth 1
    result = []
    depth = 0
    obj_start = None
    for i, ch in enumerate(inner):
        if ch == "{":
            if depth == 0:
                obj_start = i
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0 and obj_start is not None:
                obj_text = inner[obj_start : i + 1]
                name = _str_val(obj_text, "name")
                display = _str_val(obj_text, "displayName")
                typ_m = re.search(r"\btype\s*:\s*['\"]([^'\"]+)['\"]", obj_text)
                typ = typ_m.group(1) if typ_m else None
                if name:
                    entry = {"name": name}
                    if display:
                        entry["displayName"] = display
                    if typ:
                        entry["type"] = typ
                    result.append(entry)
                obj_start = None
    return result if result else None


# ---------------------------------------------------------------------------
# Per-node parsing
# ---------------------------------------------------------------------------

def parse_node_ts(ts_path: Path, rel_path: str, source_package: str, tag: str) -> dict | None:
    """
    Parse a .node.ts file and return a metadata dict.
    Returns None if we can't extract meaningful data.
    """
    try:
        src = ts_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None

    block = _extract_description_block(src)
    if block is None:
        return None

    display_name = _str_val(block, "displayName")
    name = _str_val(block, "name")
    description = _str_val(block, "description")
    group = _extract_group(block)
    version = _extract_version(block)
    # For versioned nodes, version comes from `defaultVersion` in the block
    if not version:
        dv_m = re.search(r"\bdefaultVersion\s*:\s*(\d+(?:\.\d+)?)", block)
        if dv_m:
            version = dv_m.group(1)
    credentials = _extract_credentials(block)
    operations = _extract_operations(block)
    categories_ts = _extract_categories_from_ts(block)
    subcategories_ts = _extract_subcategories_from_ts(block)
    properties_schema = _extract_properties_schema(block)

    if not name and not display_name:
        return None

    # Build github_permalink
    # rel_path is relative to the repo root, e.g.
    # "packages/nodes-base/nodes/Zoom/Zoom.node.ts"
    encoded_tag = tag.replace("@", "%40")
    github_permalink = (
        f"https://github.com/{GITHUB_REPO}/blob/{encoded_tag}/{rel_path}"
    )

    return {
        "node_name": name,
        "display_name": display_name,
        "categories": categories_ts,
        "subcategories": subcategories_ts,
        "group": group,
        "version": version,
        "description": description,
        "credentials_required": credentials,
        "operations_supported": operations,
        "properties_schema": json.dumps(properties_schema, separators=(",", ":")) if properties_schema else None,
        "source_package": source_package,
        "source_file_path": rel_path,
        "github_permalink": github_permalink,
    }


def merge_node_json(record: dict, json_path: Path) -> dict:
    """
    Overlay categories from the .node.json codex file (nodes-base).
    The JSON codex is more authoritative for categories than TS regex.
    """
    try:
        data = json.loads(json_path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, json.JSONDecodeError):
        return record

    cats = data.get("categories", [])
    if cats:
        record["categories"] = cats

    # subcategories can be a dict {"AI": ["Agents"]} or a list ["Agents"]
    subs_raw = data.get("subcategories", {})
    subs = []
    if isinstance(subs_raw, dict):
        for v in subs_raw.values():
            if isinstance(v, list):
                subs.extend(v)
    elif isinstance(subs_raw, list):
        subs = [str(s) for s in subs_raw]
    if subs:
        record["subcategories"] = subs

    return record


# ---------------------------------------------------------------------------
# Directory walker
# ---------------------------------------------------------------------------

_VERSIONED_DIR_RE = re.compile(r"^[vV]\d", re.IGNORECASE)
_IMPL_DIR_NAMES = {
    "actions", "methods", "transport", "helpers", "utils", "shared",
    "test", "tests", "__tests__", "credentials",
}


def _is_versioned_or_impl_dir(name: str) -> bool:
    """Return True if a directory name indicates versioned/implementation sub-folder."""
    if name.lower() in _IMPL_DIR_NAMES:
        return True
    if _VERSIONED_DIR_RE.match(name):
        return True
    return False


def _collect_node_ts_files(directory: Path) -> list[Path]:
    """
    Collect *.node.ts files that are the 'primary' descriptor for a node.

    Strategy: for each directory that contains *.node.ts files AND is not a
    versioned/impl sub-directory, collect those files. Recurse into
    sub-directories that are NOT versioned/impl dirs.

    This handles both:
    - Flat layout: nodes/Zoom/Zoom.node.ts
    - Nested layout: nodes/agents/Agent/Agent.node.ts
    """
    results: list[Path] = []
    if not directory.is_dir():
        return results

    for entry in sorted(directory.iterdir()):
        if entry.is_file() and entry.name.endswith(".node.ts"):
            results.append(entry)
        elif entry.is_dir() and not _is_versioned_or_impl_dir(entry.name):
            # Recurse into non-impl subdirs
            results.extend(_collect_node_ts_files(entry))

    return results


def walk_package(nodes_dir: Path, source_package: str, tag: str) -> list[dict]:
    """Walk a nodes directory and return a list of metadata records."""
    records = []
    seen_names: set[str] = set()

    if not nodes_dir.is_dir():
        print(f"  WARNING: {nodes_dir} does not exist, skipping.")
        return records

    # The repo root is always 3 levels above nodes_dir
    # nodes-base:     <root>/packages/nodes-base/nodes
    # nodes-langchain: <root>/packages/@n8n/nodes-langchain/nodes  (4 levels)
    # We compute it properly by searching for "packages" ancestor.
    repo_root = nodes_dir
    for _ in range(6):  # safety bound
        if repo_root.name == "packages":
            repo_root = repo_root.parent
            break
        repo_root = repo_root.parent

    all_ts_files = _collect_node_ts_files(nodes_dir)

    for ts_file in all_ts_files:
        rel_path = ts_file.relative_to(repo_root).as_posix()
        record = parse_node_ts(ts_file, rel_path, source_package, tag)
        if record is None:
            continue

        # Try matching .node.json in same directory
        json_file = ts_file.with_suffix("").with_suffix(".node.json")
        if json_file.exists():
            record = merge_node_json(record, json_file)

        # Dedup by node_name
        node_name = record.get("node_name") or record.get("display_name", "")
        if not node_name or node_name in seen_names:
            continue
        seen_names.add(node_name)

        records.append(record)

    return records


# ---------------------------------------------------------------------------
# Output writers
# ---------------------------------------------------------------------------

SCHEMA_FIELDS = [
    "node_name",
    "display_name",
    "categories",
    "subcategories",
    "group",
    "version",
    "description",
    "credentials_required",
    "operations_supported",
    "properties_schema",
    "source_package",
    "source_file_path",
    "github_permalink",
]

def normalize_record(rec: dict) -> dict:
    """Ensure all fields exist and have the right types."""
    out = {}
    for f in SCHEMA_FIELDS:
        val = rec.get(f)
        if f in ("categories", "subcategories", "group", "credentials_required", "operations_supported"):
            out[f] = val if isinstance(val, list) else []
        else:
            out[f] = val if val is not None else ""
    return out


def write_json(records: list[dict], path: Path):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(records, f, ensure_ascii=False, indent=2)
    print(f"  wrote {len(records)} records -> {path.name}")


def write_parquet(records: list[dict], path: Path):
    """Write records to Parquet. Lists are serialized as JSON strings for HF compatibility."""
    if not records:
        print("  no records to write to parquet")
        return

    # Build column-oriented data
    cols: dict[str, list] = {f: [] for f in SCHEMA_FIELDS}
    for rec in records:
        for f in SCHEMA_FIELDS:
            val = rec.get(f)
            if f in ("categories", "subcategories", "group", "credentials_required", "operations_supported"):
                # Store lists as JSON strings — universally readable in HF dataset viewer
                cols[f].append(json.dumps(val if isinstance(val, list) else []))
            else:
                cols[f].append(val if val is not None else "")

    arrays = {f: pa.array(cols[f], type=pa.string()) for f in SCHEMA_FIELDS}
    table = pa.table(arrays)
    pq.write_table(table, path, compression="snappy")
    print(f"  wrote {len(records)} records -> {path.name}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Extract n8n nodes metadata to nodes.json + nodes.parquet")
    parser.add_argument("--tag", default=None, help="n8n release tag (default: latest)")
    args = parser.parse_args()

    tag = args.tag
    if tag is None:
        print("Resolving latest n8n release tag ...")
        tag = get_latest_tag()
    print(f"Tag: {tag}")

    print("Downloading tarball ...")
    tarball = download_tarball(tag)

    print("Extracting tarball ...")
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        root_dir_name = extract_tarball(tarball, tmp)
        repo_root = tmp / root_dir_name

        all_records = []

        for pkg_rel in NODE_PACKAGES:
            nodes_dir = repo_root / pkg_rel
            source_package = pkg_rel.split("/")[1]  # "nodes-base" or "@n8n"
            print(f"\nWalking {pkg_rel} (source_package={source_package}) ...")
            recs = walk_package(nodes_dir, source_package, tag)
            print(f"  found {len(recs)} nodes")
            all_records.extend(recs)

    print(f"\nTotal nodes extracted: {len(all_records)}")

    # Normalize and deduplicate across packages
    normalized = []
    seen = set()
    for rec in all_records:
        normalized_rec = normalize_record(rec)
        key = normalized_rec["node_name"] or normalized_rec["display_name"]
        if key in seen:
            continue
        seen.add(key)
        normalized.append(normalized_rec)

    print(f"After dedup: {len(normalized)} nodes")

    # Sort by source_package then node_name
    normalized.sort(key=lambda r: (r["source_package"], r["node_name"]))

    print("\nWriting output files ...")
    write_json(normalized, OUT_DIR / "nodes.json")
    write_parquet(normalized, OUT_DIR / "nodes.parquet")

    print(f"\nDone. Tag: {tag}  Nodes: {len(normalized)}")


if __name__ == "__main__":
    main()
