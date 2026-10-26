# Fe2gen — From Fetal to Gene
Vietnamese doctor-reviewed workspace from fetal ultrasound findings to HPO terms, disease profiles and genes.

## Use
1. Open the GitHub Pages site.
2. Log in with the doctor account created by the administrator (or, on a server without accounts, choose
   **Kết nối** and enter the shared access code).
3. Search HPO terms or extract phrases from an ultrasound paragraph.
4. Review each HPO and its Có / Nghi ngờ / Không status.
5. Confirm review and compare disease profiles. Results are shown in three columns, left to right
   in the usual prenatal test order: chromosomal (QF-PCR/karyotype), CNV (CMA), single gene (exome/panel),
   plus diseases with no established genetic mechanism.
6. Select diseases for the lab handoff sheet and print/save PDF.

The UI does not persist case text, the session or the access code. Only the API address is saved.
Logging out clears the open case from the tab.
Similarity is IC-weighted phenotype similarity, not disease probability.
Unrecorded features include postnatal findings and require clinician interpretation.
Gene associations are displayed as associations, not proof of causality.
This is a research/clinical-review tool, not a validated diagnostic device.

## Layout
- `docs/`: dependency-free HTML/CSS/JavaScript published by GitHub Pages.
- `backend/`: Python HTTP boundary, existing matching engine, assertion rules and strict Model 1 runner.
- `backend/resources/disease_category.tsv`: mechanism group of each HPOA disease (IDs and groups only).
- `backend/tools/`: `build_disease_category.py` rebuilds that table; `build_mechanism_dataset.py` builds the case table for the mechanism classifier; `evaluate_model2.py` measures Model 2 as the web shows it.
- `training/`: Naive Bayes / SVM / Random Forest / XGBoost comparison for the mechanism classifier (numpy, scikit-learn, xgboost only).
- Clinical cases, HPO data files, model weights, credentials and training runs are not published here.

## Model 2 mechanism groups
Model 2 ranks every disease profile by IC-weighted coverage, then lists the best 10 per group
(5 for the unclassified group), keeping each disease's overall rank. A disease is shown only when it shares
at least one reviewed finding. Groups come from MONDO ancestry (aneuploidy/polyploidy/ring chromosome -> NST;
partial deletion/duplication and other segmental chromosomal disorders -> CNV), the disease name, and germline
genes from Orphanet/OMIM (-> DON_GEN); `co_che_hon_hop=1` marks chromosomal/CNV diseases that also have a
Mendelian gene. Sources are MONDO and Orphanet product6 releases of 2021-12 (versions and SHA-256 in the file header).
Diseases absent from HPOA, such as Klinefelter syndrome, cannot be suggested.
A table rebuilt into `backend/data/disease_category.tsv` takes precedence over the shipped copy.
OMIM and ORPHA IDs of the same disease (one-to-one pairs sharing a MONDO ID, `backend/resources/disease_xref_mondo.tsv`,
2,036 pairs) are shown as one card, keeping the better-ranked ID and listing the other; genes and inheritance
modes of both IDs are combined. Requests are served one at a time and wait up to 90 s in a queue.

A learned re-ranker (`backend/model2_reranker.py`, model in `backend/resources/model2_reranker.json`, numpy only)
reorders the top 100 of each column and gives the "top 5" list that mixes the groups. It is gradient-boosted trees
(XGBoost pairwise ranking) over 20 features of each candidate (coverage, likelihood, frequency-weighted coverage,
profile size, matches, column rank, group, GenCC validity from `backend/resources/gencc_validity.tsv`, CC0).
Training cases: 48 published prenatal cases and 87 hospital cases whose label is the doctor's first suggestion
(so it learns the NST > CNV > single-gene priority doctors use). Chosen by cross-validation before the test.
Fu2022 test (205 prenatal single-gene cases, scored once, not used in training): correct disease in the top 5 of
its column 23 -> 34, shown on the web 35 -> 48, overall top 10 30 -> 35. Scores are ranking scores, not probabilities.
Without the model file the web falls back to the ic_coverage order.

WES mode: a candidate-gene list from an exome report (optional field) restricts a separate panel to diseases
of those genes, in the same phenotype order, and ranks the genes. Spike-in evaluation (causal gene + N random
disease genes, 10 draws, re-ranker frozen, nothing tuned): Fu2022 test, correct gene in the top 5 at
N = 20/50/100: 91.7% / 82.4% / 70.1% (random order 23.8% / 9.8% / 5.0%). This assumes the exome found the
causal variant; the lists are simulated, not real VCFs.

The mechanism classifier in `training/` is not used by the web service. Its first run used labels derived
from doctors' first-listed suggestions, not karyotype/CMA/exome results, so its scores measure agreement
with those suggestions, not diagnostic accuracy.

## Backend
Use the existing Python environment on the Ubuntu GPU host.
Supply licensed/local files in `backend/data/`: `hp.obo`, `phenotype.hpoa`,
`genes_to_disease.txt`, `hpo_catalog_vi.json`; the clinical phrase dictionary is optional.
Set `MODEL1_ADAPTER` to the selected LoRA directory and `MODEL1_WORKER_DIR`
to the original evaluated v3.8 package (worker.py, prepare.py, core.py, tokenizer).
The serving wrapper calls that worker without changing training files.

Run `python serve_web.py` from the backend directory.

GPU on demand (`model1_process.py`): the web process never loads torch. Model 1 runs in a child process that
starts when a doctor logs in, opens **Nhập đoạn văn** or sends a report, and exits after `MODEL1_IDLE_MINUTES`
(default 60) without doctor activity, or when the last doctor logs out; the driver then gets back all of its
VRAM (about 12.4 GB for model1_v7_prod). A request arriving while the model loads waits up to
`MODEL1_LOAD_WAIT_SECONDS` (120). The model is not started when the GPU has less than `MODEL1_MIN_FREE_MIB`
(13000) free, or while the pause file exists (`bash gpu.sh pause`, for training or local AI; `bash gpu.sh resume`).
`MODEL1_PRELOAD=1` loads it at startup (still released when idle). The journal shows the load time per step.
If Model 1 cannot load (for example no NVIDIA driver after a kernel update and reboot), HPO search and disease
matching keep working, `/api/status` reports `model_state: failed` and text extraction answers 503; the next
login or extraction tries again, no restart needed.

Accounts and roles (`auth.py`, `audit.py`, admin page `docs/admin.js`):
- `admin` (everything plus the admin page), `doctor` (search, text extraction, matching, lab sheet) and `lab`
  (search, matching, lab sheet; no text extraction, so a lab login never loads the GPU).
- The first admin is created on the server: `python manage_users.py add <username> --name "<name>" --role admin`.
  Admins then create accounts on the web; each new or reset account gets a one-time password shown once, which
  the user must replace at the first login. Admins also lock/unlock, change roles, delete, end sessions, release
  or pause the GPU and read the audit log; the last active admin cannot be removed, demoted or locked.
- Accounts live in `WEB_USERS_FILE` (default `~/prenatal_pubcasefinder/.deployment/users.json`, scrypt hashes,
  mode 600); the service rereads it, so `manage_users.py` (`add`, `passwd`, `role`, `disable`, `enable`, `remove`,
  `list`) works while it runs. Once it lists an account, the shared `WEB_ACCESS_TOKEN` is no longer accepted.
- Passwords: at least 10 characters, not a common password, not containing the username. Five wrong passwords in
  a row lock a username for 1, 2, 4 ... up to 15 minutes; more than 30 wrong passwords in a minute pause all
  logins for that minute. Sessions live in memory (12 h, 4 h idle); changing a password ends the other sessions.
- Audit log `WEB_AUDIT_FILE` (default `~/prenatal_pubcasefinder/.deployment/audit.jsonl`): logins, logouts,
  password changes, admin actions, and each extraction or matching with counts only, never report text, HPO or genes.
- Responses carry a Content-Security-Policy (own scripts only) and `X-Frame-Options: DENY`.
Set `WEB_ALLOWED_ORIGINS` (and accounts or `WEB_ACCESS_TOKEN`) before enabling public HTTPS.
The service binds loopback; HTTPS is provided by the Ubuntu reverse proxy.
`server.py` is retained only as a compatibility base for data loading. Use `serve_web.py` as the entrypoint.

## Training and updates
The web service uses a fixed adapter path. Training completion never automatically promotes a checkpoint.
If training needs the GPU, run `bash gpu.sh pause` first (search and matching stay online) and
`bash gpu.sh resume` after training.
Validate a new adapter separately before changing `MODEL1_ADAPTER` in the private environment file.
Restart and smoke-test extraction and ranking; revert the path if validation fails.
No training is started by this web application.

## Vận hành an toàn
Server-side operations, all run by hand on the Ubuntu host (nothing here runs automatically):
- `backend/deploy_ubuntu.sh` writes the user unit with systemd sandboxing (`PRENATAL_HARDENING=full|basic|off`),
  restarts the service, falls back to a weaker mode or the previous unit if `/api/status` does not answer, and
  prints `systemd-analyze security --user prenatal-web`. It never rewrites an existing `web.env`.
- `backend/ops/harden_host.sh` (sudo, `--dry-run` first): ufw open only on Tailscale, SSH keys only,
  unattended security updates (kernel/NVIDIA kept manual), fail2ban for sshd.
- `backend/ops/backup_deployment.sh`: daily encrypted backup (age or gpg) of accounts, logs and `web.env`; restore steps.
- `backend/ops/rotate_token.md`: remove the shared `WEB_ACCESS_TOKEN` once accounts exist.
- `backend/ops/separate_user.md` and `backend/ops/prenatal-web.system.service.example`: run the service as a
  dedicated user without sudo, as a user or a system unit.
- `backend/ops/ADR-funnel-vs-tailnet.md`: public Funnel with admin 2FA versus tailnet-only access.
- `backend/ops/nckh_purge_history.md`: removing clinical files and secrets from the old `C:\NCKH` repository
  (destructive; for the owner to run).

## Bản cũ `web_prenatal_pubcasefinder` trong `C:\NCKH` là lưu trữ, không triển khai
That older web has only a shared access code, no personal accounts, 2FA or audit log. Keep it as an archive;
deploy only this repository.

## Checks
`python test_access.py` checks Model 1 on demand (idle release, pause file, free-VRAM check, crash recovery) and
doctor accounts, without GPU or HPO data. `python test_web.py` checks search, alias normalization, input validation, ranking metadata,
mechanism grouping and the shipped group table, review requirements, rejection of ambiguous span alignment,
and that the service starts without a usable GPU.
GPU/API/UI deployment checks are documented separately; these tests do not establish clinical accuracy.

