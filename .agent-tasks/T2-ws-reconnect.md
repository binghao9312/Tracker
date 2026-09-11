# T2 — Frontend WebSocket auto-reconnect + render throttling

Repo root is the current directory. Frontend lives in `frontend/`.

## Hard constraints
- Modify ONLY: `frontend/src/App.tsx`, `frontend/src/MetricChart.tsx`
- Create ONLY: `frontend/src/useReconnectingSocket.ts`
- Do NOT touch anything under `backend/`, `config/`, `.github/`, or any `*.md`
- Do NOT add third-party dependencies; `package.json` must stay byte-identical
- Keep the existing visual layout and CSS class names unchanged

## Context the task depends on
- React 19 + TypeScript, Vite. The only build/check command is `npm run build`
  (which runs `tsc -b` then `vite build`). There is no frontend test suite.
- `socketUrl(path)` already exists in `App.tsx` and builds the ws URL. Reuse it.
- There are three WebSocket `useEffect` blocks in `App.tsx` today:
  1. `/ws/scanner` (around line 83) — sets `connection` state, handles `type: "scanner"`
     messages, appends to `alerts`.
  2. `/ws/symbol/${selected}` — sets `detail`.
  3. `/ws/paper` — calls `refresh()` which re-fetches three REST endpoints.
- `connection` is a `string` state rendered in the UI. Existing values used:
  `"CONNECTING"`, `"CONNECTED"`, `"RECONNECTING"`, `"OFFLINE"`. Keep those exact strings.
- The backend restarts frequently in development, so reconnect must recover on its own.

## Required behaviour

### 1. `useReconnectingSocket` hook (new file)
Export a hook that owns one auto-reconnecting WebSocket. Suggested shape — adapt the
names if you need to, but the behaviour below is the contract:

```ts
useReconnectingSocket(path: string | null, options: {
  onMessage: (data: string) => void;
  onOpen?: () => void;
  onStatusChange?: (status: "CONNECTING" | "CONNECTED" | "RECONNECTING") => void;
}): void
```

- `path === null` means "do not connect"; if already connected, close and stay closed.
- On close or error while still mounted: reconnect with exponential backoff starting at
  1000ms, doubling, capped at 30000ms. Reset the delay to 1000ms after a successful open.
- `onOpen` fires on every successful open, including reconnects (callers use it to
  re-fetch a REST snapshot so the UI is not left holding stale data).
- Callbacks must be read through a ref so that passing a fresh inline arrow function on
  every render does NOT tear down and rebuild the socket. Reconnecting must depend on
  `path` only.
- Cleanup on unmount / `path` change must close the socket AND clear any pending
  reconnect timer. No reconnect may fire after cleanup.
- No `any` casts. `tsc -b` runs with the project's existing strictness.

### 2. Use the hook for all three sockets in `App.tsx`
- Scanner socket: on every open, re-fetch `/api/scanner` into `rows` (not just the
  first mount) and set `connection` to `"CONNECTED"`; report `"RECONNECTING"` while down.
- Symbol socket: `path` is `null` unless `selected && mode === "SCANNER"`.
- Paper socket: `path` is `null` unless `mode === "PAPER"`; call the existing `refresh()`
  on open.
- Preserve all current message handling exactly, including the alerts logic and the
  `Array.isArray(message.data)` snapshot branch.

### 3. Throttle scanner row updates
Today every scanner message calls `setRows`, so ~50 full-table re-renders per second.
Accumulate incoming rows in a `useRef<Map<string, ScannerRow>>` and flush to `setRows`
at most once every 250ms (a timer or `requestAnimationFrame` is fine). Requirements:
- The rendered table must still converge to the latest value for every symbol.
- A full-snapshot message (`Array.isArray(message.data)`) must replace the whole set
  and not be merged into stale per-symbol entries.
- The alerts logic must still run per message, not per flush.
- Flush any pending batch on unmount.

### 4. Stop `MetricChart` from rebuilding on every parent render
`MetricChart.tsx` has one `useEffect` with `[data, markers]` deps that calls
`chart.remove()` + `createChart()`. `markers` is an inline array literal in `App.tsx`,
so it is a new reference every render and the chart is destroyed and recreated
constantly (visible flicker, lost zoom/pan).
- Split into two effects: create the chart/series once on mount, and update data and
  markers in a separate effect via the series API (`setData`, and the v5 markers API
  already imported in that file) without recreating the chart.
- Wrap the `markers` array passed from `App.tsx` in `useMemo`.
- Keep the existing chart appearance, time-axis config, and price formatting identical.

## Acceptance
Run from the repo root and report the real output of each:

    cd frontend && npm run build

Then confirm and state explicitly in your final report:
- `git diff --name-only` lists only the files allowed above
- `git diff -- frontend/package.json frontend/package-lock.json` is empty
- grep proof that no `socket.onclose = () => setConnection("RECONNECTING")` remains
  without a reconnect path, i.e. show the final `onclose`/reconnect code from
  `useReconnectingSocket.ts`

In your final report, paste: the `npm run build` tail, the `git diff --stat`, and the
full contents of `frontend/src/useReconnectingSocket.ts`.
