// End-to-end smoke test against a running AXIOM server with at least one processed document.
//   node e2e/smoke.mjs [http://127.0.0.1:8000]      (pnpm e2e)
// Optional: AXIOM_E2E_VIZ_UNIT=<unit id with a generated visualization> to also test the sandbox from the browser.
// Browser: PLAYWRIGHT_CHROMIUM=<path to chrome> or the playwright cache (npx playwright-core install chromium).
import { chromium } from "playwright-core";

const BASE = process.argv[2] ?? "http://127.0.0.1:8000";
const results = [];
const check = (name, ok, detail = "") => {
  results.push({ name, ok, detail });
  console.log(`${ok ? "PASS" : "FAIL"}  ${name}${detail ? "  — " + detail : ""}`);
};

const api = async (p) => (await fetch(BASE + "/api" + p)).json();
const docs = await api("/documents");
if (!docs.length) throw new Error("no documents on the server");
const doc = await api(`/documents/${docs[0].id}`);
const sections = doc.chapters.flatMap((c) => c.sections).filter((s) => ["done", "partial"].includes(s.status));
const reading = sections.find((s) => !s.is_exercises);
const exSection = sections.find((s) => s.is_exercises);

const browser = await chromium.launch(process.env.PLAYWRIGHT_CHROMIUM ? { executablePath: process.env.PLAYWRIGHT_CHROMIUM } : {});
const page = await browser.newPage({ viewport: { width: 1280, height: 1600 } });
const errors = [];
page.on("pageerror", (e) => errors.push(String(e)));
page.on("console", (m) => m.type() === "error" && !/Failed to load resource.*404/.test(m.text()) && errors.push(m.text()));

// library → document
await page.goto(BASE + "/", { waitUntil: "networkidle" });
check("library lists the document", (await page.textContent("body")).includes(doc.title.slice(0, 20)));
await page.goto(`${BASE}/doc/${doc.id}`, { waitUntil: "networkidle" });
check("document page shows chapters", (await page.locator(".chapter").count()) === doc.chapters.length);
check("untouched book has no reading status", (await page.locator(".reading-status").count()) === 0);
const cards = await api(`/documents/${doc.id}/anki?format=json`);
check("study tools: generate or export", cards.length
  ? (await page.getByRole("link", { name: "Download .apkg" }).count()) === 1
  : (await page.getByRole("button", { name: /Generate study cards|sections queued or running/ }).count()) === 1);
check("study tools: model picker", (await page.getByRole("combobox", { name: "Model for Anki generation" }).count()) === 1);
check("book offers a Codex-generated cover", (await page.getByRole("button", { name: /cover with Codex|Cover queued/ }).count()) === 1);
check("chapter exam link", (await page.locator('a:has-text("exam")').count()) >= 1);

await page.getByRole("link", { name: "Jobs", exact: true }).click();
await page.getByRole("heading", { name: /^Waiting/ }).waitFor();
check("jobs tab shows queue and running work", (await page.getByRole("heading", { name: /^Running/ }).count()) === 1);
let requested;
await page.route("**/api/jobs", (route) => route.fulfill({ json: { jobs: [{
  id: "smoke-job", doc_id: doc.id, title: doc.title, kind: "anki", target_id: reading.id,
  status: "running", depends_on: [], model: "local", model_group: "anki", priority: 0,
  parallel: 0, worker: null, error: null, requested_action: requested?.action ?? null,
  restart_model: requested?.model ?? null,
}] } }));
await page.route("**/api/jobs/smoke-job", async (route) => {
  requested = route.request().postDataJSON();
  await route.fulfill({ status: 204 });
});
await page.getByRole("combobox", { name: "Restart model for smoke-job" }).selectOption("auto");
await page.getByRole("button", { name: "Restart", exact: true }).click();
await page.getByText(/Restart requested/).waitFor();
check("running job can request a model restart", requested?.action === "restart" && requested?.model === "auto");
await page.unroute("**/api/jobs/smoke-job");
await page.unroute("**/api/jobs");

// reader: three levels
await page.goto(`${BASE}/read/${reading.id}`, { waitUntil: "networkidle" });
await page.waitForSelector(".unit");
const count = async () => page.locator(`[id="sec-${reading.id}"] .unit`).count();
await page.keyboard.press("1"); await page.waitForTimeout(400); const fast = await count();
await page.keyboard.press("2"); await page.waitForTimeout(400); const std = await count();
await page.keyboard.press("3"); await page.waitForTimeout(400); const deep = await count();
check("levels nest: fast ≤ standard ≤ deep", fast <= std && std <= deep && fast > 0, `${fast} / ${std} / ${deep}`);
check("rendered math (KaTeX)", (await page.locator(".katex").count()) > 0);
await page.locator(`[id="sec-${reading.id}"] .source-btn`).first().click();
await page.waitForSelector(".source-panel img, .source-panel", { timeout: 10000 });
check("source panel opens with the scan", (await page.locator(".source-panel").count()) > 0);
const secView = await api(`/sections/${reading.id}`);
if (secView.verified) {
  const n = await page.locator(`[id="sec-${reading.id}"] .checked, [id="sec-${reading.id}"] .flag-reverted, [id="sec-${reading.id}"] .flag-unverified_claim`).count();
  check("faithfulness markers on a verified section", n > 0, `${n} markers`);
  check("'Test yourself' link on a verified section", (await page.locator('a:has-text("Test yourself")').count()) > 0);
}

if (doc.reading_section_ids.includes(reading.id)) {
  await page.locator(`[id="sec-${reading.id}"]`).getByRole("button", { name: /Mar(k section|ked) as read/ }).evaluate((button) => button.click());
  await page.goto(`${BASE}/doc/${doc.id}`, { waitUntil: "networkidle" });
  check("reading progress is independent of processing", await page.getByText("100% read", { exact: true }).count() > 0);
  check("opened book shows its reading status", (await page.locator(".reading-status").textContent()) ===
    (doc.reading_section_ids.length === 1 ? "Finished reading" : "Still reading"));
}

// exercises
if (exSection) {
  const sec = await api(`/sections/${exSection.id}`);
  const ex = sec.exercises[0];
  await page.goto(`${BASE}/exercise/${ex.id}`, { waitUntil: "networkidle" });
  check("exercise page: hints + check + solution", (await page.locator('button:has-text("Hint 1")').count()) === 1
    && (await page.locator('button:has-text("Check my work")').count()) === 1
    && (await page.locator('text=show the solution').count()) === 1);
}
await page.goto(`${BASE}/practice/${doc.id}`, { waitUntil: "networkidle" });
check("exercise-first practice lists exercises", (await page.locator(".practice-list li").count()) > 0);

// sandboxed visualization, tested from the browser side
const vizUnit = process.env.AXIOM_E2E_VIZ_UNIT;
if (vizUnit) {
  const vp = await browser.newPage();
  await vp.goto(BASE + "/", { waitUntil: "networkidle" }); // same-origin parent, like the reader
  await vp.evaluate((src) => { const f = document.createElement("iframe"); f.id = "f"; f.sandbox = "allow-scripts"; f.src = src; document.body.append(f); },
    `${BASE}/api/units/${vizUnit}/visualization.html`);
  await vp.waitForTimeout(1500);
  const frame = vp.frames().find((f) => f.url().includes("visualization.html"));
  const probe = await frame.evaluate(async () => {
    const out = {};
    try { out.parent = window.parent.document ? "LEAK" : "blocked"; } catch { out.parent = "blocked"; }
    try { await fetch("/api/documents"); out.fetch = "LEAK"; } catch { out.fetch = "blocked"; }
    try { localStorage.getItem("x"); out.storage = "LEAK"; } catch { out.storage = "blocked"; }
    out.origin = window.origin;
    return out;
  });
  check("viz sandbox: no parent DOM, no fetch, no storage, opaque origin",
    probe.parent === "blocked" && probe.fetch === "blocked" && probe.storage === "blocked" && probe.origin === "null",
    JSON.stringify(probe));
  const parentReach = await vp.evaluate(() => { try { return document.getElementById("f").contentDocument === null ? "blocked" : "LEAK"; } catch { return "blocked"; } });
  check("viz sandbox: parent cannot script into the frame either", parentReach === "blocked");
  await vp.close();
}

check("no page errors", errors.length === 0, errors.slice(0, 3).join(" | "));
await browser.close();
const failed = results.filter((r) => !r.ok).length;
console.log(`\n${results.length - failed}/${results.length} passed`);
process.exit(failed ? 1 : 0);
