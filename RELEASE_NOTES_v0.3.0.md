# Visdom v0.3.0

This release covers all work merged since **v0.2.4** (February 2023), spanning roughly three and a half years of accumulated development. It introduces a pluggable persistence layer, a new experiment-tracking API, eight new visualization types, HTTPS and OpenAPI support, and a broad set of stability and security fixes across the plotting, embeddings, and socket layers. End-to-end testing is mid-migration from Cypress to Playwright, with both suites present, and a Python unit test suite (pytest) has been added alongside them.

---

# Before You Upgrade

This is a large release. Most of it is additive, but **five changes can break an existing setup**. Please read this section before running `pip install -U visdom`.

### 1. Python 3.12 is now the minimum (was 3.10)

`visdom` now declares `python_requires>=3.12`. On Python 3.10 or 3.11, `pip install -U visdom` will **refuse to install this version** and silently leave you on v0.2.4 — or, with an older pip, install a package that cannot import.

The `six` dependency has also been dropped.

**What to do:** upgrade the interpreter first, and prefer a clean virtualenv over upgrading in place:

```bash
python3.12 -m venv .venv && source .venv/bin/activate
pip install -U visdom
```

### 2. `vis.win_hash()` and the `/win_hash` endpoint are gone

Any code calling `vis.win_hash()`, or any non-Python client hitting `/win_hash`, will break. The capability is superseded by a window-versioning scheme that requires no client-side call.

Existing saved environment files are **not** affected — window versioning is backwards compatible with the old JSON format.

### 3. `openTSNE` is now a hard install dependency

The embeddings pane moved off the CUDA-only `tsnecuda` onto `openTSNE`. This removes the CUDA requirement, but `openTSNE` is now a **required** dependency pulled in by every install, including on machines that never open an embeddings pane. It ships compiled extensions, so expect a longer install (and a build step on platforms without a matching wheel).

### 4. The server no longer fails when its port is busy

Previously, starting the server on an occupied port raised. It now logs a warning and binds an **arbitrary free port assigned by the OS** instead:

```
WARNING:root:Port 8097 is already in use, assigning a free port
```

The actual port is printed in the startup banner. If you have scripts, containers, or clients that assume `8097`, they will now connect to nothing while the server appears to have started fine. Check the banner, or pin the port and verify it is free before launch.

### 5. Authentication behaviour changed on two paths

- **Unauthenticated requests now return HTTP 401 instead of 400.** Any client that branches on a `400` status to detect an auth failure needs updating.
- **`WrappedSocketWrap.post` now requires authentication.** This endpoint was previously reachable without credentials; unauthenticated automation against a login-enabled server will now be rejected. (This was a security hole — see Security.)

### What does *not* break

To be explicit, since this release is large:

- **Saved environments load as-is.** The new `DataStore` layer preserves the classic one-file-per-environment layout under `~/.visdom/`. No migration step, no export/reimport.
- **The default port (`8097`), hostname, and env path (`~/.visdom/`) are unchanged.**
- **Existing environment JSON files remain compatible** with the new window versioning.
- **Server login credentials are established at startup** from a prompt or the `VISDOM_USERNAME`/`VISDOM_PASSWORD` env variables, so the switch to salted PBKDF2 hashing requires no action from you — there is no stored password hash to migrate.

---

# Highlights

- **Pluggable storage backend** — environment persistence now runs through a `DataStore` abstraction instead of ad hoc JSON file I/O.
- **Experiment tracking API** — new `vis.experiment` / `vis.log_metrics` / `vis.finish_experiment` client methods and a server-side `/experiments/log` endpoint.
- **Eight new visualization types**: Sankey diagrams, violin plots, confusion matrices, ROC/PR curves, 2D histograms, 3D line plots, formatted HTML tables, and parallel coordinates.
- **HTTPS support**, a **health check endpoint**, and a full **OpenAPI 3.1 specification** for language-agnostic API access.
- **PyTorch and scikit-learn logging integrations** (`VisdomLogger`, `VisdomSklearnLogger`) for automatic training-metric logging.
- **New workspace features**: undo for closed panes, batch environment delete, save-all-environments, per-pane comment threads, and a toast notification system.
- **Security hardening**: salted PBKDF2 password hashing, authentication added to a previously-unprotected socket endpoint, and consistent HTTP 401 responses for unauthenticated requests.
- **Embeddings pane** rewritten onto `openTSNE` and Three.js `BufferGeometry`, fixing WebGL context leaks and idle CPU usage.
- End-to-end tests being migrated from **Cypress to Playwright** (both suites currently ship), plus a new **pytest**-based Python unit test suite.
- Minimum supported Python raised to **3.12**; the `vis.win_hash()` API and `/win_hash` endpoint have been removed — see **Before You Upgrade**.

---

# Features

## Architecture & Persistence

Environment storage was previously implemented as direct JSON file reads/writes scattered across the server's request handlers. It's now routed through a `DataStore` interface with a `JSONStore` implementation that preserves the existing one-file-per-environment on-disk layout, so existing saved environments continue to load without migration. Environment saves, loads, deletes, layout persistence, undo stacks, and the remaining read paths (environment listing, comparison) were incrementally moved onto the new abstraction. This also lays the groundwork the Experiment Tracking API is built on.

<details>
<summary>Technical details</summary>

Components affected:
- `py/visdom/data_model/` (new)
- `py/visdom/server/handlers/web_handlers.py`, `socket_handlers.py`
- `py/visdom/utils/server_utils.py`

Related Pull Requests:
- #1518 (add DataStore abstraction with JSON backend)
- #1566 (route environment saves through DataStore)
- #1569 (route environment loads through DataStore)
- #1580 (route env delete, layouts, and undo persistence through DataStore)
- #1584 (route remaining env reads — EnvHandler, gather envs, compare envs)
- #1588 (final data-abstraction-layer wiring)
</details>

## Experiment Tracking

Adds `vis.experiment`, `vis.log_metrics`, and `vis.finish_experiment` client methods that post to a `/experiments/log` Tornado handler. The handler persists experiment metadata and metrics through an `ExperimentStore` layered on top of the `DataStore`, and mirrors the data into in-memory environment state so a full-environment save preserves it. Once an experiment is marked finished or failed, further writes are rejected with HTTP 409; malformed requests return 400, and finishing a nonexistent experiment returns 404. The endpoint respects server-wide readonly mode (returns 403).

<details>
<summary>Technical details</summary>

Components affected:
- `py/visdom/experiments/` (models, store)
- `py/visdom/server/handlers/web_handlers.py` — `ExperimentLogHandler`
- `py/visdom/server/app.py`, `openapi.yaml`

Related Pull Requests:
- #1593 (experiment metadata model + store)
- #1595 (experiment tracking API + `/experiments/log` endpoint)
</details>

## Visualizations

**Eight new plot and pane types:**

- **Sankey diagrams** via `vis.sankey()`
- **Violin plots** via `vis.violin()`
- **Confusion matrix** panes with a Python API helper
- **ROC and precision-recall curve** panes with Python API helpers
- **2D histograms** via `vis.histogram2d()`
- **3D line plots**, extending `vis.line()`
- **Formatted HTML tables** via `vis.table()`
- **Parallel coordinates** plots, aimed at multi-metric experiment comparison

**New options on existing plots:** an `opts.caption` option available on all plots; a free-draw annotation mode with its own update pipeline; alpha-channel support for images; per-point/per-label `markersize` arrays; `tight_layout` support to reduce plot margins; `store_history` for scatter/line plots; a `learning curve` convenience method; heatmap overlays for images via `vis.image_heatmap()`; caption support on audio and video panes; float Y-axis labels for scatter plots; and programmatic saving of Plotly figures to image files.

<details>
<summary>Technical details</summary>

Related Pull Requests (new plot/pane types):
- #1457 (Sankey), #1259 (violin), #1486 (confusion matrix), #1478 (ROC/PR curves),
  #1428 (histogram2d), #1357 (3D line), #1488 (`table()`), #1361 (parallel coordinates)

Related Pull Requests (plot options):
- #1596, #1563, #1300, #1313, #1182, #1183, #1568, #1572, #1302, #1277, #1021
</details>

## Export & Sharing

Pane-level export to PNG/JPG/SVG, and an option to export a plot's metadata alongside its data. You can also export a full environment to a standalone HTML file, or use the **"Save All Environments"** action.

<details>
<summary>Technical details</summary>

Related Pull Requests: #1652, #1298, #1198, #1295
</details>

## Workspace & Environments

- **Undo for closed panes** — recover a pane after accidentally closing it.
- **Batch delete** for environments.
- **Comment box** on every pane.
- **Toast notification system**, used for backend errors and recovery events.
- Environment groups now **start minimized by default**.
- **Environment filtering** and a `get_env_state` client API.
- Environment names shown in **legend titles** when comparing environments.
- **LaTeX rendering** in the Properties Pane.
- **Automatic restore** of saved environments on server start.
- **Upload a JSON file to load an environment** from the UI.
- Environment names too long to be a valid filename are stored as `hash_<sha256>.json` with the real name kept inside the file. Normally-named environments keep their existing `<name>.json` filename.
- **Text panes** gained auto-scroll-to-bottom and clipboard-copy support.

<details>
<summary>Technical details</summary>

Related Pull Requests: #1446, #1175, #1608, #1586, #1592, #1356, #1318, #1323,
#1646, #1293, #1209, #1242, #1046, #1312
</details>

## Image Handling

Side-by-side image comparison across environments, a reworked image slider (`update_image_slider` API, fixing three related bugs), and image selection (`image_select()` / `image_update_selected`).

<details>
<summary>Technical details</summary>

Related Pull Requests: #1204, #1266, #1335
</details>

## Embeddings

Lasso-selection interactions were extended with a closing-circle indicator and a minimum-selection hint.

<details>
<summary>Technical details</summary>

Related Pull Requests: #1475
</details>

## Framework Integrations

Two new logging integrations for training loops: `VisdomLogger` for PyTorch training metrics, and `VisdomSklearnLogger` for scikit-learn estimator and cross-validation search results.

<details>
<summary>Technical details</summary>

Related Pull Requests: #1562, #1603
</details>

## API & Server

- **HTTPS support**, configurable via `run_server.py`.
- **Health check endpoint** for monitoring server liveness.
- **OpenAPI 3.1 specification** (`openapi.yaml`) documenting the full HTTP API.
- **Port validation** for the `-port` argument, and an automatic fallback to a free port when the configured one is in use — see **Before You Upgrade**, as this changes startup behaviour.

<details>
<summary>Technical details</summary>

Related Pull Requests: #1487, #1157, #1328, #1138, #1351
</details>

---

# Improvements

## Embeddings

The CUDA-only `tsnecuda` dependency was replaced with `openTSNE`; the pane was migrated to Three.js `BufferGeometry`; rendering is now on-demand rather than continuous; embeddings updates were moved outside the generic update flow; and event registration can now be disabled.

<details>
<summary>Technical details</summary>

Related Pull Requests: #1500, #1510, #1366, #1372, #1585
</details>

## Client Architecture

`main.js` and related panes were migrated from class components to functional React components with hooks, the client was upgraded to React 17, the socket/relayout callback system was reimplemented, server-communication logic was extracted into a dedicated class, and unused dependencies (`md5`, `json-stable-stringify`, `react-select`) were removed. Server-side socket handler code was also deduplicated.

<details>
<summary>Technical details</summary>

Related Commits:
- `de687db` migrate main.js to functional react
- `a85e9e6` upgrade to react 17
- `910339c` reimplement setState-callbacks
- `34d62c3` reimplement relayout using the new callbacks-loop
- `fa1a3a8` extract server-communication into a dedicated class
- `8f697cc` deduplicate server-side socket handler code
</details>

## Server & Performance

Incoming-message handling is now shared between the WebSocket and polling transports; socket handler initialization was consolidated into a common base handler; message dispatching was extracted out of the Poller; and `update_packet` payloads are built without a full deepcopy.

<details>
<summary>Technical details</summary>

Related Pull Requests: #1643, #1591, #1505, #1297
</details>

---

# Bug Fixes

## Socket & Connectivity

PID-based lazy reconnection to prevent client hangs on stale connections; isolated event-subscriber errors; standardized JSON message serialization; FIFO message ordering for polling mode; capped unbounded in-memory data growth; fixed crashes from invalid saved layout JSON (now recovered with a toast); guarded against `KeyError` when an environment or pane is deleted mid-request; fixed live updates not applying in compare-mode subscriptions; fixed malformed update requests returning the wrong status code; and improved WebSocket disconnect/error handling.

<details>
<summary>Technical details</summary>

Related Pull Requests: #1367, #1485, #1503, #1248, #1320, #1560, #1390, #1480, #1401

Related Commits:
- `48de4cee`, `25a10806` — WebSocket error logging and duplicate-disconnect prevention
- `440643b3` — consolidated `run_server` hostname/logging fixes
</details>

## Plotting & Rendering

Fixed NaN/Inf handling across several code paths (a new `NanSafeEncoder` replaced ad hoc `nan2none()` handling; `np.nanmin`/`nanmax` in histograms; missing-X-value guards in smoothing and dual-axis-line rendering); fixed crashes on resizing 3D surface, heatmap, and contour plots; fixed a crash when `scatter()` was called with zero rows; fixed a crash when deleting a trace by name if two traces shared a name; fixed real-time updates on network graph panes; fixed a division-by-zero in quiver-plot normalization; fixed multi-environment window rendering aggregation; fixed a `list index out of range` crash in `/update`; fixed decoding of Plotly 6's binary array format; fixed `plotlyplot` ignoring configured width/height; fixed blank PNG exports for network graphs; fixed inconsistent export file naming; fixed cross-talk between environments; and fixed 3D scatter Z-axis updates.

<details>
<summary>Technical details</summary>

Related Pull Requests: #1389, #1483, #1364, #1489, #1502, #1385, #1515, #1550,
#1380, #1509, #1443, #1360, #1558, #1462, #1238, #1221, #1279, #1610, #1285,
#1156, #1171, #1599

Related Commits:
- `7427ddcc`, `4f45523f`, `5e9a28a3` — 3D scatter Z-axis handling fixes
</details>

## Environment & UI

Fixed a blank screen after deleting an unrelated environment; fixed the environment search input and console warnings in the environment tree; fixed keyboard input being swallowed in property fields; fixed pane titles moving during resize and panes collapsing below minimum size; fixed the resize arrow overlapping in comparison view; fixed a crash on Windows when `APPDATA` was missing; fixed one environment's name being a prefix of another causing overwrite on disk; and fixed several `NaN`/type-validation gaps.

<details>
<summary>Technical details</summary>

Related Pull Requests: #1544, #1547, #1540, #1265, #1359, #1387, #1386, #1149,
#1188, #1330, #1289, #1218, #1152, #1362, #1377, #1393, #1565, #1176, #1044,
#1554, #1549, #1394, #1303, #1177, #1460, #1305, #1229
</details>

## Image Handling

Negative float pixel values no longer wrap on the cast to `uint8` (a new `opts.normalize` option was added alongside this fix); grayscale/RGB/RGBA formats are handled explicitly, fixing a size-1-dimension collapse bug; float images slightly above 1.0 are clamped with a warning; and duplicate/overlapping captions in image and image-compare panes were fixed, along with an invalid CSS value and unnecessary re-renders on mouse movement.

<details>
<summary>Technical details</summary>

Related Pull Requests: #1315, #1325, #1368, #1306, #1482, #1481, #1376
</details>

## Embeddings

Fixed a WebGL context leak on unmount and clamped pane dimensions to avoid `GL_INVALID_VALUE` errors on resize; fixed lasso-selection drilldown losing focus.

<details>
<summary>Technical details</summary>

Related Pull Requests: #1519, #1471
</details>

---

# Security

- **Password hashing strengthened**: salted PBKDF2-HMAC-SHA256 (100,000 rounds) instead of unsalted SHA-256. Credentials are established at server startup, so no action is required on upgrade.
- **Closed an unauthenticated socket endpoint**: `WrappedSocketWrap.post` now requires authentication. **This can break unauthenticated clients** — see **Before You Upgrade**.
- **Consistent status codes**: unauthenticated requests return HTTP 401 instead of 400. **Clients branching on `400` need updating.**
- Authorization checks consolidated into a shared handler helper, and a message-validation fix closed a cross-environment data leak.

<details>
<summary>Technical details</summary>

Related Pull Requests: #1398, #1511, #1396, #1627, #1267
</details>

---

# Testing

The end-to-end suite is being migrated from Cypress to Playwright. The Playwright suite now covers panes, properties, text, uploads, modals, export, polling, and screenshot-based visual regression, and runs alongside the remaining Cypress specs (`npm test` still runs Cypress; `npm run test:pw` runs Playwright). Image and parallel-coordinates specs have not been ported yet. A Python unit test suite using `pytest` was also added, along with dedicated unit tests for the core plotting functions (bar, histogram, boxplot, surface, contour, and others).

<details>
<summary>Technical details</summary>

Related Pull Requests:
- #1541 (Playwright E2E infrastructure)
- #1551, #1557, #1582, #1594, #1600, #1607, #1615, #1622, #1597 (suite migrations)
- #1614 (fix a Plotly-smoothing crash surfaced during migration)
- #1543 (pytest suite + CI), #1538, #1657 (unit tests for plot functions)
- #1601 (consolidate test directories)
</details>

---

# Documentation

- Added a public documentation site built with **Docusaurus**, published via GitHub Pages.
- Added `AGENTS.md`, documenting contribution and AI-agent guidelines for the repository.
- Added a standardized **agent skills scaffold** and AI usage guidelines for first-time contributors.
- Synced the plotting API documentation and Python type stubs with the README, and added missing type stubs for newer visualization methods.
- Numerous README/CONTRIBUTING clarity and typo fixes, including a clarified environment-hierarchy explanation and a clarified image shape (C × H × W) requirement for `vis.image`.

<details>
<summary>Technical details</summary>

Related Pull Requests: #1467, #1246, #1301, #1477, #1494, #1418, #1445, #1099,
#1159, #1045, #1495, #982, #946, #947, #1124, #1514
</details>

---

# Breaking Changes & Upgrade Notes

Everything that needs action before upgrading is written out in full at the top: **Before You Upgrade**. Reference details:

| Change | Impact | Reference |
| --- | --- | --- |
| Minimum Python raised 3.10 → 3.12; `six` dropped | `pip install -U visdom` is a no-op on older Python | #1527 |
| `vis.win_hash()` and `/win_hash` removed | Calling code breaks; saved files unaffected | `b4ec4c84`, `23353464` |
| `openTSNE` now required (replaces `tsnecuda`) | Larger install; CUDA no longer required | #1500 |
| Busy port falls back to an OS-assigned free port | Server no longer fails on a taken port | #1351 |
| HTTP 401 replaces 400 for unauthenticated requests | Clients branching on `400` break | #1396 |
| `WrappedSocketWrap.post` requires authentication | Unauthenticated clients break | #1511 |

<details>
<summary>Technical details</summary>

Related Commits:
- `b4ec4c84` api-change: remove `/win_hash` endpoint
- `23353464` api-change: add window versioning (backwards compatible with old JSON files)
- `5d5de4d5` implement versioning for `_pendingPanes`
</details>

---

# Contributors

Thanks to everyone whose work is included in this release:

@ali0786mehdi, @ArnavBallinCode, @Ashishat404, @Bekka592, @da-h, @Debajeet-1411,
@hpdang, @Jayantparashar10, @Manik-Khajuria-5, @marcoag, @mariobehling, @norbusan,
@omkarsureshs, @rajnisht7, @Saksham-Sirohi, @sumedhaagh, @SxxAq, @TahoorBR,
@tayyabazahid147-art, @tonypzy, @vanshika-hgnis, @vedansh-5, @Vidhushaaa30, @vish-4-1

## New Contributors

Visdom changed hands after v0.2.4, and almost everyone above is here for the first
time. Making their first contribution to the project in this release:

@ali0786mehdi, @ArnavBallinCode, @Ashishat404, @Bekka592, @Debajeet-1411, @hpdang,
@Jayantparashar10, @Manik-Khajuria-5, @marcoag, @mariobehling, @norbusan,
@omkarsureshs, @rajnisht7, @Saksham-Sirohi, @sumedhaagh, @SxxAq, @TahoorBR,
@tayyabazahid147-art, @tonypzy, @vanshika-hgnis, @vedansh-5, @Vidhushaaa30, @vish-4-1

---

# Full Changelog

https://github.com/fossasia/visdom/compare/v0.2.4...v0.3.0
