# Workflow Analysis — dxf-converter web UI (pre-v2 UI)
**Date:** 2026-10-02
**Verdict:** FAILS BASELINE

## Summary

The anonymous editor does its core job well: upload, detect, clean up, export works end to end
for both scans and badge photos, and editing (select, box-select, delete, move/insert points,
undo, layer toggles, eyedropper) is solid. It fails the baseline on the edges: **Re-detect
silently throws away hand edits** with no confirm and no undo, and **any server restart traps the
user's work** — export fails with "upload not found" although every path is in the browser and
export never needs the image. Two of today's redeploys would have done exactly that to anyone
mid-edit. Errors surface only as toasts, empty and loading states are thin, and several backend
settings have no control.

Everything v2 adds — accounts, library, versions, compare, admin — exists only as API (phases
1-2). Tracing those workflows against the plan turned up gaps in the **plan itself**, not just
unbuilt UI: no way for a user to change their own password or delete their account, no admin
password reset or promote/demote outside the CLI, no handling for a login expiring mid-edit, no
search in a library that grows forever, and compare overlays that silently disagree when two
versions were scaled differently. These are folded into phase 3 below.

Evidence: traced live in a browser against the current code (local server, no database) —
upload, bad-file error, detect, Del on a path, Re-detect after the edit, export, and export /
Re-detect after deleting the upload's temp dir (a simulated restart). Photo mode with eyedropper,
layer hide and export were traced the same way on 2026-10-01. Account, library, version and
admin workflows were traced through the phase 1-2 API tests and an end-to-end HTTP run of the
built image; they have no UI to trace.

## Baseline Checklist Results

From the `ui-development` non-negotiables, against the current UI.

| Item | Result | Note |
|---|---|---|
| Searchable dropdowns for every foreign-key field | N/A | No foreign-key fields today (inks are picked on the image, layers by checkbox). Becomes real in v2: the compare picker and version strip. |
| Inline validation errors | **Fail** | Every error is a transient toast: unsupported file type, conversion failure, "upload not found". Nothing appears where it happened. |
| Immediate list refresh after mutation | Pass | Path list, layer counts and stats update on delete, undo, redo and re-detect. |
| Confirmation before destructive actions | **Fail** | Re-detect discards all hand edits on one click; only a passive "Re-detect discards your edits" line shows, and undo history is cleared. (Path delete is undoable, so it passes.) |
| Empty states | **Fail** | Path list is a blank box when there are no paths; no "nothing detected — try lowering Min length" guidance. |
| Loading states | **Fail** | During a 3-5 s photo detection only the status bar text changes; the canvas and panels sit unchanged or blank. |
| All backend fields exposed | **Fail** | `threshold_value` (used by Fixed threshold), `upsample`, and the export `layer` name are accepted by the API with no control. |
| Dark mode by default | Pass | Dark theme throughout. |

## Entity + Relationship Coverage

| Entity | Fields | In UI today | Missing from UI |
|---|---|---|---|
| Upload (anonymous) | id, file, suggested_source | create (drop/choose), discard (New scan) | — |
| Path | points, closed, layer, color, kind | read, delete, move/insert/delete point | — |
| Detection settings | 20 fields (`Settings`) | 17 | `threshold_value`, `upsample`; `scale` only through the export's separate "Scale factor" |
| User | username, is_admin, created_at, password | none | everything (sign-up, login, logout, me) |
| Auth token | expires_at | none | expiry handling |
| Scan | name, image, thumb, size, versions, created/updated | none | all CRUD |
| Version | number, label, note, parent, settings, view, paths, stats | none | all CRUD, compare |
| Admin view of users | counts, delete content, delete user | none | all |

| Relationship | Established | Browsable | Removable | Needs a raw ID? |
|---|---|---|---|---|
| User → Scans | API (save) | API list | API delete | no |
| Scan → Versions | API (save version) | API detail | API delete (not the last) | no |
| Version → parent version | API (`parent_number`) | API detail | n/a | **would**, if the UI asked the user for it — must be set automatically from what was loaded |
| Version ↔ Version (compare) | — | — | — | plan says "pick a version"; must be a labelled list, never a typed number |

Navigation inventory: today one surface (drop zone → editor) and one way back ("New scan"). v2
adds library, editor, admin. No duplicate paths exist yet; the plan must keep "open a scan" to one
place (the library) rather than also adding a scan picker inside the editor.

Lists: path list (sorted shortest-first, no filter — fine, layer toggles filter it); **library
(unbounded, no search planned)**; version list (grows per scan); admin user list (small).

## Workflow Coverage by Persona

### Anonymous converter (one-off job, no account)

| Workflow | Status | Notes |
|---|---|---|
| Convert a pen scan to a to-scale DXF | ✅ | Traced: upload → detect → export. |
| Convert a badge photo: pick inks, fit size, square lettering | ✅ | Traced 2026-10-01 incl. eyedropper adding to auto inks. |
| Remove dirt and stray paths | ✅ | Click/shift-drag select, Del, undo/redo. |
| Hide a layer and export without it | ✅ | Hidden layers excluded from the DXF. |
| Tweak a setting and re-detect without losing edits | ❌ | Re-detect wipes edits with no confirm and no undo. |
| Finish and export after a server restart/redeploy | ❌ | Export and Re-detect both fail: "upload not found — re-upload the scan". Work is unrecoverable. |
| Set the real-world scale | ⚠️ | Two unrelated controls do it: "Largest dimension" (Detection) and "Scale factor" (Export), and they multiply. |
| Name the exported file | ⚠️ | Defaults to "profile" though the upload's name ("seeburg.webp") is known. |

### Account holder (comes back to their work)

| Workflow | Status | Notes |
|---|---|---|
| Sign up / log in / log out | ❌ | API done; no UI. |
| Save the current work as a new scan | ❌ | API done (`POST /api/scans` with the upload id); no UI. Plan gap: an anonymous user who presses Save must be able to log in or sign up **without losing the editor state**. |
| Reopen a scan later at its latest version | ❌ | API done; no UI. Needs the image via an authenticated fetch → blob URL (underlay + eyedropper). |
| Save edits as a new version | ❌ | API done. Parent must be set automatically from the loaded version. |
| Re-detect / export a saved scan | ❌ | API done (`/api/scans/{id}/convert`, `/export`); the editor must route to them when a saved scan is open. |
| Compare two versions | ❌ | API supports loading any version. Plan gap: if the versions used different `fit_mm`/`scale`, overlaying their millimetre geometry misleads; the panel must say so. |
| Find a scan among many | ❌ | No search or sort in the plan for an unbounded list. |
| Rename a scan; label/note a version; delete a version or scan | ❌ | API done; no UI. |
| Change own password; delete own account | ❌ | **No API.** Only the admin CLI can change a password. |
| Keep working when the login expires mid-edit | ❌ | **Not in the plan.** A 401 on save must keep the editor intact, ask for the password, then retry. |

### Admin

| Workflow | Status | Notes |
|---|---|---|
| Create the first admin | ✅ | `app.admin init-admin`, traced in the built image. |
| See users and how much each has saved | ❌ | API done; no UI. |
| Delete a user's content / delete a user | ❌ | API done; no UI; needs a confirm naming the user and counts. |
| Reset a user's forgotten password | ❌ | CLI only (`set-password`); no API or UI. |
| Make a user an admin / revoke admin | ❌ | Nowhere — only `add-user --admin` at creation. |
| Turn sign-up off | ⚠️ | `ALLOW_SIGNUP` env var, needs a redeploy. Acceptable for a home tool. |

## Anti-Pattern Audit Results

| Anti-pattern | Where | Severity |
|---|---|---|
| Destructive action without confirm | Detection → Re-detect button after hand edits (also clears undo) | P1 |
| No error feedback where it happened | Toast-only errors for upload, convert, export | P1 |
| No empty state | Paths panel with zero paths | P2 |
| No loading state | Canvas/panels during detection | P2 |
| Partial form | Detection lacks `threshold_value`, `upsample`; Export lacks layer name | P2 |
| Duplicate controls for one concept | Scale: "Largest dimension" vs export "Scale factor" | P1 |
| System knows, user re-types | Export filename defaults to "profile" | P2 |
| Inconsistent numbers | Status bar "231 points" vs stats "Points 1308" (circles counted as sampled points in one, not the other) | P2 |
| State lost across a boundary | Server restart loses the upload; export needlessly depends on it | P0 |
| Unbounded list without search (planned) | Library | P1 |

Keyboard: Escape cancels picking and clears selection; Del/Backspace, Ctrl+Z/Ctrl+Shift+Z work.
Enter-to-submit has no form to apply to yet — the v2 login form must have it.

## Prioritized Gap List

### P0 — Workflow Blockers
1. **Export depends on the upload's temp files.** After any restart the user's finished geometry
   can't be exported. Export only needs the posted paths.
2. **No account UI at all** — sign-up, login, library, save, load, versions, admin are API-only.
   (This is phase 3's purpose; listed so the report is honest about what users can do today.)

### P1 — High Friction
1. Re-detect discards edits without confirm and wipes undo.
2. Errors are toasts only; upload/convert/export failures vanish after a few seconds.
3. Two scale controls that multiply.
4. Login expiry mid-edit isn't designed for (plan gap).
5. Users can't change their own password or delete their account; admins can't reset passwords
   or change roles without the CLI (plan + API gap).
6. Library has no search/sort (plan gap).
7. Compare doesn't account for versions scaled differently (plan gap).
8. Save while anonymous must not lose the editor state across login/sign-up (plan gap).

### P2 — Polish
1. Empty state for the paths panel.
2. Loading indicator on the canvas during detection.
3. Expose `threshold_value` (when Fixed), `upsample` (Advanced), export layer name.
4. Default export filename from the upload/scan name.
5. One definition of "points" in both counters.
6. `ALLOW_SIGNUP` toggle in the admin panel instead of an env var.

## Recommendations

**P0.1 Stateless export.** Add `POST /api/export` (no id) that runs `editing.write_export` on the
posted paths into a fresh temp dir; point the editor's Download button at it for anonymous work
and at `/api/scans/{id}/export` for saved scans. Keep `/api/export/{upload_id}` as an alias. When
Re-detect hits "upload not found", say "The server restarted and dropped the image — your edits
are still here and can be exported; re-upload the image to detect again."

**P0.2 Phase 3 UI**, built one workflow at a time per `ui-development`, in this order: login/sign-up
→ save (incl. from anonymous) → library + reopen → versions (save, strip, load, label) → compare →
admin panel. The rest of this list is folded into those workflows.

**P1.1** Re-detect: when `S.dirty`, `confirm("Re-detect discards N hand edits. Continue?")`; and
push the pre-detect paths onto the undo stack so it can be undone.

**P1.2** Inline errors: a persistent error strip at the top of the panel the action came from
(upload errors on the drop zone, convert errors under Re-detect, export errors under Download),
cleared on the next success. Keep the toast for success only.

**P1.3** One scale control: keep "Largest dimension (mm)" in Detection; replace the export "Scale
factor" with a read-only "Exports at W × H mm" line plus a "Measured size" input that sets
`fit_mm` and re-detects — so there is one place to set scale.

**P1.4** Expiry: the API client wraps every authed call; on 401 it opens the login dialog over the
editor (state untouched), and after login retries the original request once. The token and the
username persist in `localStorage`; the editor state is never tied to it.

**P1.5** Add `POST /api/auth/password` (current + new; signs out other tokens) and
`DELETE /api/auth/me` (password-confirmed; uses `content.delete_user`, refuses the last admin).
Admin: `POST /api/admin/users/{id}/password` (returns a generated password once, like
`init-admin`) and `PATCH /api/admin/users/{id}` `{is_admin}` (refuses demoting the last admin).
UI: an "Account" menu under the user name; reset/role buttons on admin user rows.

**P1.6** Library: a name filter box (client-side over `GET /api/scans`) and sort by last updated /
name / created. Cards show thumbnail, name, version count, latest label, updated date.

**P1.7** Compare: overlay in page millimetres as stored, and show a warning chip when the two
versions' `settings.fit_mm` or `scale` differ ("v2 was scaled to 76 mm, v3 to 80 mm — geometry
won't line up"). Stats table shows both versions' paths, points, circles and extents with deltas.

**P1.8** Save while anonymous: Save opens the login/sign-up dialog; on success it posts
`/api/scans` with the still-valid `upload_id` and current paths. If the upload is gone (restart),
say so and offer re-upload — the paths are still in the editor.

**P2s** as listed; each is a few lines in `app.js`/`index.html`.
