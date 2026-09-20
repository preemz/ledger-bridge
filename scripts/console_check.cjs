// Interactive check of the console against the live Django API.
//
// 1. create a migration through the form and follow it to the job page
// 2. wait for the reconciliation report and read the summary
// 3. resolve a break through the inline control
// 4. assert a disabled signal button explains itself
// 5. assert a REFUSED signal (HTTP 200, ok:false) is reported to the operator rather
//    than being announced as accepted
//
// Needs the API on :8000, the console on :3000, a worker, and a seeded database.
// puppeteer-core is deliberately not a project dependency, so install it ad hoc:
//
//   mkdir -p /tmp/lb-pptr && cd /tmp/lb-pptr && npm init -y && npm i puppeteer-core
//   node '<repo>/scripts/console_check.cjs'
//
// Exits non-zero on the first failed check.

const puppeteer = require("puppeteer-core");

const CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome";
const CONSOLE = "http://localhost:3000";
const REF = "NORTHWIND-BROWSER-TEST-" + Date.now().toString().slice(-5);
const REFUSAL = "This migration is running on the inline runner, which cannot be signalled.";

const results = [];
function check(name, ok, detail) {
  results.push({ name, ok });
  console.log(`${ok ? "PASS" : "FAIL"}  ${name}${detail ? " :: " + detail : ""}`);
}
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
// innerText reflects the rendered text, so CSS text-transform matters: headings come
// back uppercased even though the source string is "Reconciliation".
const has = (text, needle) => text.toLowerCase().includes(needle.toLowerCase());

async function waitForText(page, predicate, timeoutMs, label) {
  const deadline = Date.now() + timeoutMs;
  let last = "";
  while (Date.now() < deadline) {
    try {
      last = await page.evaluate(() => document.body.innerText);
    } catch (err) {
      last = `(evaluate failed: ${err.message})`;
    }
    if (predicate(last)) return last;
    await sleep(1000);
  }
  console.log(`     timed out waiting for ${label}; page text head was:\n${last.slice(0, 600)}`);
  return null;
}

(async () => {
  const browser = await puppeteer.launch({
    executablePath: CHROME,
    headless: true,
    args: ["--no-sandbox", "--disable-gpu"],
    userDataDir: "/tmp/lb_pptr_profile",
  });

  // ---------------------------------------------------------------------------
  // Live run against the real backend
  // ---------------------------------------------------------------------------
  const page = await browser.newPage();
  await page.setViewport({ width: 1440, height: 1100 });

  const pageErrors = [];
  page.on("pageerror", (e) => pageErrors.push("pageerror: " + e.message));
  page.on("console", (m) => {
    if (m.type() === "error") pageErrors.push("console: " + m.text());
  });
  page.on("response", (r) => {
    if (r.url().includes("/api/") && r.status() >= 400) {
      pageErrors.push(`http ${r.status()} ${r.request().method()} ${r.url()}`);
    }
  });

  await page.goto(`${CONSOLE}/`, { waitUntil: "domcontentloaded" });
  await page.waitForSelector("#customer_reference", { timeout: 30000 });
  const dash = await waitForText(page, (t) => has(t, "Migration jobs"), 30000, "dashboard");
  check("dashboard renders", !!dash);

  await page.type("#customer_reference", REF);
  await page.click("button[type=submit]");
  await sleep(3000);
  check("form POST navigates to the new job", page.url().includes("/jobs/"), page.url());

  const detail = await waitForText(
    page,
    (t) => has(t, "Reconciliation") && /NEEDS_REVIEW|COMPLETED/i.test(t) && has(t, "Legacy debit"),
    300000,
    "reconciliation report"
  );
  check("reconciliation report rendered", !!detail);
  if (detail) {
    check("phase timeline rendered", has(detail, "Phase timeline") && has(detail, "RECONCILE"));
    check("job reached a terminal status", /NEEDS_REVIEW|COMPLETED/i.test(detail));
    const diff = (detail.match(/Difference[\s\S]{0,40}/) || ["(none)"])[0].replace(/\s+/g, " ");
    check(
      "reconciliation summary rendered",
      has(detail, "Legacy debit") && has(detail, "Loaded debit"),
      diff
    );
    check("polling stopped on the terminal job", has(detail, "Polling stopped"));
  }

  // resolve a break through the inline control
  let code = null;
  try {
    await page.waitForSelector('select[aria-label^="Classification for account"]', { timeout: 30000 });
    code = (
      await page.$eval('select[aria-label^="Classification for account"]', (el) =>
        el.getAttribute("aria-label")
      )
    )
      .replace("Classification for account ", "")
      .trim();
  } catch (err) {
    console.log("     no unresolved break row found: " + err.message);
  }

  if (code) {
    const unresolvedBefore = await page.$$eval(".resolve-form", (els) => els.length);
    await page.select(`select[aria-label="Classification for account ${code}"]`, "TIMING");
    await page.type(
      `input[aria-label="Resolution note for account ${code}"]`,
      "Confirmed with the finance lead during the cutover review."
    );
    await page.evaluate((accountCode) => {
      const sel = document.querySelector(
        `select[aria-label="Classification for account ${accountCode}"]`
      );
      sel.closest(".resolve-form").querySelector("button").click();
    }, code);
    await sleep(5000);
    const after = await page.evaluate((accountCode) => {
      const rows = Array.from(document.querySelectorAll("tbody tr"));
      const row = rows.find((r) => r.innerText.includes(accountCode));
      return {
        rowText: row ? row.innerText.replace(/\n/g, " | ") : "(row not found)",
        resolvedRows: document.querySelectorAll(".resolved-line").length,
        auditHasResolve: document.body.innerText.toLowerCase().includes("break.resolved"),
      };
    }, code);
    check(
      "break row shows Resolved after the inline control posts",
      after.resolvedRows >= 1,
      `${unresolvedBefore} unresolved before; row now: ${after.rowText.slice(0, 140)}`
    );
    check("audit log recorded break.resolved", after.auditHasResolve);
  } else {
    check("break resolve control present", false, "no unresolved break row rendered");
  }

  // disabled signal buttons must explain themselves
  const pause = await page.evaluate(() => {
    const button = Array.from(document.querySelectorAll("button")).find(
      (b) => b.innerText.trim() === "Pause"
    );
    return button
      ? { disabled: button.disabled, title: button.getAttribute("title") || "" }
      : { disabled: null, title: "(no Pause button)" };
  });
  check(
    "terminal job disables Pause and says why",
    pause.disabled === true && /cannot be signalled|finished/i.test(pause.title),
    `${pause.disabled ? "disabled" : "enabled"}; title="${pause.title}"`
  );

  // ---------------------------------------------------------------------------
  // Refused signal: HTTP 200 with ok:false must not be reported as success
  // ---------------------------------------------------------------------------
  const stub = await browser.newPage();
  const fakeJob = {
    id: "11111111-1111-1111-1111-111111111111",
    customer_reference: "STUBBED-RUNNING-JOB",
    source_system: "legacy_erp",
    as_of_date: "2026-09-08",
    status: "RUNNING",
    executor: "inline",
    temporal_workflow_id: "",
    counters: { rows_read: 1200 },
    error: "",
    started_at: "2026-09-18T10:00:00Z",
    finished_at: null,
    created_at: "2026-09-18T10:00:00Z",
    phases: [],
    reconciliation: null,
  };

  await stub.setRequestInterception(true);
  const CORS = {
    "access-control-allow-origin": "*",
    "access-control-allow-methods": "GET, POST, OPTIONS",
    "access-control-allow-headers": "content-type",
  };
  stub.on("request", (req) => {
    const url = req.url();
    // The console is served from :3000 and the API is :8000, so the POST needs a real
    // preflight answer or the browser blocks it before the handler ever runs.
    if (req.method() === "OPTIONS") {
      req.respond({ status: 204, headers: CORS, body: "" });
      return;
    }
    if (url.includes("/signal/")) {
      req.respond({
        status: 200,
        contentType: "application/json",
        headers: CORS,
        body: JSON.stringify({ ok: false, signal: "pause", status: "RUNNING", detail: REFUSAL }),
      });
      return;
    }
    if (url.includes("/api/jobs/11111111-1111-1111-1111-111111111111/")) {
      req.respond({
        status: 200,
        contentType: "application/json",
        headers: CORS,
        body: JSON.stringify(fakeJob),
      });
      return;
    }
    if (url.includes("/audit/")) {
      req.respond({
        status: 200,
        contentType: "application/json",
        headers: CORS,
        body: "[]",
      });
      return;
    }
    req.continue();
  });

  await stub.goto(`${CONSOLE}/jobs/11111111-1111-1111-1111-111111111111`, {
    waitUntil: "domcontentloaded",
  });
  await sleep(5000);
  const stubState = await stub.evaluate(() => {
    const button = Array.from(document.querySelectorAll("button")).find(
      (b) => b.innerText.trim() === "Pause"
    );
    return { enabled: button ? !button.disabled : null, text: document.body.innerText };
  });
  check("running inline job enables the Pause button", stubState.enabled === true);

  const refusal = await stub.evaluate(async () => {
    const button = Array.from(document.querySelectorAll("button")).find(
      (b) => b.innerText.trim() === "Pause"
    );
    if (!button) return "(no button)";
    button.click();
    await new Promise((r) => setTimeout(r, 4000));
    return document.body.innerText.replace(/\s+/g, " ");
  });
  const claimsAccepted = /signal accepted|accepted\. Job status/i.test(refusal);
  const showsRefusal = refusal.includes(REFUSAL.slice(0, 40));
  check("refused signal is shown to the operator", showsRefusal, 
    (refusal.match(/.{0,80}inline runner.{0,80}/i) || ["(refusal text absent)"])[0]);
  check("refused signal is NOT reported as accepted", !claimsAccepted,
    claimsAccepted ? "still claims success" : "no false success message");

  const noisy = pageErrors.filter((e) => !/favicon|icon\.svg/i.test(e));
  check("no uncaught page errors or 4xx/5xx API responses", noisy.length === 0, noisy.slice(0, 4).join(" | "));

  await browser.close();
  const failed = results.filter((r) => !r.ok);
  console.log(`\n${results.length - failed.length}/${results.length} checks passed`);
  console.log("RESULT:", failed.length === 0 ? "PASS" : "FAIL");
  process.exit(failed.length === 0 ? 0 : 1);
})().catch((err) => {
  console.error("driver error:", err.message);
  process.exit(2);
});
