# n8n-nodes-catalog

Source for the [`automatelab/n8n-nodes-catalog`](https://huggingface.co/datasets/automatelab/n8n-nodes-catalog) HuggingFace dataset — a structured, machine-readable catalog of n8n node metadata extracted from the [upstream n8n repo](https://github.com/n8n-io/n8n).

The dataset on HuggingFace is the source of truth. This repo holds the extraction script, the most recent snapshot, the dataset card, and the monthly update workflow.

## Repo layout

| Path | What |
|---|---|
| [`extract.py`](extract.py) | Extraction script. Downloads the n8n release tarball, walks the node packages, emits `nodes.json` + `nodes.parquet`. |
| [`nodes.json`](nodes.json) | Canonical record-per-node snapshot, UTF-8 JSON. |
| [`nodes.parquet`](nodes.parquet) | Columnar snapshot, Snappy-compressed. |
| [`dataset-card.md`](dataset-card.md) | HuggingFace dataset card. Uploaded to HF as `README.md` by the workflow. |
| [`.github/workflows/monthly-update.yml`](.github/workflows/monthly-update.yml) | Re-runs extraction on the 1st of each month, uploads to HF if data changed. |

## Running locally

```bash
pip install requests pyarrow huggingface_hub
python extract.py                  # latest n8n release
python extract.py --tag n8n@2.20.6 # pinned tag
```

Outputs `nodes.json` and `nodes.parquet` in the repo root.

## Updating the dataset

Manual trigger: GitHub Actions → "n8n nodes catalog monthly update" → Run workflow.

The workflow:
1. Runs `extract.py` against the latest n8n release.
2. Hashes the new `nodes.json` and compares it with the HF-hosted copy.
3. If changed: bumps the `Last updated` line in `dataset-card.md`, uploads `nodes.json` + `nodes.parquet` + `dataset-card.md` (as `README.md`) to HF.
4. If unchanged: skips the upload.
5. On failure: emails `a@1n.ax` via SMTP.

Required GitHub secrets: `HF_TOKEN`, `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASS`.

## License

Our additions (extraction script, dataset card, repo glue): [CC-BY-4.0](https://creativecommons.org/licenses/by/4.0/).

Upstream node metadata: copyright n8n team, used under the [n8n Sustainable Use License](https://docs.n8n.io/sustainable-use-license/). This dataset is a community-maintained catalog of that metadata, not a redistribution.

## Maintainer

[AutomateLab](https://automatelab.tech)
