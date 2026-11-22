# OTC Silver notebooks

These notebooks extend the Bronze discovery notebooks with canonical Silver tables, effective-dated demo mappings, traceable finance controls, and an optional local snapshot. Each notebook opens directly against committed Bronze inputs. It runs the prior steps in its own kernel so there are no cross-notebook state dependencies.

## Run in VS Code on Windows

1. Extract the ZIP from the repository root so its `silver/` files sit next to your existing `silver/bronze_review.py` and notebooks 01–04. The companion Bronze helper is also included for a clean workspace.
2. Start the Bronze MinIO API on port `19000`. Use the existing `silver/compose.bronze-local.yaml` override and the commands in your Silver discovery README. The Bronze console uses a separate port.
3. Open the `silver/` folder in VS Code and select the existing Silver virtual environment as the Python kernel. For a new kernel, run the commands below from `silver/`.

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

The included helper uses the existing `ingestion-airflow/platform.env` for Bronze S3 credentials. Keep that file in the project beside the `silver/` folder. The JSON settings template contains only modeling settings and Bronze object keys.

| Notebook | Output to review |
|---|---|
| `05_silver_masters_orders.ipynb` | Legal entities, source customer and product, warehouses, accounts, and normalized order headers and lines |
| `06_silver_mdm.ipynb` | Customer, product and account crosswalks, approval coverage, and a conformed order-line preview |
| `07_silver_fulfillment_inventory.ipynb` | Dispatches, return lines and Stock Entry movement legs |
| `08_silver_finance.ipynb` | Billing, credits, payment allocations, GL and source AR snapshots |
| `09_silver_channels_bank_budget.ipynb` | Logical events and deliveries, POS, bank match candidates and budget amounts |
| `10_silver_reconcile_publish.ipynb` | Complete build, reconciliation and exception reports, plus optional local JSONL snapshot |

Run 05–10 in order for a walkthrough. Run only 10 when you want the complete build and validation. Notebook 04 remains the two-order design example.

## Pin inputs and repeat a run

The default selects the newest completed ERP, dealer SQL and POS manifests by publication time, and the newest dealer export by export cutoff. It requires a unique finance and MDM manifest per category, unless pinned. All landed Kafka records are included by default. Every notebook prints the selected manifests before showing results. Bronze source families can arrive at different times, so check their selections before interpreting a cross-source mismatch.

For a fixed baseline or a Round 2 comparison, copy `silver_settings.example.json` to `silver_settings.json`. Set the exact Bronze manifest keys and, when needed, the list of exact Kafka delivery keys. For a reproduced run, copy the input selection from the published manifest into the settings file. Compare runs with their respective manifest keys and business cutoffs. A dealer AR observation on 16 December and an observation on 10 December are two distinct balances.

The baseline fixture checks at the 10 December 2026 cutoff cover P-S1 and D-S1. Expect parent 10 pumps and ₹100,000 booked, dealer 1 pump and ₹40,000 booked, net billing totaling ₹135,000, and open AR totaling ₹100,000 across those named orders. New Round 2 rows remain identifiable. The dealer credit lacks an original invoice ID. Silver can reconcile the credit to GL and record the source AR observation. Its calculated invoice balance remains uncovered when credit allocation evidence is missing.

## MDM decisions and coverage

The seeded crosswalks use the configured fixture effective date. The parent ERPNext customer alias is obtained from the explicit OTC seed name, and the actual native ID remains on the local master and order. This alias extraction is specific to this lab. An approved automatic customer pair requires a unique alias on each side and the same recorded registration value. Product approvals and account classifications come from the committed fixture manifest.

Pending candidates remain pending by default. To exercise a steward decision, set an entry under `mdm_steward_decisions` keyed by the candidate ID displayed in notebook 06. For example, the seal product candidate ID is the JSON array string `["product","P-SEAL","D-SEAL"]`. An entry requires `effective_from`, `reviewer` and `evidence`. To exercise the subsequent pack conversion, set `seal_conversion` with its own `effective_from`, `reviewer` and `evidence`. It applies only to D-SEAL and uses the documented fixture factor of ten. A conversion effective after an order date leaves that order's conformed quantity unresolved.

| Coverage boundary | Behavior |
|---|---|
| Dealer invoice lines | Keep document totals. |
| Dealer credit with no source invoice link | Keep the credit and report the incomplete invoice-level coverage. |
| Bank account missing from statement fields | Keep the statement and mark its payment match for review. Account configuration requires recorded evidence. |
| Stock-affecting shipments and returns | The movement dataset currently contains ERPNext Stock Entry legs. |
| Catalog pricing, user master and full operational history | Remain design coverage until appropriate source evidence lands. |

## Publish the Silver review output

Notebook 10 defaults to preview. Set `PUBLISH_LOCAL = True` to write under `silver_output/`. Each snapshot includes table JSONL, a checksum manifest, observations, control results, and exceptions. Failed numerical controls and error-level exceptions block publication. Review and not-covered statuses remain visible in the output. Monetary decimals are serialized as strings. The format supports immutable review snapshots and exact reruns. It does not implement a production Silver table store or an SCD history model.
