#!/usr/bin/env python
"""
Browser regression harness for the Smart Lab frontend (smart_lab_charts.js and the page templates).

Drives a real headless Chromium (Playwright) against a RUNNING NEMO dev server, walks every configured
tool's pages the way a person would - overview, base-pressure/continuous-pressure charts, the Trends tab
and its pagination, a full run's chart tabs (incl. tab switching, zoom, reset zoom, CSV download names),
the run history page, and the Data page's sortable tables - and records what it saw as a stable JSON
report. Read-only: it only issues GETs the UI itself issues.

Typical use around a refactor of the JS/templates:

    ./harness/browser_check.py --save /tmp/before.json      # on the code as-is
    ...refactor...
    ./harness/browser_check.py --compare /tmp/before.json   # exit 1 + a diff if anything changed

Needs the dev server up (default http://localhost:8100, which auto-logs-in as the dev user) and
`playwright` + its chromium installed in the venv running this script:

    ~/dev/snf/NEMO/venv/bin/python harness/browser_check.py --help
"""

import argparse
import json
import re
import sys
import time
from urllib.parse import urljoin

from playwright.sync_api import TimeoutError as PWTimeout
from playwright.sync_api import sync_playwright

CHART_WAIT_MS = 90_000  # a cold tool can need a real Oak fetch before its first chart draws
UI_WAIT_MS = 15_000

# (input seconds) -> what smartLabFormatElapsed must return. A pure function, so this pins the exact
# formatting the Events list and chart axes rely on, independent of any tool's data.
ELAPSED_CASES = [0, 1.2, 59.94, 60, 61.5, 2761.6, 3599.9, 3600, 3661, 90061.5]


class Page:
    """One browsing context with everything noteworthy the page emitted captured for the report."""

    def __init__(self, browser, base):
        self.base = base
        self.ctx = browser.new_context(accept_downloads=True, viewport={"width": 1400, "height": 1000})
        self.page = self.ctx.new_page()
        self.console_errors = []
        self.bad_responses = []
        self.page.on("console", lambda m: self.console_errors.append(m.text) if m.type == "error" else None)
        self.page.on("pageerror", lambda e: self.console_errors.append(f"PAGEERROR: {e}"))
        self.page.on(
            "response",
            lambda r: self.bad_responses.append(f"{r.status} {r.url.replace(base, '')}")
            if r.status >= 400 and r.url.startswith(base)
            else None,
        )

    def goto(self, path):
        self.console_errors.clear()
        self.bad_responses.clear()
        self.page.goto(urljoin(self.base, path), wait_until="domcontentloaded", timeout=120_000)

    def problems(self):
        return {"console_errors": sorted(set(self.console_errors)), "bad_responses": sorted(set(self.bad_responses))}


def chart_state(page, mount_id, wait_ms=CHART_WAIT_MS):
    """Wait until `mount_id` shows either a rendered uPlot, an events list/table, or an error message, then
    describe which - the thing a person would actually see."""
    js = """(id) => {
        const m = document.getElementById(id); if (!m) return {kind: 'missing'};
        const err = document.getElementById(id + '-error');
        if (err && err.style.display !== 'none' && err.textContent.trim()) return {kind: 'error', text: err.textContent.trim().slice(0, 120)};
        if (m.querySelector('.uplot')) {
            const inst = (window.SMART_LAB_CHARTS && SMART_LAB_CHARTS[id]) ? SMART_LAB_CHARTS[id].instance : null;
            return {kind: 'uplot', series: m.querySelectorAll('.u-legend .u-series').length - 1,
                    points: inst && inst.data && inst.data[0] ? inst.data[0].length : null};
        }
        if (m.querySelector('table')) return {kind: 'list', rows: m.querySelectorAll('tbody tr').length};
        return null;
    }"""
    try:
        page.wait_for_function(f"(id) => ({js})(id) !== null", arg=mount_id, timeout=wait_ms)
    except PWTimeout:
        return {"kind": "timeout"}
    return page.evaluate(js, mount_id)


def activate_tab(page, key, mount_id="smart-lab-chart-mount"):
    """Click a chart tab and wait until THAT tab's content is what's on screen (not the previous tab's still-drawn
    chart), then describe it. Deterministic: waits on the chart registry's own URL (which carries group=<key>) or,
    for a list/error tab, on the mount's actual content."""
    page.locator(f"#{mount_id}-tabs [data-group-key='{key}']").click()
    page.wait_for_function(
        """([id, key]) => {
            const m = document.getElementById(id);
            const err = document.getElementById(id + '-error');
            if (err && err.style.display !== 'none' && err.textContent.trim()) return true;
            const entry = window.SMART_LAB_CHARTS && SMART_LAB_CHARTS[id];
            if (entry && entry.baseUrl && entry.baseUrl.includes('group=' + key + '&') || (entry && entry.baseUrl && entry.baseUrl.endsWith('group=' + key))) return !!m.querySelector('.uplot');
            if (!entry && m.querySelector('table') && !m.querySelector('.uplot')) return true;
            return false;
        }""",
        arg=[mount_id, key],
        timeout=CHART_WAIT_MS,
    )
    return chart_state(page, mount_id)


def csv_link_state(page, mount_id, download=True):
    """Visibility of a chart's 'Download as CSV' link and (optionally) the filename the browser is given."""
    link = page.locator(f"#{mount_id}-csv-link")
    if not link.count():
        return {"present": False}
    visible = link.is_visible()
    state = {"present": True, "visible": visible}
    if visible and download:
        href = link.get_attribute("href")
        try:
            with page.expect_download(timeout=UI_WAIT_MS) as dl:
                link.click()
            state["filename"] = dl.value.suggested_filename
        except PWTimeout:
            state["filename"] = "NO-DOWNLOAD"
        state["href_is_placeholder"] = href in (None, "#")
    return state


def check_dashboard(b, base):
    p = Page(b, base)
    p.goto("/smart_lab/")
    p.page.wait_for_load_state("networkidle", timeout=60_000)
    hrefs = p.page.eval_on_selector_all(
        "a[href*='/smart_lab/tool/']", "els => els.map(e => e.getAttribute('href'))"
    )
    ids = sorted({int(m.group(1)) for h in hrefs if (m := re.match(r"^/smart_lab/tool/(\d+)/$", h or ""))})
    result = {
        "tool_ids": ids,
        "status_badges": sorted(set(p.page.eval_on_selector_all(".label", "els => els.map(e => e.textContent.trim().replace(/\\s+/g,' '))"))),
        **p.problems(),
    }
    p.ctx.close()
    return result, ids


def check_elapsed_formatting(b, base):
    p = Page(b, base)
    p.goto("/smart_lab/")
    p.page.wait_for_load_state("networkidle", timeout=60_000)
    # The chart script is only on tool pages - load one to get its globals.
    return p


def check_tool(b, base, tool_id):
    out = {}
    p = Page(b, base)

    # ---- overview: base pressure + continuous pressure charts, Trends tab ------------------------
    p.goto(f"/smart_lab/tool/{tool_id}/")
    out["title"] = p.page.title()
    out["base_pressure"] = chart_state(p.page, "smart-lab-base-pressure-chart") if p.page.locator("#smart-lab-base-pressure-chart").count() else {"kind": "absent"}
    if out["base_pressure"].get("kind") == "uplot":
        out["base_pressure"]["csv"] = csv_link_state(p.page, "smart-lab-base-pressure-chart", download=False)
    if p.page.locator("#smart-lab-continuous-pressure-chart").count():
        out["continuous_pressure"] = chart_state(p.page, "smart-lab-continuous-pressure-chart")
        if out["continuous_pressure"].get("kind") == "uplot":
            out["continuous_pressure"]["csv"] = csv_link_state(p.page, "smart-lab-continuous-pressure-chart")
    out["overview_problems"] = p.problems()

    trends_tab = p.page.locator("#smart-lab-tool-tabs li[data-tab-key='trends'] a")
    if trends_tab.count():
        trends_tab.click()
        try:
            p.page.wait_for_selector("#smart-lab-trends-mount table, #smart-lab-trends-mount .text-danger, #smart-lab-trends-mount .alert", timeout=CHART_WAIT_MS)
        except PWTimeout:
            pass
        tables = p.page.locator("#smart-lab-trends-mount table.smart-lab-paginated-table")
        pagers = p.page.locator("#smart-lab-trends-mount button:has-text('Older')")
        labels = p.page.eval_on_selector_all(
            "#smart-lab-trends-mount", "els => els.flatMap(e => (e.innerText.match(/Weeks? \\d+.{0,3}\\d+ of \\d+[^\\n]*/g) || []))"
        )
        out["trends"] = {"tables": tables.count(), "pagers": pagers.count(), "page_labels": labels[:8]}
    out["trends_problems"] = p.problems()

    # ---- run history ------------------------------------------------------------------------------
    p.goto(f"/smart_lab/tool/{tool_id}/history/")
    p.page.wait_for_load_state("networkidle", timeout=CHART_WAIT_MS)
    rows = p.page.locator("table tbody tr").count()
    run_href = p.page.eval_on_selector_all("a[href*='run=']", "els => els.length ? els[0].getAttribute('href') : null")
    summary = p.page.eval_on_selector_all(".pagination li, p", "els => els.map(e => e.textContent).join(' ').match(/Showing page[^.]*\\./)")
    out["history"] = {"rows": rows, "has_run_link": bool(run_href), "summary": (summary or [""])[0][:80] if summary else None, **p.problems()}

    # ---- full run page: chart tabs, zoom, csv ---------------------------------------------------
    if run_href:
        p.goto(run_href)
        tabs = p.page.locator("#smart-lab-chart-mount-tabs [data-group-key]")
        keys = [tabs.nth(i).get_attribute("data-group-key") for i in range(tabs.count())]
        run_info = {"tab_keys": keys, "tabs": {}}
        for key in keys[:6]:
            state = activate_tab(p.page, key)
            state["csv"] = csv_link_state(p.page, "smart-lab-chart-mount")
            run_info["tabs"][key] = state
        # zoom on the first line-chart tab
        for key in keys:
            state = run_info["tabs"].get(key, {})
            if state.get("kind") == "uplot" and (state.get("points") or 0) > 20:
                activate_tab(p.page, key)
                over = p.page.locator("#smart-lab-chart-mount .u-over")
                box = over.bounding_box()
                get_scale = "() => { const u = SMART_LAB_CHARTS['smart-lab-chart-mount'].instance; return [u.scales.x.min, u.scales.x.max]; }"
                before = p.page.evaluate(get_scale)
                p.page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
                for _ in range(4):
                    p.page.mouse.wheel(0, -400)
                    p.page.wait_for_timeout(120)
                p.page.wait_for_timeout(1500)  # debounced range re-fetch
                zoomed = p.page.evaluate(get_scale)
                p.page.evaluate("() => smartLabResetZoom('smart-lab-chart-mount')")
                p.page.wait_for_timeout(1500)
                reset = p.page.evaluate(get_scale)
                run_info["zoom"] = {
                    "tab": key,
                    "zoomed_in": (zoomed[1] - zoomed[0]) < (before[1] - before[0]),
                    "reset_restores_range": abs((reset[1] - reset[0]) - (before[1] - before[0])) < 1e-6 * max(1, before[1] - before[0]) or (reset[1] - reset[0]) >= (before[1] - before[0]) * 0.99,
                }
                break
        run_info.update(p.problems())
        out["run_detail"] = run_info

    # ---- data page (recipes + configs, sortable tables) ------------------------------------------
    p.goto(f"/smart_lab/tool/{tool_id}/data/")
    p.page.wait_for_load_state("networkidle", timeout=CHART_WAIT_MS)
    data = {"tables": p.page.locator("table.smart-lab-sortable-table").count(), "row_counts": p.page.eval_on_selector_all(
        "table.smart-lab-sortable-table", "els => els.map(t => t.querySelectorAll('tbody tr').length)")}
    header = p.page.locator("table.smart-lab-sortable-table thead th").first
    if header.count() and header.is_visible():
        first_before = p.page.eval_on_selector("table.smart-lab-sortable-table tbody tr td", "e => e.textContent.trim()")
        header.click()
        p.page.wait_for_timeout(300)
        first_after = p.page.eval_on_selector("table.smart-lab-sortable-table tbody tr td", "e => e.textContent.trim()")
        header.click()
        p.page.wait_for_timeout(300)
        first_desc = p.page.eval_on_selector("table.smart-lab-sortable-table tbody tr td", "e => e.textContent.trim()")
        data["sort_reorders_rows"] = len({first_before, first_after, first_desc}) > 1 or data["row_counts"][0] < 2
    data.update(p.problems())
    out["data_page"] = data
    p.ctx.close()
    return out


def check_elapsed(b, base, tool_id):
    p = Page(b, base)
    p.goto(f"/smart_lab/tool/{tool_id}/")
    p.page.wait_for_function("() => typeof smartLabFormatElapsed === 'function'", timeout=UI_WAIT_MS)
    values = p.page.evaluate("(cases) => cases.map(s => [s, smartLabFormatElapsed(s)])", ELAPSED_CASES)
    globals_present = p.page.evaluate(
        """() => ['smartLabRenderChart','smartLabInitTabbedChart','smartLabInitBasePressureChart','smartLabResetZoom',
                  'smartLabDispatchChartData','smartLabSetCsvDownload','smartLabDownloadCsvRows','smartLabPaginateTables',
                  'smartLabFormatElapsed','smartLabElapsedAxisValues','smartLabCsvFilename','smartLabCsvNamePrefix',
                  'smartLabRenderUplot','smartLabAttachUplotZoom','smartLabShowError','smartLabDestroyChart']
                 .filter(n => typeof window[n] !== 'function')"""
    )
    return {"elapsed_format": values, "missing_globals": globals_present}


def diff(a, b, path=""):
    """Human-readable differences between two report dicts (timings/ordering-insensitive by construction)."""
    out = []
    if isinstance(a, dict) and isinstance(b, dict):
        for k in sorted(set(a) | set(b)):
            if k not in a:
                out.append(f"+ {path}/{k} = {b[k]!r}")
            elif k not in b:
                out.append(f"- {path}/{k} = {a[k]!r}")
            else:
                out += diff(a[k], b[k], f"{path}/{k}")
    elif a != b:
        out.append(f"~ {path}: {a!r} -> {b!r}")
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--base", default="http://localhost:8100", help="running NEMO dev server (default %(default)s)")
    ap.add_argument("--tools", help="comma-separated tool ids (default: every tool on the dashboard)")
    ap.add_argument("--save", metavar="FILE", help="write the report here")
    ap.add_argument("--compare", metavar="FILE", help="diff this run against a saved report; exit 1 on any difference")
    ap.add_argument("--headed", action="store_true", help="show the browser")
    args = ap.parse_args()

    started = time.time()
    with sync_playwright() as pw:
        b = pw.chromium.launch(headless=not args.headed)
        dash, ids = check_dashboard(b, args.base)
        if args.tools:
            ids = [int(x) for x in args.tools.split(",")]
        report = {"dashboard": dash, "tools": {}}
        if ids:
            report["frontend"] = check_elapsed(b, args.base, ids[0])
        for tid in ids:
            print(f"checking tool {tid} ...", file=sys.stderr, flush=True)
            report["tools"][str(tid)] = check_tool(b, args.base, tid)
        b.close()

    text = json.dumps(report, indent=2, sort_keys=True)
    if args.save:
        with open(args.save, "w") as f:
            f.write(text + "\n")
        print(f"saved report to {args.save} ({time.time() - started:.0f}s)", file=sys.stderr)

    # Problems are always a failure, whatever the comparison says.
    problems = []
    def walk(o, path=""):
        if isinstance(o, dict):
            for k, v in o.items():
                if k in ("console_errors", "bad_responses") and v:
                    problems.append(f"{path}/{k}: {v}")
                else:
                    walk(v, f"{path}/{k}")
    walk(report)
    if report.get("frontend", {}).get("missing_globals"):
        problems.append(f"missing JS globals: {report['frontend']['missing_globals']}")

    if args.compare:
        with open(args.compare) as f:
            base_report = json.load(f)
        if args.tools:  # only compare the tools this run actually covered
            base_report["tools"] = {k: v for k, v in base_report.get("tools", {}).items() if k in report["tools"]}
            base_report.pop("dashboard", None)
            report_cmp = {k: v for k, v in report.items() if k != "dashboard"}
        else:
            report_cmp = report
        changes = diff(base_report, report_cmp)
        print("\n".join(changes) if changes else "no differences vs " + args.compare)
        if changes:
            problems.append(f"{len(changes)} difference(s) vs baseline")
    elif not args.save:
        print(text)

    for pr in problems:
        print("PROBLEM:", pr, file=sys.stderr)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
