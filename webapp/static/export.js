/* 导出当前修订；不伪造第三方地图或跨域照片的导出结果。 */

function downloadJSON() {
  if (!PLAN) return;
  const blob = new Blob([JSON.stringify(PLAN, null, 2)], { type: "application/json;charset=utf-8" });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = `${PLAN.destination || "旅行"}-r${PLAN.revision || 0}.json`;
  a.click();
  setTimeout(() => URL.revokeObjectURL(a.href), 1000);
}

async function downloadImage() {
  if (!PLAN) return;
  if (typeof html2canvas !== "function") { fail("图片导出库未加载，请检查网络后重试；JSON 与打印仍可用"); return; }
  const target = document.createElement("div");
  target.className = "export-sheet";
  const days = (PLAN.days || []).map(d => `<h2>第 ${esc(d.day)} 天 · ${esc(d.date || "")}</h2>
    <ol>${d.timeline.map(i => `<li>${esc(i.start_time || "待定")} ${esc(i.name)}
    ${i.cost == null ? "（费用待核实）" : `（${i.cost} 元）`}</li>`).join("")}</ol>`).join("");
  target.innerHTML = `<h1>${esc(PLAN.destination || "旅行")} · ${esc(PLAN.days_count || "")} 日行程</h1>
    <p>${esc(PLAN.start_date || "")} — ${esc(PLAN.end_date || "")} · 修订 ${PLAN.revision || 0}</p>
    ${days}<p>图片仅含文字行程，动态地图与第三方照片不在导出范围内。</p>`;
  document.body.appendChild(target);
  try {
    const canvas = await html2canvas(target, { backgroundColor: "#fffdf8", scale: 2 });
    const a = document.createElement("a");
    a.download = `${PLAN.destination || "旅行"}-r${PLAN.revision || 0}.png`;
    a.href = canvas.toDataURL("image/png");
    a.click();
  } finally { target.remove(); }
}

$("#export-json").addEventListener("click", downloadJSON);
$("#export-image").addEventListener("click", () => downloadImage().catch(err => fail(err.message)));
$("#export-pdf").addEventListener("click", () => window.print());
