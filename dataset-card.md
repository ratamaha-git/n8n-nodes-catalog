---
license: cc-by-4.0
task_categories:
  - text-generation
  - text-classification
language:
  - en
size_categories:
  - 1K<n<10K
tags:
  - n8n
  - workflow-automation
  - node-metadata
  - agent-tooling
  - llm-training
  - structured-data
  - no-code
pretty_name: n8n Nodes Catalog
---

# n8n Nodes Catalog

A structured, machine-readable catalog of n8n node metadata extracted directly from the [n8n GitHub repository](https://github.com/n8n-io/n8n). Covers 524 nodes across `packages/nodes-base` (431 nodes) and `packages/@n8n/nodes-langchain` (93 nodes), sourced from n8n@2.20.6.

Updated monthly. Last updated: 2026-05.

## Dataset Summary

This dataset catalogs what each n8n node *is*: its name, category, supported operations, credential requirements, properties schema, and source location. Existing n8n datasets on HuggingFace (workflow collections, builder training sets) focus on *how* workflows are assembled. This dataset fills the gap underneath - the node-level metadata that lets an AI agent reason about which nodes to use and what they support, without guessing from stale training data.

## Intended Uses

**LLM training and fine-tuning.** Ground models in current n8n node capabilities. A model that has seen this catalog stops hallucinating node names and operation signatures.

**Agent tooling at inference time.** An AI agent building an n8n workflow can load this dataset as context to select the right node, check credential requirements, and validate operation names before generating a workflow.

**Developer reference.** "What n8n nodes support database operations?" is currently a docs-browsing exercise. With this dataset it is a one-liner (see Sample Queries below).

**Research.** Quantitative analysis of the n8n node ecosystem: coverage by category, credential distribution, operation surface area over time.

## Files

| File | Description |
|---|---|
| `nodes.json` | Canonical record-per-node, UTF-8 JSON array |
| `nodes.parquet` | Columnar output, Snappy-compressed, HuggingFace dataset viewer-ready |
| `extract.py` | Extraction script - run to regenerate |

## Schema

All fields are locked to what the extraction script (`extract.py`) produces. Do not rely on fields not listed here.

| Field | Type | Notes |
|---|---|---|
| `node_name` | string | Internal identifier (e.g. `slack`, `airtable`). Matches the `name` field in `INodeTypeDescription`. |
| `display_name` | string | Human-readable name shown in the n8n UI. |
| `categories` | list[string] | Node category tags from `.node.json` codex (authoritative) or inline `codex.categories` in `.node.ts` (fallback). Examples: `Communication`, `AI`, `Data & Storage`. |
| `subcategories` | list[string] | Subcategory values, flattened from the `codex.subcategories` dict. Keys (parent categories) are dropped; only the leaf values are kept. |
| `group` | list[string] | n8n execution group: `input`, `output`, or `transform`. |
| `version` | string | For single-version nodes: the explicit `version` value. For multi-version nodes (`defaultVersion`): the current default version. |
| `description` | string | One-line description from `INodeTypeDescription.description`. |
| `credentials_required` | list[string] | Credential type names from the node's `credentials` array. Empty for trigger nodes, core nodes, and multi-version nodes where credentials live in versioned implementation files. |
| `operations_supported` | list[string] | Values from the `operation` property options array. Falls back to `resource` options if no `operation` property exists. Empty for nodes without a resource/operation picker (e.g. webhooks, core transforms). |
| `properties_schema` | string (JSON) | Compact array of top-level property descriptors: `[{"name": "...", "displayName": "...", "type": "..."}]`. Top-level only - nested options are not included. Serialized as a JSON string. |
| `source_package` | string | `nodes-base` or `@n8n` (for nodes-langchain nodes). |
| `source_file_path` | string | Repo-relative path to the primary `.node.ts` file. |
| `github_permalink` | string | Permanent GitHub link to the file at the extracted tag. |

**Note on list fields in Parquet:** `categories`, `subcategories`, `group`, `credentials_required`, and `operations_supported` are stored as JSON strings in the Parquet file (e.g. `'["Communication","HITL"]'`). Parse with `json.loads()`.

## Methodology

The catalog is extracted from the n8n GitHub repository using `extract.py`. The script:

1. Downloads the n8n release tarball for the target tag (default: latest release). The tarball is cached locally to make re-runs fast.
2. Walks `packages/nodes-base/nodes/` and `packages/@n8n/nodes-langchain/nodes/`, collecting `.node.ts` files that are NOT inside versioned or implementation sub-directories (`v1/`, `v2/`, `V1/`, `V2/`, `actions/`, `methods/`, `transport/`, etc.).
3. For each `.node.ts`, parses the TypeScript source with a targeted regex/AST approach to extract the `INodeTypeDescription` fields. Also reads the sibling `.node.json` codex file for category metadata when present.
4. Handles multi-version nodes by reading `baseDescription` from the primary file and recording `defaultVersion`. Versioned implementation files (`V1/`, `V2/`, etc.) are excluded as they are not standalone nodes.
5. Emits `nodes.json` (UTF-8 JSON array) and `nodes.parquet` (Snappy-compressed columnar).

The script is idempotent: re-running with the same tag produces identical output. Run with `--tag n8n@2.20.6` to pin to a specific release.

**What is not included:** credentials definitions, utility modules, the core workflow engine, EE-only nodes that don't follow the standard descriptor pattern.

## Update Cadence

This dataset is updated monthly via an automated pipeline. The `github_permalink` field anchors each record to the specific tag it was extracted from, so older rows remain stable across updates.

The `Last Updated` field at the top of this card tracks the most recent extraction run.

## Sample Queries

**Find all nodes that support Slack operations (pandas):**

```python
import pandas as pd, json
df = pd.read_parquet("nodes.parquet")
df["ops"] = df["operations_supported"].apply(json.loads)
slack_nodes = df[df["node_name"].str.contains("slack", case=False)]
print(slack_nodes[["display_name", "ops"]])
```

**List all nodes requiring OAuth2 credentials (HuggingFace datasets API):**

```python
from datasets import load_dataset
import json
ds = load_dataset("automatelab/n8n-nodes-catalog", split="train")
oauth = [r for r in ds if "oAuth2Api" in json.loads(r["credentials_required"])]
print([r["display_name"] for r in oauth])
```

**Count nodes by category (SQL via DuckDB):**

```sql
SELECT category, COUNT(*) as node_count
FROM read_parquet('nodes.parquet'),
     UNNEST(json_extract_string(categories, '$[*]')) AS t(category)
GROUP BY category
ORDER BY node_count DESC;
```

## Companion Blog Post

A detailed companion post covering the dataset, queryability, and AI-agent use cases is forthcoming at [automatelab.tech](https://automatelab.tech). This card will be updated with a direct link when it publishes.

## License

**Our additions** (catalog format, extraction script `extract.py`, this dataset card, and any editorial framing) are licensed under [CC-BY-4.0](https://creativecommons.org/licenses/by/4.0/).

**Upstream node metadata:** The node metadata in this catalog is derived from n8n source code. Upstream node metadata copyright n8n team, used under the n8n Sustainable Use License. This dataset is a community-maintained catalog/index of that metadata, not a redistribution.

The n8n Sustainable Use License permits derivative works free of charge for non-commercial use and requires preservation of the copyright notice. The full license text is available at [https://docs.n8n.io/sustainable-use-license/](https://docs.n8n.io/sustainable-use-license/) and in the [n8n repository](https://github.com/n8n-io/n8n/blob/master/LICENSE.md).

Explicit attribution: *Upstream node metadata copyright n8n team, used under n8n SUL. This dataset is a community-maintained catalog/index of that metadata, not a redistribution.*

## Maintainer

[AutomateLab](https://automatelab.tech) - AI automation guides and tools.
