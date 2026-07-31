# The Mainsail fork

Mainsail has no plugin API — only CSS theming — so a real panel needs a fork.
This document is the runbook for keeping that fork cheap to maintain.

Fork: **`Vylyne/mainsail`**, working branch **`ku/stable`**.

## Branch layout

Upstream's branches are **`master`** (stable) and **`develop`** (default, active).
There is no upstream `main` — the fork's `main` is our own name for the stable
mirror, so **rebase commands must say `upstream/master`**.

| Branch | Base | Role |
| --- | --- | --- |
| `ku/stable` | `upstream/master` | **Primary.** Where the code is written, what CI releases, what the printer installs. |
| `ku/develop` | `upstream/develop` | Cherry-pick target, only when preparing or refreshing an upstream PR. |

Upstream requires PRs target `develop`, which is the only reason the second
branch exists. As of 2026-07-30 `master` and `develop` were on an identical
`package.json` (2.18.2, Vue 2.7 / Vuetify 2 / Vuex 3, no Pinia), so cherry-picking
between them is near-free.

First-time setup:

```bash
git remote add upstream https://github.com/mainsail-crew/mainsail.git
git fetch upstream --tags
git switch -c ku/stable upstream/master
```

## The delta

**9 files added, 4 edited, zero deletions.** Keeping it that shape is the whole
strategy — don't grow the edited list.

Added:

```
.github/workflows/ku-ci.yml                                       (fork-only)
src/components/panels/Machine/FirmwareUpdaterPanel.vue
src/components/panels/Machine/FirmwareUpdaterPanel/FirmwareUpdaterPanelType.vue
src/components/panels/Machine/FirmwareUpdaterPanel/FirmwareUpdaterPanelUntracked.vue
src/store/server/fwUpdater/{index,actions,mutations,getters,types}.ts
```

Edited — this is the entire rebase surface:

| File | Change |
| --- | --- |
| `src/store/socket/actions.ts` | one `case 'notify_agent_event'` in the `onMessage` switch |
| `src/store/server/index.ts` | one import + one entry in `modules` |
| `src/pages/Machine.vue` | one import, one component registration, one element |
| `src/locales/en.json` | a `FirmwareUpdaterPanel` block under `Machine` |

Note `webSocketClient.ts` needs **no** change — it already forwards every
unmatched message to `socket/onMessage`.

## Commits

Upstream requires Conventional Commits. Keep the branch as **exactly 5
upstreamable commits, never squashed**, plus the fork-only CI commit on top so it
can be dropped for a PR:

1. `feat(store): add server/fwUpdater module` — add-only
2. `feat(panels): add FirmwareUpdater components` — add-only
3. `feat(socket): route notify_agent_event to fwUpdater`
4. `feat(store): register the fwUpdater module`
5. `feat(machine): mount FirmwareUpdaterPanel`
6. `chore(ci): fork-only workflow` ← drop when upstreaming

Commits 1–2 are new files and always apply clean. Only 3–5 can conflict, each a
1–3 line hunk in a known location.

## Rebasing

```bash
git fetch upstream --tags
git rebase upstream/master        # routine, on each upstream release
```

Then re-run the gates (below) and tag a release as
`v<upstream-version>-fw<n>` — e.g. `v2.18.2-fw1` — so provenance is obvious in
Mainsail's Update Manager panel.

To refresh an upstream PR: `git switch ku/develop && git cherry-pick <the 5>`.

**Check for API drift before starting any phase.** Upstream has a live
`feat/rework-init-process` branch, and a Vue 3 / Vuetify 3 / Pinia migration would
invalidate every added file:

```bash
git diff upstream/master..upstream/develop -- package.json
git diff upstream/master..upstream/develop -- \
    src/store/socket/actions.ts src/store/server/index.ts src/pages/Machine.vue
```

## Gates

Locally, before every push:

```bash
npm run format      # prettier . --write
npm run lint:fix    # eslint src --fix
npm run test:unit   # vitest run
npm run build       # vite build && build.zip
```

CI uses the **non-mutating** variants — `format:check` and `lint` — because
`format`/`lint:fix` would let CI pass by rewriting the code it is policing.

`npm run build` already emits `dist/mainsail.zip` via upstream's own `build.zip`
script, so releases just attach that artifact. Run `npm run i18n-extract` after
touching `en.json` to confirm every `$t()` key resolves.

## Installing on the printer

Point Mainsail's own Update Manager at the fork — one line in `moonraker.conf`:

```ini
[update_manager mainsail]
type: web
channel: stable
repo: Vylyne/mainsail        # was mainsail-crew/mainsail
path: ~/mainsail
```

You then update the UI from inside the UI, and revert to stock by pointing
`repo:` back and updating again.

## Upstream symbols depended on

If a rebase fails, these are the four things to check first:

- the `switch (payload.method)` in `store/socket/actions.ts::onMessage`
- the `modules: { … }` block in `store/server/index.ts`
- the right-hand column layout in `pages/Machine.vue`
- `components/ui/Panel.vue`'s props (`title`, `icon`, `cardClass`, `collapsible`)

Plus the class-component style itself: `@Component`, `Mixins(BaseMixin)`,
`Vue.$socket.emit`, Vuetify 2 components.

## Is the fork permanent?

Probably not. Upstream absorbs third-party integrations as first-class panels —
`MmuPanel` (Happy Hare), `AfcPanel`, and `SpoolmanPanel` are all upstream now —
and the panel is deliberately built to be upstreamable: `v-if` gated on the agent
being present, no hard dependency in any init path, standard `Panel`/`BaseMixin`/
`$socket.emit`, English-only locale additions, and placed on `Machine.vue` where
upstream would want it.

⚠️ But upstream runs a **vouch-based review system** that can auto-close a new
contributor's PR. Open an issue describing the agent and panel *before* writing
the PR, and treat a merge as upside rather than the plan. The fork has to stand on
its own — which is what the 4-file edit budget buys.
