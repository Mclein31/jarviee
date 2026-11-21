---
name: Jarviee
description: Dark glass over the owner's own desktop. The wallpaper glows through frosted panes with specular rims, and every hue keeps one job from tile to wire to pill.
colors:
  wall-fallback: "#121317"
  wall-fallback-lit: "#2b2d35"
  tint: "rgb(14, 15, 19)"
  panel: "rgba(255, 255, 255, .065)"
  panel-2: "rgba(255, 255, 255, .09)"
  panel-3: "rgba(255, 255, 255, .14)"
  node-bg: "rgba(34, 36, 42, .7)"
  line: "rgba(255, 255, 255, .1)"
  line-2: "rgba(255, 255, 255, .14)"
  line-3: "rgba(255, 255, 255, .24)"
  rule: "rgba(255, 255, 255, .08)"
  well: "rgba(0, 0, 0, .34)"
  text: "#f5f6f8"
  text-2: "#d6d9df"
  muted: "#adb2bb"
  faint: "#949aa3"
  arc: "#5b8def"
  arc-hi: "#7ea6f5"
  arc-ink: "#9dbbff"
  on-arc: "#050b18"
  ember: "#ffb547"
  ember-hi: "#ffd596"
  ember-ink: "#ffc56b"
  on-ember: "#1f1300"
  green: "#3fd18b"
  green-dim: "#258a5c"
  red: "#ff6b63"
  violet: "#a594ff"
  grey: "#7f858e"
  grey-2: "#4a4e56"
  claude: "#d97757"
  claude-ink: "#f2a483"
  agy: "#2a46c4"
  agy-ink: "#93abff"
  tier-basic: "#4cc9e0"
  tier-light: "#b9e36c"
  tier-complex: "#cf95ff"
  tier-heavy: "#ff7fa8"
  primary: "#f2f3f5"
  primary-hi: "#ffffff"
  on-primary: "#0d0e10"
  mark-crimson: "#8e1426"
  mark-oxblood: "#3d0b15"
  mark-black: "#0c090b"
  opaque-pane: "#24262c"
  opaque-side: "#1b1c21"
  opaque-float: "#2a2c33"
typography:
  display:
    fontFamily: "ui-monospace, \"SF Mono\", Menlo, Consolas, monospace"
    fontSize: "46px"
    fontWeight: 300
    lineHeight: 1
    letterSpacing: "-0.03em"
    fontFeature: "\"tnum\""
  headline:
    fontFamily: "-apple-system, BlinkMacSystemFont, \"SF Pro Display\", \"Segoe UI\", system-ui, sans-serif"
    fontSize: "20px"
    fontWeight: 650
    lineHeight: 1.2
    letterSpacing: "-0.01em"
  title:
    fontFamily: "-apple-system, BlinkMacSystemFont, \"SF Pro Display\", \"Segoe UI\", system-ui, sans-serif"
    fontSize: "15px"
    fontWeight: 600
    lineHeight: 1.2
    letterSpacing: "-0.005em"
  body:
    fontFamily: "-apple-system, BlinkMacSystemFont, \"SF Pro Text\", \"Segoe UI\", system-ui, sans-serif"
    fontSize: "13px"
    fontWeight: 400
    lineHeight: 1.5
    fontFeature: "\"tnum\""
  label:
    fontFamily: "-apple-system, BlinkMacSystemFont, \"SF Pro Text\", \"Segoe UI\", system-ui, sans-serif"
    fontSize: "12px"
    fontWeight: 600
    lineHeight: 1.3
  mono:
    fontFamily: "ui-monospace, \"SF Mono\", Menlo, Consolas, monospace"
    fontSize: "11px"
    fontWeight: 500
    lineHeight: 1.3
    fontFeature: "\"tnum\""
rounded:
  r-check: "5px"
  r-xs: "8px"
  r-sm: "12px"
  r-node: "12px"
  r: "20px"
  mark: "23%"
  pill: "999px"
spacing:
  gap: "16px"
  pad: "22px"
  panel-pad: "16px"
  side-w: "240px"
  top-h: "60px"
components:
  button-primary:
    backgroundColor: "{colors.primary}"
    textColor: "{colors.on-primary}"
    rounded: "{rounded.pill}"
    height: "32px"
    padding: "0 16px"
  button-primary-hover:
    backgroundColor: "{colors.primary-hi}"
    textColor: "{colors.on-primary}"
  button:
    backgroundColor: "rgba(255, 255, 255, .08)"
    textColor: "{colors.text}"
    rounded: "{rounded.r-sm}"
    height: "32px"
    padding: "0 13px"
  button-hover:
    backgroundColor: "rgba(255, 255, 255, .14)"
  button-ghost:
    textColor: "{colors.muted}"
    rounded: "{rounded.r-sm}"
    height: "32px"
  button-round:
    backgroundColor: "rgba(255, 255, 255, .08)"
    textColor: "{colors.text}"
    rounded: "{rounded.pill}"
    size: "32px"
  input:
    backgroundColor: "rgba(0, 0, 0, .18)"
    textColor: "{colors.text}"
    rounded: "{rounded.r-sm}"
    height: "32px"
    padding: "0 10px"
  checkbox:
    backgroundColor: "rgba(0, 0, 0, .22)"
    rounded: "{rounded.r-check}"
    size: "16px"
  checkbox-checked:
    backgroundColor: "{colors.arc}"
    textColor: "{colors.primary-hi}"
    rounded: "{rounded.r-check}"
  nav-item:
    textColor: "{colors.muted}"
    rounded: "{rounded.r-sm}"
    height: "36px"
    padding: "0 12px"
  nav-item-active:
    backgroundColor: "rgba(255, 255, 255, .2)"
    textColor: "{colors.text}"
    rounded: "{rounded.r-sm}"
  nav-count-need:
    backgroundColor: "{colors.ember}"
    textColor: "{colors.on-ember}"
    rounded: "{rounded.pill}"
    typography: "{typography.mono}"
    height: "20px"
  pane:
    backgroundColor: "{colors.panel}"
    rounded: "{rounded.r}"
    padding: "16px"
  kpi-tile:
    backgroundColor: "{colors.panel}"
    textColor: "{colors.text}"
    rounded: "{rounded.r}"
    padding: "16px 16px 14px"
  kpi-figure:
    textColor: "{colors.text}"
    typography: "{typography.display}"
  pill:
    textColor: "{colors.text-2}"
    rounded: "{rounded.pill}"
    height: "22px"
    padding: "0 9px"
  chip:
    backgroundColor: "rgba(255, 255, 255, .07)"
    textColor: "{colors.text-2}"
    rounded: "{rounded.r-xs}"
    height: "24px"
    padding: "0 9px"
  status-label:
    textColor: "{colors.muted}"
    typography: "{typography.label}"
  graph-node:
    backgroundColor: "{colors.node-bg}"
    textColor: "{colors.text}"
    rounded: "{rounded.r-node}"
    padding: "13px 14px"
  tool-badge:
    rounded: "{rounded.pill}"
    size: "28px"
  meter:
    backgroundColor: "rgba(0, 0, 0, .3)"
    rounded: "6px"
    height: "6px"
  mark:
    backgroundColor: "{colors.mark-crimson}"
    textColor: "{colors.primary-hi}"
    rounded: "{rounded.mark}"
    size: "34px"
---

# Design System: Jarviee

## Overview

**Creative North Star: "Dark Glass Over Your Own Desktop"**

Jarviee is a pane of dark glass laid over the owner's own desktop picture. The wallpaper glows through as colour, the panes frost it a second time and catch light at their rims, and every number still keeps one hue. The window is built from three planes, back to front: the wall (the macOS desktop picture, softly blurred and veiled), the panes (sidebar, top bar, panels, tiles, the command bar and every popover), and the content on them (hairlines, white-at-low-alpha controls, the white primary pill). It replaced the flat graphite operations desk, which had replaced a neon JARVIS HUD; it equally refuses the generic grey-blur "glassmorphism" card. Here the glass is measured: the veil and pane tint are computed from the photo so any desktop, pastel or night, yields AA text and colour that glows rather than fills.

The desk stays dense and quiet. Headings are sentence case in the display cut of the operating system face, words are set in its text cut, and every figure, including the big KPI values, is SF Mono with tabular numerals. Light comes only from the material: the specular rim and the top-left sheen. Nothing glows at rest. The white primary pill is the brightest object on screen; the current page is a brighter glass tile. Amber is reserved for the owner.

The interface renders at a user interface scale: the body is zoomed by `--z` (0.85 by default, Settings → Interface scale), and full-height parts use `--vh` / `--vw`, which divide the viewport by that scale. Every size here is the authored size before the scale. The product is dark only (`color-scheme: dark`), uses system font stacks only, and makes no external requests: the wallpaper is served by the hub itself (`/api/wallpaper`), no hosted fonts, no CDN.

**Key Characteristics:**
- Three planes: a blurred, veiled wallpaper wall; frosted panes with a 1px specular rim; translucent content.
- Adaptive glass: veil and pane tint are measured from the wallpaper so text keeps AA on any desktop.
- A full palette where each hue has exactly one job and keeps it everywhere.
- Status is always shape + word + hue; colour never carries meaning alone.
- Sentence-case display headings, system text face for words, system mono for every figure.
- The white primary pill is the brightest object on screen; nothing glows at rest.
- One mark everywhere: a white italic brush "j" on a crimson glass tile.

## Colors

A full, role-locked palette carried on translucent white glass over the owner's wallpaper: blue is the system, amber is the owner, and every other hue has one fixed job.

### Primary
- **System Blue** (arc): running work, the system, selection, focus rings, links, the caret, checked checkboxes and toggles. Its ink step (arc-ink) colours text on blue washes, the current nav icon, link-like buttons and live wire dashes; arc-hi is the lifted step. The token keeps its old name `--arc`; read it as "system blue". `--teal` aliases it, so "running" is drawn in the system colour.
- **Primary White** (primary): the primary action only (New task, Launch), as a full pill with on-primary ink; primary-hi on hover. It is deliberately the brightest object on screen.

### Secondary
- **Owner Amber** (ember): needs you, and warnings. The needs-you KPI tile's rim, top wash and figure when hot, the needs-you nav count, the needs-you wire, waiting job cards, the diamond status mark, banners. ember-ink for amber text on its 12% wash; ember-hi for the moving dash on an amber wire. The token keeps its old name `--ember`.

### Tertiary
- **Done Green** (green): finished work, success figures; green-dim for quieter fills.
- **Failure Red** (red): failed, out, cut and used-up states only, including the red hatch across a used-up quota track.
- **Fallback Violet** (violet): fallback routing, and the quota KPI tile's hue.
- **Tier hues**: tier-basic (teal), tier-light (lime), tier-complex (orchid), tier-heavy (rose). Each load tier owns one, kept clear of the state colours, from its pill through its model row, the lit effort chip and the wire to the job card's edge.
- **Tool hues**: claude (Claude orange) with claude-ink, and agy (agy dark blue) with agy-ink. Lane load bars, quota meters, running-session pips and the Running tile's slot gauge take the tool's hue; Claude token mixes step through the Claude hue only.
- **Mark Crimson** (mark-crimson → mark-oxblood → mark-black): the brand mark's tile gradient only. It is not a UI colour and appears nowhere else.

### Neutral
- **The wall** (wall-fallback, wall-fallback-lit): the ground under the photo while it loads, and the radial ground of the Plain dark background. The live wall is the photo itself.
- **The veil** (tint): the smoky tint laid over the photo and under every pane, at the adaptive alphas `--veil` and `--pane-a`.
- **Glass steps** (panel, panel-2, panel-3): white at 6.5%, 9% and 14% for pane fills, raised controls and hover or selected states.
- **Node glass** (node-bg): denser smoky glass for graph nodes and job cards, so a wire never shows through.
- **Hairlines** (line, line-2, line-3, rule): white at 10%, 14%, 24% and 8%: borders, control borders, emphasis borders, in-pane dividers.
- **Wells** (well): dark glass at 34% for logs, code and streams; tracks and inputs use black at 18-30%.
- **Text steps** (text, text-2, muted, faint): primary text; secondary text; labels and quiet meta; placeholders and the faintest meta.
- **Greys** (grey, grey-2): idle, queued, stale and stopped states.
- **Opaque fallbacks** (opaque-pane, opaque-side, opaque-float): the panes, sidebar and popovers when the system asks for reduced transparency.

Washes are derived, not separate colours: hue at 12-15% alpha for pill and chip backgrounds (`--arc-bg`, `--ember-bg`, `--green-bg`, `--red-bg`, `--violet-bg`, `--claude-bg`, `--agy-bg`, tier hues via `color-mix(... 14-20%, transparent)`), and hue at 50% alpha for line accents (`--arc-line`, `--ember-line`).

### Named Rules
**The One Job Rule.** Every hue has one meaning and keeps it from tile to wire to pill. Blue is running and the system, amber is needs you, green done, red failed or out, violet fallback, the four tier hues are the four tiers, orange and dark blue are the two tools. A new surface may not borrow a hue for decoration, and the wallpaper's own colour never stands in for one.

**The Amber Is Yours Rule.** Amber marks only what needs the owner. Nothing else on screen may be amber, including emphasis, critical paths or highlights.

**The Red Means Gone Rule.** Meters take their tool's hue (or the tile's hue) and turn red only when the allowance is used up; then the whole track becomes a red hatch and the figure turns red, never an empty-looking bar. Low-but-not-empty is not red.

**The Glow Not Fill Rule.** Colour behind glass is a glow. A vivid wallpaper raises the veil and pane tint (by its measured saturation) until it can no longer compete with the status hues.

## Typography

**Display Font:** SF Pro Display (via `-apple-system, BlinkMacSystemFont`, with Segoe UI / system-ui fallback)
**Body Font:** SF Pro Text (same system stack)
**Label/Mono Font:** SF Mono (`ui-monospace`, with Menlo / Consolas fallback)

**Character:** The operating system's own face in its two optical cuts, with mono reserved for figures. On glass it reads like a native macOS window: exact, unadorned, quiet.

### Hierarchy
- **Display** (mono, 300, 46px, line-height 1, -0.05em): the KPI figure, large and light as the tile's focal object; 36px under 1100px. Big numbers are mono, never the display sans.
- **Headline** (SF Pro Display, 650, 20px, 1.2, -0.01em): the page title in the top bar. The wordmark is the same cut at 18px.
- **Title** (SF Pro Display, 600, 15px, 1.2): pane headings, sentence case. Empty-state headings use 600 at 16px.
- **Body** (SF Pro Text, 400, 13px, 1.5, tabular numerals on): running text, table cells, nav labels (500). The command box sets at 14px.
- **Label** (SF Pro Text, 600, 12px, 1.3): section heads in muted, nav group heads in faint, status words. Pills and chips set at 11.5px, 550-600.
- **Mono** (SF Mono, 500, 11px): counts, timestamps, IDs, durations and every figure inside a row. Count badges set 600.

Sizes step 11 / 12 / 13 / 14 / 18 / 22 / 32px (`--fs-xs` to `--fs-kpi`). Tracking is zero by default (`--track: 0`).

### Named Rules
**The Mono Figure Rule.** Every figure is mono with tabular numerals, from an 11px count badge to the 46px KPI value.

**The Sentence Case Rule.** Headings and labels are sentence case in plain weight. No tracked capitals, no small-caps labels above headings.

## Layout

A full-size app, not a floating window: the panes fill the browser window edge to edge, with the wallpaper showing in every gap and through every pane. A 240px labelled sidebar pane runs down the left edge (the mark and wordmark, grouped routes with 36px items and count pills, a collapsible system row and Settings at the foot); the main column carries a sticky 60px glass top bar (page title, crumb, 240px search, live pill, the white New task pill). Pages stack full-width panes with a 16px gap and 22px page padding; panes pad 16px and their heads are at least 54px with a rule beneath.

The Command Center leads with the command bar, then the KPI strip (three tiles plus a wider quota tile at 1.35fr; two columns under 1100px), the Routing graph full width, and Active agents full width below, with "For you" (waiting on you and today side by side) under it. The Routing detail opens only when a node is clicked; closed, the graph takes the whole row. Analytics runs six KPI tiles (6, then 3 + 3 at 1360px, then 2 + 2 + 2 at 640px).

Pages fill the window rather than leaving blank glass. Rows of panes end on one line: paired panes share a height, and Settings is two balanced columns (above 1100px) whose last panes grow so both end level. Workflows' graph takes the free height of the window with its diagnostics and timeline accordions at the foot. The Agents detail fills its pane, with the live steps scrolling inside it and the Details row always visible; the Agents list shows the last 24 hours of finished runs under the live rows. An empty Task Queue fills the page with a centred empty state.

The routing graph is sized by its own width through container queries, not the window, so browser zoom and the interface scale cannot push it into the wrong layout: under 1040px of graph width the jobs drop under the graph, and under 700px it stacks and folds the effort column into the model rows. The sidebar collapses to an icon rail at 1180px and to an off-canvas drawer at 760px. Wide screens (1800px and up) add a library column to Workflows and a side detail pane to the Command Center.

## Elevation & Depth

Depth is material, not tone. The wall sits furthest back: the photo blurred 30px, slightly desaturated and scaled up so its edges never show, under a veil (a radial plus linear smoky tint at the adaptive `--veil`, a 125° white sheen from the top left, and a fine SVG film grain so the frost reads as a material). Panes float above it: they blur 26px, saturate (`--pane-sat`, 135–190%: pushed for a grey photo, held back for a vivid one) and brighten what is behind them, take a white gradient lit from the top left over the smoky tint at `--pane-a`, carry a 1px specular rim drawn with a mask (a crisp catch-light at the top-left corner that falls off within the first eighth, catching again bottom-right), and sit on a soft lift shadow. The focal pane floats a step higher (`--lift-hi`, a brighter rim and a lighter tint): Routing always, Needs you while something waits. Nodes and job cards inside panes are nested glass: a top-left-lit gradient over `--node-bg`, dense enough that no wire shows through. The sidebar is one tall pane, darker than the page panes, with its rim only on the right edge; the top bar is a clearer strip. Popovers (dialogs, menus, the toast) are denser glass at 62% tint with the float shadow on top of the lift.

Adaptive glass is the signature. Before the photo is shown it is drawn onto a 32 × 20 canvas; its 85th-percentile luminance and mean saturation set `--veil` (from 0.42 + saturation × 0.2 up to 0.86) and `--pane-a` (from 0.22 + saturation × 0.1 up to 0.62), so a pale or vivid desktop gets a heavier veil and a night photo a lighter one, and every text step keeps AA. It is re-measured every two minutes, so a new desktop picture arrives without a reload. Settings → Display → Background offers Follow my desktop (the dark appearance of a dynamic wallpaper), Big Sur Road, Catalina Sunset, Catalina Clouds, Quiet Coast and Plain dark (no photo: the same panes on a quiet radial ground). With `prefers-reduced-transparency`, backdrop filters switch off and panes become opaque (opaque-pane, opaque-side, opaque-float).

### Shadow Vocabulary
- **Lift** (`inset 0 1px 0 rgba(255,255,255,.16), 0 22px 44px -28px rgba(0,0,0,.75), 0 2px 6px -2px rgba(0,0,0,.28)`): every pane, tile, the command bar and popovers. Hover adds `inset 0 0 0 1px rgba(255,255,255,.06)`.
- **Float** (`0 24px 48px -20px rgba(0,0,0,.8), 0 4px 12px -4px rgba(0,0,0,.5)`): added under dialogs, context menus and the toast.
- **Lit nav tile** (`inset 0 1px 0 rgba(255,255,255,.22), inset 0 0 0 1px rgba(255,255,255,.1), 0 8px 18px -12px rgba(0,0,0,.7)`): the current page in the sidebar.
- **Primary lift** (`inset 0 -1px 0 rgba(0,0,0,.08), 0 8px 22px -12px rgba(255,255,255,.55)`): the white primary pill only.
- **Control highlight** (`inset 0 1px 0 rgba(255,255,255,.1)`): default buttons and the pressed segment.
- **Focus halo** (`0 0 0 3px` system-blue wash, 4px on the command bar): focused inputs and the selected graph node.

### Named Rules
**The Rim And Sheen Rule.** Nothing glows at rest. Light comes only from the material: the specular rim, the top-left sheen and the inset top highlight. Coloured halos appear only as a response to focus or selection, in system blue; the one standing coloured rim is the hot Needs you tile's amber rim.

**The Measured Veil Rule.** Never hard-code a veil, pane alpha or saturation for a surface; panes read `--veil`, `--pane-a`, `--pane-sat` and `--wall-sat` so the adaptive measurement keeps contrast and colour on any wallpaper (glass.js: veil floor .36 + sat × .28; wall saturation 1.6 − sat × 1.4 within .85–1.45; pane saturation 190 − sat × 110 within 135–190%).

## Shapes

Generously rounded, in the soft geometry of visionOS: 20px for panes, tiles, dialogs, the command bar and Workflows graph nodes; 12px for buttons, inputs, nav items, routing graph nodes, job cards and small cards; 8px for chips and effort chips; 5px for the glass checkbox; full round for the primary pill, pills, counts, segmented controls, toggles, round icon buttons and tool badges. The brand mark is a rounded square at a 23% corner. Borders are 1px hairlines or the masked specular rim; dashed borders mean empty or out (an empty jobs slot, an unavailable lane, "show more"). Status marks are tiny geometric shapes: a dot, a diamond, a square, a ring, a triangle.

## Components

### Buttons
Translucent glass controls with one bright exception.
- **Shape:** 32px tall, 12px radius, 13px side padding.
- **Primary:** a full pill, white fill with graphite ink at weight 650 and the primary lift; hover goes pure white. Disabled primaries drop to white at 12% with muted text.
- **Default:** white at 8% with a white-at-12% border and an inset top highlight; hover white at 14% with a 20% border.
- **Ghost:** transparent, muted text, no highlight.
- **Danger:** red text; hover a red wash.
- **Round / icon:** 32px (28px small) circles for "open" links.
- **Link-like:** inline buttons in running text are arc-ink and underlined.

### Chips and Pills
- **Pills** (22px, full round, 11.5px / 600): neutral by default; tier pills are the tier hue on a 14% wash of itself.
- **Chips** (24px, 8px radius): white at 7%; state chips take a hue wash, hue border and hue ink.
- **Counts** (20px, full round, mono 600): white at 12% (20% in the current nav item); the needs-you count is solid amber with dark ink.

### Cards / Containers (panes)
- **Corner Style:** 20px.
- **Background:** the lit white gradient over the smoky tint at `--pane-a`, frosting the wall behind it.
- **Shadow Strategy:** lift, plus the masked 1px specular rim (see Elevation & Depth).
- **Border:** transparent; the rim is the edge.
- **Internal Padding:** 16px; heads at least 54px with a rule beneath. Tables keep a sticky head of its own glass so rows never show through it.

### Inputs / Fields
- **Style:** 32px, 12px radius, black at 18% with a white-at-12% border; placeholders in faint; caret in system blue.
- **Focus:** border turns system blue with a 3px blue wash halo. The command bar takes a 4px halo on focus-within.
- **Segmented controls:** full-round track at black 22%; the pressed segment is white at 16% with an inset highlight.
- **Toggles:** 34 × 20 track at black 28%, system blue when on.
- **Checkboxes:** a 16px frosted square (5px radius, black at 22%, white-at-24% border) that fills system blue with a white tick when checked, centred on its label's line.

### Navigation
Sidebar items are 36px, 12px radius, muted text at 500 with an 18px stroke icon. Hover is white at 7%. The current page is a brighter glass tile (a white 20% to 9% gradient with the lit nav tile shadow), text at 600, icon in arc-ink. Groups are headed by faint 12px sentence-case labels. In the icon rail, group names become thin rules and counts sit on the icon's corner.

### Status
A status is a shape, a word and a hue together: running is a blue dot with a soft ping; needs you an amber diamond; queued or idle a grey ring; done a green dot; failed or out a red square; fallback a violet triangle; stale or stopped a grey square. The word sits beside it at 12px / 600 in the matching ink.

### KPI Tile (signature)
A 20px glass pane, at least 136px tall, that owns one hue for its dot, round icon and mini chart: Needs you amber, Running blue, Finished green, Quota violet (Analytics adds cost in Claude orange). The figure is 32px mono; the label is sentence case at 13px. Charts draw only real data: one amber diamond per waiting agent; for Running, a slot gauge per tool lane (8 × 22px bars, lit in the tool's hue for each running session, faint glass wells for free slots); finished runs per hour as bars; quota meters per tool, a used-up one hatched red across its full track. When something needs the owner, the Needs you tile's rim turns amber and it gains a faint amber top wash and an amber figure.

### Routing Graph (signature)
Router → tool lane → model → effort → job. Nodes and job cards are denser node glass so wires never show through. Lanes lead with the tool's real app logo and fill their load bar in the tool's hue; an out lane is dashed with a faint red hatch. Each model row carries its tier pill in the tier hue; efforts sit in aligned slots, lit in the tier hue with a mono count where jobs run now, amber where a job needs the owner. Live wires carry a moving dash; the needs-you wire is amber and 2px whatever its tier. Out wires are dashed red. The Router node wears the mark.

### Tool Badge
A 28px badge (34px on lane heads) showing the tool's real app logo: each app's own rounded-square tile at a 22% radius, edge to edge with no disc or frame. Tools without a logo fall back to a neutral round mono monogram.

### Meters
6px rounded bars on a black-at-30% track. The fill takes the tool's hue (or the tile's hue); it turns into a full red hatch only when used up, and grey when unknown.

### Workflow Graph
Phase columns of task nodes (20px glass) joined by hairline edges, centred in a pane that takes the free height. The critical path is the heaviest neutral line; it is weight, not colour.

### The Mark
An italic brush "j" (a hairline entry, a heavy downstroke, a tapered tail) and its round dot, white (to a faint rose-white at the foot) on a crimson-to-black glass tile (mark-crimson → mark-oxblood → mark-black, diagonal) with a top-left white sheen and a faint white rim, at a 23% corner. One SVG serves the sidebar, the command bar, the Router node, the favicon and the macOS app icon; it is never redrawn, recoloured or set beside a second logo.

## Do's and Don'ts

### Do:
- **Do** build every new surface as a pane: `--pane` over `rgba(tint, var(--pane-a))`, the 26px backdrop blur, the masked specular rim and `--lift`.
- **Do** keep text on panes and read `--veil` / `--pane-a`, so the adaptive measurement holds AA on any wallpaper.
- **Do** give every new number, lane or state a hue that already owns that meaning, and carry it from tile to wire to pill.
- **Do** pair every status hue with a shape and a word.
- **Do** set every figure in SF Mono with tabular numerals, including the 32px KPI value.
- **Do** keep the white primary pill the brightest object on screen and the current nav item a brighter glass tile.
- **Do** use node glass (`--node-bg`) for anything that sits over wires.
- **Do** fill the window: let the main pane of a page take the free height instead of leaving blank glass, and end paired rows on one line.
- **Do** provide an opaque fallback under `prefers-reduced-transparency`.
- **Do** author sizes at 100% and let the interface scale (body zoom, 0.85 by default) apply; use `--vh` / `--vw` for full-height parts.
- **Do** show tools with their real app logo and brand hue (Claude orange, agy dark blue).

### Don't:
- **Don't** let anything glow at rest; light comes from the rim and sheen only.
- **Don't** use flat grey-blur cards with a plain border; a pane without its rim and lit gradient is the generic glassmorphism this world refuses.
- **Don't** let the wallpaper's colour fill a surface or compete with a status hue.
- **Don't** use amber for anything that does not need the owner; the critical path, highlights and emphasis stay neutral.
- **Don't** use mark crimson anywhere outside the mark.
- **Don't** use tracked uppercase labels or small-caps kickers above headings.
- **Don't** load hosted fonts or make external requests; system stacks only.
- **Don't** add a light theme; the app is dark only.
- **Don't** add decorative illustration.
- **Don't** let colour alone carry a status.
