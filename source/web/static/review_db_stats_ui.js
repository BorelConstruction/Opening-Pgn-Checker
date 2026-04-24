const MIN_SEGMENT_LABEL_PERCENT = 12;
const countFormatter = new Intl.NumberFormat();

export function createReviewDbStatsUi() {
  const panel = document.getElementById("reviewDbStatsPanel");
  const summaryEl = document.getElementById("reviewDbStatsSummary");
  const listEl = document.getElementById("reviewDbStatsList");

  const state = {
    active: false,
    mode: "idle",
    review: null,
  };

  function isReviewMode() {
    return state.active && state.mode === "review";
  }

  function requestIdsEqual(a, b) {
    return Number.isInteger(a) && Number.isInteger(b) && a === b;
  }

  function formatCount(value) {
    if (!Number.isFinite(value)) return "0";
    return countFormatter.format(value);
  }

  function clearPanel() {
    summaryEl.textContent = "";
    listEl.innerHTML = "";
    panel.hidden = true;
  }

  function appendMessage(text, className) {
    const div = document.createElement("div");
    div.className = className;
    div.textContent = text;
    listEl.appendChild(div);
  }

  function buildBarSegment(kind, value, total, label) {
    const segment = document.createElement("div");
    segment.className = `review-db-stats-bar-segment ${kind}`;

    if (!Number.isFinite(value) || value <= 0 || !Number.isFinite(total) || total <= 0) {
      segment.hidden = true;
      return segment;
    }

    const percent = (value / total) * 100;
    segment.style.flexGrow = String(value);
    segment.title = `${label}: ${Math.round(percent)}% (${formatCount(value)})`;

    // Small slices become unreadable quickly, so omit the label instead of clipping noise.
    if (percent >= MIN_SEGMENT_LABEL_PERCENT) {
      segment.textContent = `${Math.round(percent)}%`;
    }

    return segment;
  }

  function renderStats(stats) {
    if (!stats || typeof stats !== "object" || !Array.isArray(stats.moves)) {
      throw new Error("Invalid review DB stats payload");
    }

    if (stats.loading === true) {
      summaryEl.textContent = "Loading…";
      return;
    }

    if (typeof stats.error === "string" && stats.error.trim()) {
      summaryEl.textContent = "Unavailable";
      appendMessage(`Database stats unavailable: ${stats.error}`, "review-db-stats-error");
      return;
    }

    const totalGames = Number.isFinite(stats.totalGames) ? stats.totalGames : 0;
    summaryEl.textContent = `${formatCount(totalGames)} games`;

    if (!stats.moves.length) {
      appendMessage("No database games for this position.", "review-db-stats-empty");
      return;
    }

    for (const move of stats.moves) {
      const item = document.createElement("div");
      item.className = "review-db-stats-item";
      item.title = `${move.san}: ${formatCount(move.gameCount)} games`;

      const moveEl = document.createElement("span");
      moveEl.className = "review-db-stats-move";
      moveEl.textContent = typeof move.san === "string" ? move.san : "";
      item.appendChild(moveEl);

      const countEl = document.createElement("span");
      countEl.className = "review-db-stats-count";
      countEl.textContent = formatCount(move.gameCount);
      item.appendChild(countEl);

      const bar = document.createElement("div");
      bar.className = "review-db-stats-bar";
      bar.appendChild(buildBarSegment("white", move.white, move.gameCount, "White"));
      bar.appendChild(buildBarSegment("draws", move.draws, move.gameCount, "Draws"));
      bar.appendChild(buildBarSegment("black", move.black, move.gameCount, "Black"));
      item.appendChild(bar);

      listEl.appendChild(item);
    }
  }

  function render() {
    listEl.innerHTML = "";

    if (!isReviewMode() || !state.review) {
      clearPanel();
      return;
    }

    panel.hidden = false;
    renderStats(state.review.dbStats);
  }

  function applySrState(sr) {
    state.active = !!sr.active;
    state.mode = sr.mode || "idle";
    state.review = sr.review || null;
    render();
  }

  function applyReviewNavigation(review) {
    if (!isReviewMode() || !state.review) return;
    if (!review || !Object.prototype.hasOwnProperty.call(review, "dbStats")) {
      throw new Error("Invalid review DB stats update");
    }

    if (Object.prototype.hasOwnProperty.call(review, "currentPath")) {
      state.review.currentPath = review.currentPath;
    }
    if (Object.prototype.hasOwnProperty.call(review, "dbStatsRequestId")) {
      state.review.dbStatsRequestId = review.dbStatsRequestId;
    }
    state.review.dbStats = review.dbStats;
    render();
  }

  function applyReviewDbStats(review) {
    if (!isReviewMode() || !state.review) return;
    if (!review || !Object.prototype.hasOwnProperty.call(review, "requestId")) {
      throw new Error("Invalid review DB stats payload");
    }
    if (!Object.prototype.hasOwnProperty.call(review, "dbStats")) {
      throw new Error("Review DB stats payload is missing dbStats");
    }

    // Stats arrive asynchronously after navigation, so only accept the latest request.
    const currentRequestId = state.review.dbStatsRequestId;
    if (!requestIdsEqual(review.requestId, currentRequestId)) {
      return;
    }

    state.review.dbStats = review.dbStats;
    render();
  }

  clearPanel();

  return {
    applySrState,
    applyReviewNavigation,
    applyReviewDbStats,
  };
}
