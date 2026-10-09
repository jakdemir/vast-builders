/**
 * Feature 4 — Find more like this (client helper).
 * Pure fetch wrapper; parent wires the button and appends results.
 *
 * @param {string} apiBase  e.g. "" or "/app/crosswatch" (no trailing slash)
 * @param {string} caseId
 * @param {string[]} [excludeSources]
 * @returns {Promise<{case_id: string, query: string, count: number, results: object[]}>}
 */
async function findMoreLikeThis(apiBase, caseId, excludeSources) {
  const base = (apiBase || "").replace(/\/$/, "");
  const url = `${base}/api/find-more`;
  const res = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      case_id: caseId,
      exclude_sources: excludeSources || [],
    }),
  });
  const data = await res.json();
  if (!res.ok) {
    throw new Error(data.error || `find-more failed (${res.status})`);
  }
  return data;
}

// Export for module bundlers; also usable as a plain script global.
if (typeof module !== "undefined" && module.exports) {
  module.exports = { findMoreLikeThis };
}
