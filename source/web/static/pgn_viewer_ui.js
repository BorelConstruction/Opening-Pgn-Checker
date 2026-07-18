// import { log } from "./board.js";

export function createPgnViewerUi({ send, onFlipBoard, onResetBoard, getCurrentFen }) {
  const MIN_STUDY_START_RANGE = 0;
  const MAX_STUDY_START_RANGE = 50;
  // log("Creating PGN Viewer UI");
  const srNewBtn = document.getElementById("srNew");
  const srHistoryBtn = document.getElementById("srHistory");
  const srBookmarksBtn = document.getElementById("srBookmarks");
  const srProgressBtn = document.getElementById("srProgress");
  const srHintBtn = document.getElementById("srHint");
  const srStudyFromHereBtn = document.getElementById("srStudyFromHere");
  const srSearchMoveBtn = document.getElementById("srSearchMove");
  const srMarkedMovesBtn = document.getElementById("srMarkedMoves");
  const srBookmarkMoveBtn = document.getElementById("srBookmarkMove");
  const srBookmarkedMovesBtn = document.getElementById("srBookmarkedMoves");
  const srAnalyzeLichessLink = document.getElementById("srAnalyzeLichess");

  const treePanel = document.getElementById("treePanel");
  const guessActions = document.getElementById("guessActions");
  const srGuessGiveUpBtn = document.getElementById("srGuessGiveUp");
  const srGuessFinishBtn = document.getElementById("srGuessFinish");
  const srGuessFinishNewBtn = document.getElementById("srGuessFinishNew");
  const srGuessBookmarkBtn = document.getElementById("srGuessBookmark");
  const srGuessBookmarkMoveBtn = document.getElementById("srGuessBookmarkMove");
  const srGuessSkipBtn = document.getElementById("srGuessSkip");
  const srGuessBlacklistBtn = document.getElementById("srGuessBlacklist");
  const srGuessBlacklistLineBtn = document.getElementById("srGuessBlacklistLine");
  const srAcceptAlternativeBtn = document.getElementById("srAcceptAlternative");
  const reviewTreeOptions = document.getElementById("reviewTreeOptions");
  const srShowAlternativesInput = document.getElementById("srShowAlternatives");
  const treeContainer = document.getElementById("variation-tree");
  const reviewNextMovesPanel = document.getElementById("reviewNextMovesPanel");
  const reviewNextMovesList = document.getElementById("reviewNextMovesList");
  const debugWeightPanel = document.getElementById("debugWeightPanel");
  const debugWeightTree = document.getElementById("debugWeightTree");
  const commentPanel = document.getElementById("commentPanel");
  const commentText = document.getElementById("commentText");

  const searchMoveOverlay = document.getElementById("searchMoveOverlay");
  const searchMovePanel = document.getElementById("searchMovePanel");
  const searchMoveTitle = document.getElementById("searchMoveTitle");
  const searchMoveResults = document.getElementById("searchMoveResults");
  const searchMoveManualBtn = document.getElementById("searchMoveManual");
  const searchMoveCloseBtn = document.getElementById("searchMoveClose");

  const historyOverlay = document.getElementById("historyOverlay");
  const historyPanel = document.getElementById("historyPanel");
  const historyTitle = document.getElementById("historyTitle");
  const historyResults = document.getElementById("historyResults");
  const historyHardestMovesBtn = document.getElementById("historyHardestMoves");
  const historyCloseBtn = document.getElementById("historyClose");
  const progressOverlay = document.getElementById("progressOverlay");
  const progressPanel = document.getElementById("progressPanel");
  const progressTitle = document.getElementById("progressTitle");
  const progressResults = document.getElementById("progressResults");
  const progressCloseBtn = document.getElementById("progressClose");
  const srToast = document.getElementById("srToast");

  const state = {
    active: false,
    mode: "idle", // idle | guess | review
    currentSpecId: null,
    review: null,
    reviewShowsAlternatives: false,
    debugTree: null,
    searchMove: null,
    searchMoveDismissed: false,
    history: null,
    bookmarks: null,
    hardestMoves: null,
    historyOpen: false,
    historyMode: "history",
    historyLoading: false,
    bookmarksLoading: false,
    hardestMovesLoading: false,
    progress: null,
    progressOpen: false,
    progressLoading: false,
    startRange: null,
    bookmarkQueued: false,
    guessMoveBookmarkTarget: null,
    moveBookmarkPending: false,
    pendingAlternative: null,
  };

  const reviewNav = {
    requestedPath: null,
    queuedPath: null,
  };

  let toastTimer = null;
  let toastHideTimer = null;

  function clonePath(path) {
    return Array.isArray(path) ? [...path] : [];
  }

  function pathsEqual(a, b) {
    if (!Array.isArray(a) || !Array.isArray(b)) return false;
    if (a.length !== b.length) return false;
    for (let i = 0; i < a.length; i += 1) {
      if (a[i] !== b[i]) return false;
    }
    return true;
  }

  function resetReviewNavigation() {
    reviewNav.requestedPath = null;
    reviewNav.queuedPath = null;
  }

  function isReviewMode() {
    return state.active && state.mode === "review";
  }

  function getReviewContext() {
    if (!isReviewMode() || !state.review || !state.review.tree) return null;

    const tree = state.review.tree;
    const currentPath = Array.isArray(state.review.currentPath) ? state.review.currentPath : [];
    const node = findNodeAtPath(tree, currentPath);
    if (!node) return null;

    return { tree, currentPath, node };
  }

  function getReviewNextUcis() {
    const ctx = getReviewContext();
    if (!ctx) return [];

    const children = Array.isArray(ctx.node.children) ? ctx.node.children : [];
    return children.map((c) => (c && typeof c.uci === "string" ? c.uci : "")).filter(Boolean);
  }

  function getMoveDests() {
    const ctx = getReviewContext();
    if (!ctx) return {};

    const children = Array.isArray(ctx.node.children) ? ctx.node.children : [];
    const out = {};
    for (const child of children) {
      const uci = child && typeof child.uci === "string" ? child.uci : "";
      if (!uci || uci.length < 4) continue;
      const orig = uci.slice(0, 2);
      const dest = uci.slice(2, 4);
      if (!out[orig]) out[orig] = new Set();
      out[orig].add(dest);
    }

    const obj = {};
    for (const [orig, set] of Object.entries(out)) {
      obj[orig] = Array.from(set).sort();
    }
    return obj;
  }

  function getNavigationBasePath() {
    if (Array.isArray(reviewNav.queuedPath)) return reviewNav.queuedPath;
    if (Array.isArray(reviewNav.requestedPath)) return reviewNav.requestedPath;
    return clonePath(state.review && state.review.currentPath);
  }

  function flushQueuedReviewNavigation() {
    if (!isReviewMode() || !state.review || !state.review.tree) {
      resetReviewNavigation();
      return;
    }
    if (Array.isArray(reviewNav.requestedPath)) return;
    if (!Array.isArray(reviewNav.queuedPath)) return;

    const nextPath = reviewNav.queuedPath;
    reviewNav.queuedPath = null;
    if (pathsEqual(nextPath, state.review.currentPath)) return;

    reviewNav.requestedPath = clonePath(nextPath);
    send({ type: "sr_goto", path: nextPath });
  }

  function queueReviewNavigation(path) {
    if (!isReviewMode() || !state.review || !state.review.tree) return;

    const nextPath = clonePath(path);
    if (Array.isArray(reviewNav.requestedPath)) {
      if (pathsEqual(nextPath, reviewNav.requestedPath) || pathsEqual(nextPath, reviewNav.queuedPath)) {
        return;
      }
      // Keep only the latest requested destination while one server round-trip is in flight.
      reviewNav.queuedPath = nextPath;
      return;
    }

    if (pathsEqual(nextPath, state.review.currentPath)) return;
    reviewNav.requestedPath = nextPath;
    reviewNav.queuedPath = null;
    send({ type: "sr_goto", path: nextPath });
  }

  function acknowledgeReviewNavigation(currentPath) {
    if (!Array.isArray(reviewNav.requestedPath)) return;
    if (!pathsEqual(currentPath, reviewNav.requestedPath)) return;

    reviewNav.requestedPath = null;
    flushQueuedReviewNavigation();
  }

  function handleReviewMove(uci) {
    const ctx = getReviewContext();
    if (!ctx) return false;

    const children = Array.isArray(ctx.node.children) ? ctx.node.children : [];
    const idx = children.findIndex((child) => child && child.uci === uci);
    if (idx < 0) return false;

    queueReviewNavigation([...ctx.currentPath, idx]);
    return true;
  }

  function buildLichessAnalysisUrl(fen) {
    if (typeof fen !== "string" || !fen.trim()) return "";

    // Keep rank separators readable in the path while still escaping spaces and other unsafe characters.
    return `https://lichess.org/analysis/standard/${encodeURIComponent(fen.trim()).replaceAll("%2F", "/")}`;
  }

  function refreshAnalyzeLichessLink() {
    const analysisUrl = buildLichessAnalysisUrl(getCurrentFen());

    srAnalyzeLichessLink.href = analysisUrl || "#";
    srAnalyzeLichessLink.classList.toggle("disabled", !analysisUrl);
    srAnalyzeLichessLink.setAttribute("aria-disabled", analysisUrl ? "false" : "true");
    srAnalyzeLichessLink.tabIndex = analysisUrl ? 0 : -1;
  }

  function closeHistoryOverlay() {
    state.historyOpen = false;
    historyOverlay.hidden = true;
  }

  function closeProgressOverlay() {
    state.progressOpen = false;
    progressOverlay.hidden = true;
  }

  function formatHistoryMoveLabel(move) {
    const moveNumber = typeof move.moveNumber === "number" ? move.moveNumber : 0;
    const san = typeof move.san === "string" ? move.san : "";
    return move.color === "black" ? `${moveNumber}... ${san}` : `${moveNumber}. ${san}`;
  }

  function clamp01(value) {
    if (!Number.isFinite(value)) return 0;
    return Math.max(0, Math.min(1, value));
  }

  function canStudyHistoryEntry(entry) {
    const promptId = entry && entry.promptId;
    return (
      !!promptId &&
      typeof promptId.startFen === "string" &&
      Array.isArray(promptId.moves) &&
      promptId.moves.every((move) => typeof move === "string")
    );
  }

  function formatHardestMoveStats(entry) {
    const wrongCount = Number.isFinite(entry.wrongCount) ? Math.max(0, Math.trunc(entry.wrongCount)) : 0;
    const attemptCount = Number.isFinite(entry.attemptCount) ? Math.max(0, Math.trunc(entry.attemptCount)) : 0;
    return `${wrongCount.toLocaleString()} wrong / ${attemptCount.toLocaleString()} attempts`;
  }

  function currentMoveBookmarkTarget() {
    if (state.active && state.mode === "guess") {
      const target = state.guessMoveBookmarkTarget;
      if (target === null) return null;
      if (
        !target ||
        typeof target.fen !== "string" ||
        !target.fen.trim() ||
        typeof target.uci !== "string" ||
        !target.uci.trim() ||
        typeof target.bookmarked !== "boolean"
      ) {
        throw new Error("Invalid guess move bookmark target");
      }
      return target;
    }

    const ctx = getReviewContext();
    if (!ctx) return null;
    if (!Array.isArray(ctx.currentPath) || ctx.currentPath.length === 0) return null;

    const node = ctx.node;
    if (!node || typeof node.uci !== "string" || !node.uci) return null;
    if (typeof node.parentFen !== "string" || !node.parentFen.trim()) {
      throw new Error("Selected review move is missing parentFen");
    }

    return {
      fen: node.parentFen,
      uci: node.uci,
      bookmarked: !!node.bookmarked,
    };
  }

  function renderHistory() {
    if (!state.historyOpen) {
      historyOverlay.hidden = true;
      return;
    }

    const isHistory = state.historyMode === "history";
    const isBookmarks = state.historyMode === "bookmarks";
    const isHardestMoves = state.historyMode === "hardestMoves";
    if (!isHistory && !isBookmarks && !isHardestMoves) {
      throw new Error(`Unknown history mode: ${state.historyMode}`);
    }

    const payload = isBookmarks ? state.bookmarks : (isHardestMoves ? state.hardestMoves : state.history);
    const entries = payload && Array.isArray(payload.entries) ? payload.entries : [];
    const count = payload && typeof payload.count === "number" ? payload.count : entries.length;
    const title = isBookmarks ? "Bookmarks" : (isHardestMoves ? "Hardest Moves" : "History");
    historyTitle.textContent = `${title} (${count})`;
    historyHardestMovesBtn.disabled = !state.active || state.hardestMovesLoading;
    historyHardestMovesBtn.textContent = state.hardestMovesLoading ? "Loading..." : "Hardest Moves";
    historyResults.innerHTML = "";

    const loadingActive = isBookmarks
      ? state.bookmarksLoading
      : (isHardestMoves ? state.hardestMovesLoading : state.historyLoading);
    if (loadingActive) {
      const loading = document.createElement("div");
      loading.className = "history-empty";
      loading.textContent = isBookmarks
        ? "Loading bookmarks..."
        : (isHardestMoves ? "Loading hardest moves..." : "Loading history...");
      historyResults.appendChild(loading);
      historyOverlay.hidden = false;
      return;
    }

    if (!entries.length) {
      const empty = document.createElement("div");
      empty.className = "history-empty";
      empty.textContent = isBookmarks
        ? "No bookmarked prompt lines yet"
        : (isHardestMoves ? "No wrong moves recorded yet" : "No prompt history yet");
      historyResults.appendChild(empty);
      historyOverlay.hidden = false;
      return;
    }

    for (const entry of entries) {
      const item = document.createElement("div");
      item.className = "history-item";
      item.classList.toggle("bookmarked", !isHardestMoves && !!entry.bookmarked);

      if (isHardestMoves && typeof entry.lastAttemptTime === "number") {
        item.title = `Last attempt: ${new Date(entry.lastAttemptTime * 1000).toLocaleString()}`;
      } else if (typeof entry.promptTime === "number") {
        item.title = new Date(entry.promptTime * 1000).toLocaleString();
      }

      const moves = document.createElement("div");
      moves.className = "history-moves";
      item.appendChild(moves);

      const promptMoves = Array.isArray(entry.moves) ? entry.moves : [];
      for (const move of promptMoves) {
        const moveBtn = document.createElement("button");
        moveBtn.type = "button";
        moveBtn.className = "history-move";
        moveBtn.textContent = formatHistoryMoveLabel(move);
        moveBtn.addEventListener("click", (event) => {
          event.stopPropagation();
          if (Array.isArray(move.path) && isReviewMode()) {
            queueReviewNavigation(move.path);
            return;
          }
          send({
            type: "sr_history_goto",
            path: Array.isArray(move.path) ? move.path : null,
            fen: typeof move.fen === "string" ? move.fen : null,
            san: typeof move.san === "string" ? move.san : "",
          });
        });
        moves.appendChild(moveBtn);
      }

      const meta = document.createElement("div");
      meta.className = "history-meta";

      if (isHistory) {
        const performance = document.createElement("div");
        performance.className = "history-performance";
        if (typeof entry.performance === "number") {
          const hue = 120 * (1 - clamp01(entry.performance));
          performance.textContent = `avg loss ${entry.performance.toFixed(2)}`;
          performance.style.color = `hsl(${hue} 75% 60%)`;
        } else {
          performance.textContent = "avg loss n/a";
        }
        meta.appendChild(performance);
      }

      if (isHardestMoves) {
        const performance = document.createElement("div");
        performance.className = "history-performance";
        performance.textContent = formatHardestMoveStats(entry);
        meta.appendChild(performance);
      }

      if (!isHardestMoves) {
        const bookmarkBtn = document.createElement("button");
        bookmarkBtn.type = "button";
        bookmarkBtn.className = "history-bookmark";
        bookmarkBtn.disabled = !canStudyHistoryEntry(entry);
        bookmarkBtn.textContent = isBookmarks ? "Remove" : (entry.bookmarked ? "Bookmarked" : "Bookmark");
        bookmarkBtn.addEventListener("click", (event) => {
          event.stopPropagation();
          if (bookmarkBtn.disabled) return;
          bookmarkBtn.disabled = true;
          send({
            type: "sr_bookmark_set",
            promptId: entry.promptId,
            bookmarked: isBookmarks ? false : !entry.bookmarked,
            view: state.historyMode,
          });
        });
        meta.appendChild(bookmarkBtn);

        const studyBtn = document.createElement("button");
        studyBtn.type = "button";
        studyBtn.className = "history-study";
        studyBtn.textContent = "Study Again";
        studyBtn.disabled = !canStudyHistoryEntry(entry);
        studyBtn.addEventListener("click", (event) => {
          event.stopPropagation();
          if (studyBtn.disabled) return;
          closeHistoryOverlay();
          send({
            type: "sr_history_study",
            promptId: entry.promptId,
          });
        });
        meta.appendChild(studyBtn);
      }

      item.appendChild(meta);

      historyResults.appendChild(item);
    }

    historyOverlay.hidden = false;
  }

  function progressNumber(progress, key) {
    const value = progress && progress[key];
    if (!Number.isFinite(value)) return 0;
    return Math.max(0, Math.trunc(value));
  }

  function appendProgressStat(parent, label, value) {
    const item = document.createElement("div");
    item.className = "progress-stat";

    const number = document.createElement("div");
    number.className = "progress-stat-number";
    number.textContent = value.toLocaleString();
    item.appendChild(number);

    const text = document.createElement("div");
    text.className = "progress-stat-label";
    text.textContent = label;
    item.appendChild(text);

    parent.appendChild(item);
  }

  function renderProgress() {
    if (!state.progressOpen) {
      progressOverlay.hidden = true;
      return;
    }

    progressTitle.textContent = "Progress";
    progressResults.innerHTML = "";

    if (state.progressLoading) {
      const loading = document.createElement("div");
      loading.className = "progress-empty";
      loading.textContent = "Loading progress...";
      progressResults.appendChild(loading);
      progressOverlay.hidden = false;
      return;
    }

    if (!state.progress) {
      const empty = document.createElement("div");
      empty.className = "progress-empty";
      empty.textContent = "No progress data yet";
      progressResults.appendChild(empty);
      progressOverlay.hidden = false;
      return;
    }

    const learnedMoves = progressNumber(state.progress, "learnedMoves");
    const initiallyMissedMoves = progressNumber(state.progress, "initiallyMissedMoves");
    const shortTermMoves = progressNumber(state.progress, "shortTermMoves");
    const testedMoves = progressNumber(state.progress, "testedMoves");
    const learnableMoves = progressNumber(state.progress, "learnableMoves");

    const summary = document.createElement("div");
    summary.className = "progress-summary";

    const count = document.createElement("div");
    count.className = "progress-count";
    count.textContent = learnedMoves.toLocaleString();
    summary.appendChild(count);

    const label = document.createElement("div");
    label.className = "progress-label";
    label.textContent = learnedMoves === 1 ? "learned move" : "learned moves";
    summary.appendChild(label);
    progressResults.appendChild(summary);

    const stats = document.createElement("div");
    stats.className = "progress-stats";
    appendProgressStat(stats, "started with a miss", initiallyMissedMoves);
    appendProgressStat(stats, "waiting on delay", shortTermMoves);
    appendProgressStat(stats, "tested moves", testedMoves);
    appendProgressStat(stats, "learnable moves", learnableMoves);
    progressResults.appendChild(stats);

    const threshold = Number.isFinite(state.progress.recallThreshold)
      ? Math.round(state.progress.recallThreshold * 100)
      : 95;
    const delaySeconds = Number.isFinite(state.progress.delaySeconds)
      ? Math.round(state.progress.delaySeconds)
      : 0;
    const note = document.createElement("div");
    note.className = "progress-note";
    // note.textContent = `Criterion: recall > ${threshold}%, latest success older than ${delaySeconds}s.`;
    progressResults.appendChild(note);

    progressOverlay.hidden = false;
  }

  function applyHistory(history) {
    if (!history || typeof history.count !== "number" || !Array.isArray(history.entries)) {
      throw new Error("Invalid history payload");
    }
    state.history = history;
    state.historyLoading = false;
    renderHistory();
  }

  function applyBookmarks(bookmarks) {
    if (!bookmarks || typeof bookmarks.count !== "number" || !Array.isArray(bookmarks.entries)) {
      throw new Error("Invalid bookmarks payload");
    }
    state.bookmarks = bookmarks;
    state.bookmarksLoading = false;
    renderHistory();
  }

  function applyHardestMoves(hardestMoves) {
    if (!hardestMoves || typeof hardestMoves.count !== "number" || !Array.isArray(hardestMoves.entries)) {
      throw new Error("Invalid hardest moves payload");
    }
    state.hardestMoves = hardestMoves;
    state.hardestMovesLoading = false;
    renderHistory();
  }

  function applyProgress(progress) {
    if (!progress || typeof progress.learnedMoves !== "number") {
      throw new Error("Invalid progress payload");
    }
    state.progress = progress;
    state.progressLoading = false;
    renderProgress();
  }

  function refreshButtons() {
    const active = !!state.active;
    const isGuess = active && state.mode === "guess";
    const isReview = active && state.mode === "review";
    const canSkipGuess = isGuess && state.currentSpecId === "new";
    const canAcceptAlternative = isGuess && !!state.pendingAlternative;
    srNewBtn.disabled = !active;
    srHistoryBtn.disabled = !active;
    srBookmarksBtn.disabled = !active;
    srProgressBtn.disabled = !active;
    srHintBtn.disabled = !isGuess;
    srStudyFromHereBtn.disabled = !isReview;
    // srSearchMoveBtn.disabled = !isReview;
    srMarkedMovesBtn.disabled = !isReview;
    srBookmarkedMovesBtn.disabled = !isReview;
    srGuessGiveUpBtn.disabled = !isGuess;
    srGuessFinishBtn.disabled = !isGuess;
    srGuessFinishNewBtn.disabled = !isGuess;
    srGuessBookmarkBtn.disabled = !isGuess || !!state.bookmarkQueued;
    srGuessBookmarkBtn.textContent = state.bookmarkQueued ? "Bookmark queued" : "Bookmark";
    srGuessSkipBtn.disabled = !canSkipGuess;
    srGuessBlacklistBtn.disabled = !isGuess;
    srGuessBlacklistLineBtn.disabled = !isGuess;
    const bookmarkTarget = currentMoveBookmarkTarget();
    for (const button of [srGuessBookmarkMoveBtn, srBookmarkMoveBtn]) {
      button.disabled = !bookmarkTarget || state.moveBookmarkPending;
      button.textContent = bookmarkTarget && bookmarkTarget.bookmarked ? "Unbookmark move" : "Bookmark move";
      button.classList.toggle("active", !!bookmarkTarget && bookmarkTarget.bookmarked);
    }
    srAcceptAlternativeBtn.hidden = !canAcceptAlternative;
    srAcceptAlternativeBtn.disabled = !canAcceptAlternative;
    srShowAlternativesInput.disabled = !isReview;
    refreshAnalyzeLichessLink();
  }

  function refreshReviewTreeOptions() {
    const isReview = isReviewMode();
    reviewTreeOptions.hidden = !isReview;
    srShowAlternativesInput.checked = !!state.reviewShowsAlternatives;
    srShowAlternativesInput.disabled = !isReview;
  }

  function hideSkipToast() {
    if (toastTimer !== null) {
      clearTimeout(toastTimer);
      toastTimer = null;
    }
    if (toastHideTimer !== null) {
      clearTimeout(toastHideTimer);
      toastHideTimer = null;
    }
    srToast.classList.remove("visible");
    srToast.hidden = true;
  }

  function showSkipToast() {
    hideSkipToast();
    srToast.textContent = "Will be skipping this move from now on";
    srToast.hidden = false;
    // Wait one frame before toggling visibility so the CSS transition runs reliably.
    requestAnimationFrame(() => {
      srToast.classList.add("visible");
    });
    toastTimer = setTimeout(() => {
      srToast.classList.remove("visible");
      toastHideTimer = setTimeout(() => {
        srToast.hidden = true;
        toastHideTimer = null;
      }, 220);
      toastTimer = null;
    }, 2000);
  }

  function closeSearchMoveOverlay() {
    state.searchMoveDismissed = true;
    searchMoveOverlay.hidden = true;
  }

  function promptSearchMoveNotation() {
    const notation = prompt('Enter algebraic notation and the side, such as "Nc3xd5 W".');
    if (notation === null) return;

    const trimmedNotation = notation.trim();
    if (!trimmedNotation) {
      alert("Enter a move notation.");
      return;
    }

    state.searchMoveDismissed = false;
    send({ type: "sr_search_move", notation: trimmedNotation });
  }

  function normalizeMoveMark(mark) {
    if (mark === "blacklist" || mark === "skip" || mark === "bookmark") return mark;
    throw new Error(`Unknown move mark: ${mark}`);
  }

  function markedMoveLabel(mark) {
    const normalizedMark = normalizeMoveMark(mark);
    if (normalizedMark === "blacklist") return "Blacklisted";
    if (normalizedMark === "skip") return "Skipped";
    return "Bookmarked";
  }

  function markedMoveActionLabel(mark) {
    const normalizedMark = normalizeMoveMark(mark);
    if (normalizedMark === "blacklist") return "Unblacklist";
    if (normalizedMark === "skip") return "Stop skipping";
    return "Unbookmark";
  }

  function renderSearchMove() {
    const hasSearch = isReviewMode() && state.searchMove && Array.isArray(state.searchMove.results);
    if (!hasSearch) {
      searchMoveOverlay.hidden = true;
      state.searchMoveDismissed = false;
      searchMoveTitle.textContent = "Search move";
      searchMoveResults.innerHTML = "";
      return;
    }

    const kind = typeof state.searchMove.kind === "string" ? state.searchMove.kind : "searchMove";
    const isMarkedMoves = kind === "markedMoves";
    const results = state.searchMove.results || [];
    const query = state.searchMove.query || {};
    const queryMove = typeof query.move === "string" ? query.move : "";
    const count = typeof state.searchMove.count === "number" ? state.searchMove.count : results.length;

    if (isMarkedMoves) {
      const title = typeof query.title === "string" && query.title ? query.title : "Marked moves";
      searchMoveTitle.textContent = `${title} (${count})`;
    } else {
      searchMoveTitle.textContent = queryMove ? `Search move: ${queryMove} (${count})` : `Search move (${count})`;
    }
    searchMoveResults.innerHTML = "";

    const currentPath = state.review && Array.isArray(state.review.currentPath) ? state.review.currentPath : [];

    if (!results.length) {
      const empty = document.createElement("div");
      empty.className = "search-empty";
      empty.textContent = typeof state.searchMove.emptyMessage === "string"
        ? state.searchMove.emptyMessage
        : "No occurrences found";
      searchMoveResults.appendChild(empty);
      searchMoveOverlay.hidden = state.searchMoveDismissed;
      return;
    }

    for (const item of results) {
      const path = Array.isArray(item.path) ? item.path : [];
      const isCurrent = pathsEqual(path, currentPath);

      const div = document.createElement("div");
      div.className = `search-item ${isCurrent ? "current" : ""}`;
      div.addEventListener("click", (event) => {
        event.stopPropagation();
        queueReviewNavigation(path);
      });

      if (isMarkedMoves) {
        const mark = normalizeMoveMark(item.mark);
        const markSpan = document.createElement("span");
        markSpan.className = `search-mark ${mark}`;
        markSpan.textContent = markedMoveLabel(mark);
        div.appendChild(markSpan);
      } else {
        const startDots = document.createElement("span");
        startDots.className = "search-ellipsis";
        startDots.textContent = "…";
        div.appendChild(startDots);
      }

      const prevSpan = document.createElement("span");
      const prevText = typeof item.prev === "string" && item.prev.trim() ? item.prev : "start";
      prevSpan.className = `search-prev ${item.matchPrev ? "match" : ""}`;
      prevSpan.textContent = prevText;
      div.appendChild(prevSpan);

      const moveSpan = document.createElement("span");
      moveSpan.className = "search-move";
      moveSpan.textContent = typeof item.move === "string" ? item.move : "";
      div.appendChild(moveSpan);

      const nextSpan = document.createElement("span");
      const nextText = typeof item.next === "string" && item.next.trim() ? item.next : ".";
      nextSpan.className = `search-next ${item.matchNext ? "match" : ""}`;
      nextSpan.textContent = nextText;
      div.appendChild(nextSpan);

      if (isMarkedMoves) {
        const action = document.createElement("button");
        action.type = "button";
        action.className = "search-action";
        action.textContent = markedMoveActionLabel(item.mark);
        const mark = normalizeMoveMark(item.mark);
        action.disabled = !mark || typeof item.fen !== "string" || typeof item.uci !== "string";
        action.addEventListener("click", (event) => {
          event.stopPropagation();
          if (action.disabled) return;
          action.disabled = true;
          send({
            type: "sr_unmark_move",
            mark: item.mark,
            fen: item.fen,
            uci: item.uci,
          });
        });
        div.appendChild(action);
      } else {
        const similaritySpan = document.createElement("span");
        similaritySpan.className = "search-similarity";
        const similarity = typeof item.similarity === "number" ? item.similarity : 0;
        const distance = typeof item.distance === "number" ? item.distance : 0;
        similaritySpan.textContent = `${Math.round(similarity * 100)}% d=${distance.toFixed(2)}`;
        div.appendChild(similaritySpan);

        const endDots = document.createElement("span");
        endDots.className = "search-ellipsis";
        endDots.textContent = "…";
        div.appendChild(endDots);
      }

      searchMoveResults.appendChild(div);
    }

    searchMoveOverlay.hidden = state.searchMoveDismissed;
  }

  function promptStudyStartRange() {
    const defaultValue = Number.isInteger(state.startRange) ? String(state.startRange) : "5";

    while (true) {
      // Reprompt until the user gives a valid integer or cancels so the server only receives usable values.
      const rawValue = prompt(
        "How far should prompts start? Enter maximum number of moves from this position.",
        defaultValue,
      );
      if (rawValue === null) return null;

      const parsedValue = Number(rawValue.trim());
      if (
        Number.isInteger(parsedValue) &&
        parsedValue >= MIN_STUDY_START_RANGE &&
        parsedValue <= MAX_STUDY_START_RANGE
      ) {
        return parsedValue;
      }

      alert(`Enter an integer from ${MIN_STUDY_START_RANGE} to ${MAX_STUDY_START_RANGE}.`);
    }
  }

  function findNodeAtPath(tree, path) {
    let node = tree;
    for (const idx of path) {
      if (!node || !Array.isArray(node.children) || idx < 0 || idx >= node.children.length) {
        return null;
      }
      node = node.children[idx];
    }
    return node;
  }

  function getArrowNavigationPath(tree, currentPath, direction) {
    const node = findNodeAtPath(tree, currentPath || []);
    if (!node) return currentPath || [];

    if (direction === 'left') {
      return (currentPath && currentPath.length > 0) ? currentPath.slice(0, -1) : currentPath || [];
    }

    if (direction === 'right') {
      if (node.children && node.children.length > 0) {
        return [...(currentPath || []), 0];
      }
      return currentPath || [];
    }

    if (!currentPath || currentPath.length === 0) {
      return currentPath || [];
    }

    const parentPath = currentPath.slice(0, -1);
    const siblingIndex = currentPath[currentPath.length - 1];
    const parentNode = findNodeAtPath(tree, parentPath);
    if (!parentNode || !Array.isArray(parentNode.children)) {
      return currentPath || [];
    }

    if (direction === 'up') {
      return siblingIndex > 0 ? [...parentPath, siblingIndex - 1] : currentPath || [];
    }

    if (direction === 'down') {
      return siblingIndex < parentNode.children.length - 1 ? [...parentPath, siblingIndex + 1] : currentPath || [];
    }

    return currentPath || [];
  }

  function formatTreeMoveLabel(node) {
    if (!node || typeof node.san !== "string" || !node.san) {
      throw new Error("Cannot format a tree move without SAN");
    }

    if (!Number.isInteger(node.moveNumber)) {
      throw new Error("Cannot format a tree move without a move number");
    }

    if (node.color !== "white" && node.color !== "black") {
      throw new Error("Cannot format a tree move without a valid color");
    }

    const prefix = node.color === "black" ? `${node.moveNumber}...` : `${node.moveNumber}.`;
    const nags = formatNodeNags(node);
    return nags ? `${prefix} ${node.san} ${nags}` : `${prefix} ${node.san}`;
  }

  function formatNodeNags(node) {
    if (!node) return "";
    if (!Object.prototype.hasOwnProperty.call(node, "nags")) return "";
    if (!Array.isArray(node.nags)) {
      throw new Error("Tree node nags must be an array when present");
    }

    return node.nags
      .filter((nag) => typeof nag === "string" && nag.trim())
      .join(" ");
  }

  function handleKeyNavigation(event) {
    if (event.key === "Escape") {
      if (!searchMoveOverlay.hidden) {
        closeSearchMoveOverlay();
        return;
      }
      if (!historyOverlay.hidden) {
        closeHistoryOverlay();
        return;
      }
    }

    if (!isReviewMode() || !state.review || !state.review.tree) return;
    if (event.target instanceof Element) {
      const tagName = event.target.tagName.toLowerCase();
      if (tagName === 'input' || tagName === 'textarea' || event.target.isContentEditable) {
        return;
      }
    }

    let direction = null;
    if (event.key === 'ArrowLeft') {
      direction = 'left';
    } else if (event.key === 'ArrowRight') {
      direction = 'right';
    } else if (event.key === 'ArrowUp') {
      direction = 'up';
    } else if (event.key === 'ArrowDown') {
      direction = 'down';
    } else {
      return;
    }

    // Keep arrow keys owned by review navigation even when the path cannot change.
    event.preventDefault();

    const currentPath = getNavigationBasePath();
    const newPath = getArrowNavigationPath(state.review.tree, currentPath, direction);
    if (!pathsEqual(newPath, currentPath)) {
      queueReviewNavigation(newPath);
    }
  }

  function commonPrefixLength(a, b) {
    let i = 0;
    while (i < a.length && i < b.length && a[i] === b[i]) i++;
    return i;
  }

  const MIN_FORWARD_PLIES_SHOWN = 7;
  const TREE_VIEWPORT_MARGIN_RATIO = 0.2;
  const TREE_VIEWPORT_MIN_MARGIN_PX = 24;
  let treeViewportSyncPending = false;

  function pathIsPrefix(prefix, path) {
    if (!Array.isArray(prefix) || !Array.isArray(path)) return false;
    if (prefix.length > path.length) return false;
    for (let i = 0; i < prefix.length; i++) {
      if (prefix[i] !== path[i]) return false;
    }
    return true;
  }

  function getPathRelation(nodePath, currentPath) {
    if (pathsEqual(nodePath, currentPath)) return "current";
    if (pathIsPrefix(nodePath, currentPath)) return "past";
    if (pathIsPrefix(currentPath, nodePath)) return "future";
    return "branch";
  }

  function ensureCurrentTreeRowVisible() {
    const currentRow = treeContainer.querySelector(".tree-node.current > .tree-row");
    if (!(currentRow instanceof HTMLElement)) return;

    const panelRect = treeContainer.getBoundingClientRect();
    const rowRect = currentRow.getBoundingClientRect();
    const margin = Math.max(
      TREE_VIEWPORT_MIN_MARGIN_PX,
      Math.floor(treeContainer.clientHeight * TREE_VIEWPORT_MARGIN_RATIO),
    );
    const visibleTop = panelRect.top + margin;
    const visibleBottom = panelRect.bottom - margin;

    if (rowRect.top < visibleTop) {
      treeContainer.scrollTop -= visibleTop - rowRect.top;
      return;
    }

    if (rowRect.bottom > visibleBottom) {
      treeContainer.scrollTop += rowRect.bottom - visibleBottom;
    }
  }

  function queueTreeViewportSync() {
    if (treeViewportSyncPending) return;

    // The tree DOM is rebuilt on each review navigation, so wait for layout before correcting scroll.
    treeViewportSyncPending = true;
    requestAnimationFrame(() => {
      treeViewportSyncPending = false;
      ensureCurrentTreeRowVisible();
    });
  }

  function clearReviewNextMoves() {
    reviewNextMovesPanel.hidden = true;
    reviewNextMovesList.innerHTML = "";
  }

  function renderReviewNextMoves() {
    if (!isReviewMode() || !state.review || !state.review.tree) {
      clearReviewNextMoves();
      return;
    }

    const ctx = getReviewContext();
    if (!ctx) {
      clearReviewNextMoves();
      return;
    }

    const children = Array.isArray(ctx.node.children) ? ctx.node.children : [];
    reviewNextMovesList.innerHTML = "";
    reviewNextMovesPanel.hidden = false;

    if (!children.length) {
      const empty = document.createElement("div");
      empty.className = "review-next-moves-empty";
      empty.textContent = "End of line";
      reviewNextMovesList.appendChild(empty);
      return;
    }

    for (let i = 0; i < children.length; i += 1) {
      const child = children[i];
      const button = document.createElement("button");
      button.type = "button";
      button.className = "review-next-move";
      button.classList.toggle("bookmarked", !!child.bookmarked);
      button.textContent = formatTreeMoveLabel(child);
      button.addEventListener("click", (event) => {
        event.stopPropagation();
        queueReviewNavigation([...ctx.currentPath, i]);
      });
      reviewNextMovesList.appendChild(button);
    }
  }

  function renderTreeNode(node, nodePath = [], basePath = [], globalCurrentPath = []) {
    const globalPath = [...basePath, ...nodePath];
    const normalizedCurrentPath = Array.isArray(globalCurrentPath) ? globalCurrentPath : [];
    const isCurrent = pathsEqual(globalPath, globalCurrentPath);
    const pathRelation = getPathRelation(globalPath, normalizedCurrentPath);
    const isBookmarked = !!node.bookmarked;
    const div = document.createElement("div");
    div.className = `tree-node ${pathRelation} ${isCurrent ? 'current' : ''} ${isBookmarked ? 'bookmarked' : ''}`.trim();
    div.dataset.path = JSON.stringify(globalPath);

    const row = document.createElement("div");
    row.className = "tree-row";
    div.appendChild(row);

    if (node.san) {
      // This is a move node
      const moveSpan = document.createElement("span");
      moveSpan.className = "tree-move";
      moveSpan.textContent = formatTreeMoveLabel(node);
      row.addEventListener("click", (event) => {
        event.stopPropagation();
        queueReviewNavigation(globalPath);
      });
      row.appendChild(moveSpan);
      if (isBookmarked) {
        const bookmarkSpan = document.createElement("span");
        bookmarkSpan.className = "tree-bookmark";
        bookmarkSpan.textContent = "Bookmarked";
        row.appendChild(bookmarkSpan);
      }
    } else {
      // This is a position node (root)
      const posSpan = document.createElement("span");
      posSpan.className = "tree-position";
      posSpan.textContent = `Position (ply ${node.ply})`;
      row.appendChild(posSpan);
    }

    const cpl = commonPrefixLength(globalPath, normalizedCurrentPath);
    const depthFromCurrent = globalPath.length - cpl;

    const isPastOrCurrent = pathRelation === "past" || pathRelation === "current";

    let shouldRenderChildren = true;

    if (!isPastOrCurrent) {
      // Keep a usable click target window in front of the current node when the user backs up.
      if (depthFromCurrent >= MIN_FORWARD_PLIES_SHOWN) {
        shouldRenderChildren = false;
      }
    }

    if (node.children && node.children.length > 0 && shouldRenderChildren) {
      const childrenDiv = document.createElement("div");
      childrenDiv.className = "tree-children";
      for (let i = 0; i < node.children.length; i++) {
        const child = node.children[i];
        childrenDiv.appendChild(renderTreeNode(child, [...nodePath, i], basePath, globalCurrentPath));
      }
      div.appendChild(childrenDiv);
    }

    return div;
  }

  function renderReviewTree() {
    if (state.active && state.mode === "guess") {
      treePanel.classList.remove("review-mode");
      treePanel.style.display = "block";
      guessActions.hidden = false;
      reviewTreeOptions.hidden = true;
      treeContainer.innerHTML = "";
      clearReviewNextMoves();
      return;
    }

    if (isReviewMode() && state.review && state.review.tree) {
      treePanel.classList.add("review-mode");
      treePanel.style.display = "flex";
      guessActions.hidden = true;
      refreshReviewTreeOptions();
      treeContainer.innerHTML = "";
      const tree = state.review.tree;
      const globalCurrentPath = Array.isArray(state.review.currentPath) ? state.review.currentPath : [];
      const viewRootPath = Array.isArray(state.review.viewRootPath) ? state.review.viewRootPath : [];
      const viewRootNode = findNodeAtPath(tree, viewRootPath) || tree;
      const treeRoot = renderTreeNode(viewRootNode, [], viewRootPath, globalCurrentPath);
      treeContainer.appendChild(treeRoot);
      renderReviewNextMoves();
      queueTreeViewportSync();
      return;
    }

    treePanel.classList.remove("review-mode");
    treePanel.style.display = "none";
    guessActions.hidden = true;
    reviewTreeOptions.hidden = true;
    treeContainer.innerHTML = "";
    clearReviewNextMoves();
  }

  function hideDebugWeightTree() {
    debugWeightPanel.hidden = true;
    debugWeightTree.innerHTML = "";
  }

  function formatDebugMetric(value) {
    if (!Number.isFinite(value)) return "n/a";
    return value.toFixed(2);
  }

  const DEBUG_SVG_NS = "http://www.w3.org/2000/svg";
  const DEBUG_NODE_MIN_WIDTH = 26;
  const DEBUG_NODE_MAX_WIDTH = 94;
  const DEBUG_NODE_HEIGHT = 22;
  const DEBUG_NODE_PADDING_X = 8;
  const DEBUG_EDGE_MIN_GAP = 18;
  const DEBUG_EDGE_LABEL_MARGIN = 12;
  const DEBUG_ROW_GAP = 12;
  const DEBUG_CANVAS_PADDING = 10;
  const DEBUG_NODE_CHAR_WIDTH = 6.2;
  const DEBUG_EDGE_LABEL_CHAR_WIDTH = 5.8;
  const DEBUG_EDGE_LABEL_LINE_HEIGHT = 11;
  const DEBUG_EDGE_LABEL_PADDING_X = 6;
  const DEBUG_EDGE_LABEL_PADDING_Y = 4;

  function debugSvgEl(name, attrs = {}) {
    const el = document.createElementNS(DEBUG_SVG_NS, name);
    for (const [key, value] of Object.entries(attrs)) {
      el.setAttribute(key, String(value));
    }
    return el;
  }

  function debugCompactNodeLabel(node) {
    if (node.kind === "move") {
      return typeof node.san === "string" && node.san ? node.san : "?";
    }
    if (typeof node.label === "string" && node.label) {
      if (node.label === "Study root") return "Root";
      if (node.label === "Active review position") return "Review";
      return node.label.length > 10 ? node.label.slice(0, 10) : node.label;
    }
    return "Pos";
  }

  function debugNodeTitle(node) {
    const parts = [];
    if (node.kind === "move") {
      parts.push(formatTreeMoveLabel(node));
      if (typeof node.uci === "string" && node.uci) {
        parts.push(node.uci);
      }
    } else {
      parts.push(debugCompactNodeLabel(node));
    }
    if (node.isAnchor) parts.push("anchor");
    if (node.isCurrent) parts.push("current");
    if (node.onPromptPath) parts.push("prompt path");
    if (node.showPriorityLabels) {
      parts.push(formatDebugMetric(node.moveFreq));
      parts.push(formatDebugMetric(node.recallProbability));
    }
    return parts.join(" | ");
  }

  function debugNodeWidth(node) {
    const label = debugCompactNodeLabel(node);
    const naturalWidth = label.length * DEBUG_NODE_CHAR_WIDTH + DEBUG_NODE_PADDING_X * 2;
    return Math.max(DEBUG_NODE_MIN_WIDTH, Math.min(DEBUG_NODE_MAX_WIDTH, naturalWidth));
  }

  function debugEdgeLabelMetrics(node) {
    const lines = [];
    if (node.showPriorityLabels) {
      lines.push(formatDebugMetric(node.moveFreq));
      lines.push(formatDebugMetric(node.recallProbability));
    }

    if (!lines.length) {
      return { lines, width: 0, height: 0 };
    }

    return {
      lines,
      width: Math.max(...lines.map((line) => line.length)) * DEBUG_EDGE_LABEL_CHAR_WIDTH + DEBUG_EDGE_LABEL_PADDING_X * 2,
      height: lines.length * DEBUG_EDGE_LABEL_LINE_HEIGHT + DEBUG_EDGE_LABEL_PADDING_Y * 2,
    };
  }

  function debugEdgeHorizontalGap(node) {
    const metrics = debugEdgeLabelMetrics(node);
    if (!metrics.lines.length) {
      return DEBUG_EDGE_MIN_GAP;
    }
    return Math.max(DEBUG_EDGE_MIN_GAP, metrics.width + DEBUG_EDGE_LABEL_MARGIN);
  }

  function measureDebugTreeLayout(node) {
    const children = Array.isArray(node.children) ? node.children : [];
    const childLayouts = children.map(measureDebugTreeLayout);
    const nodeWidth = debugNodeWidth(node);
    const childrenHeight = childLayouts.length
      ? childLayouts.reduce((sum, child) => sum + child.height, 0) + DEBUG_ROW_GAP * (childLayouts.length - 1)
      : 0;
    return {
      node,
      nodeWidth,
      childLayouts,
      childrenHeight,
      width: childLayouts.length
        ? Math.max(...childLayouts.map((child) => nodeWidth + debugEdgeHorizontalGap(child.node) + child.width))
        : nodeWidth,
      height: Math.max(DEBUG_NODE_HEIGHT, childrenHeight || 0),
    };
  }

  // Layout is computed top-down so parents stay vertically centered above their subtree.
  function placeDebugTreeLayout(layout, x, yTop, scene) {
    const nodeY = yTop + (layout.height - DEBUG_NODE_HEIGHT) / 2;
    const placedNode = {
      node: layout.node,
      x,
      y: nodeY,
      width: layout.nodeWidth,
      height: DEBUG_NODE_HEIGHT,
      centerY: nodeY + DEBUG_NODE_HEIGHT / 2,
    };
    scene.nodes.push(placedNode);

    if (!layout.childLayouts.length) {
      return placedNode;
    }

    let childTop = yTop + (layout.height - layout.childrenHeight) / 2;
    for (const childLayout of layout.childLayouts) {
      const childGap = debugEdgeHorizontalGap(childLayout.node);
      const childNode = placeDebugTreeLayout(
        childLayout,
        x + layout.nodeWidth + childGap,
        childTop,
        scene,
      );
      scene.edges.push({ parent: placedNode, child: childNode, node: childLayout.node });
      childTop += childLayout.height + DEBUG_ROW_GAP;
    }
    return placedNode;
  }

  function debugNodeFill(node) {
    if (node.isCurrent) return "rgba(56, 189, 248, 0.18)";
    if (node.onPromptPath) return "rgba(34, 197, 94, 0.14)";
    if (node.isAnchor) return "rgba(248, 250, 252, 0.08)";
    return "rgba(2, 6, 23, 0.55)";
  }

  function debugNodeStroke(node) {
    if (node.isCurrent) return "rgba(56, 189, 248, 0.95)";
    if (node.onPromptPath) return "rgba(34, 197, 94, 0.8)";
    if (node.isAnchor) return "rgba(229, 231, 235, 0.45)";
    return "rgba(255, 255, 255, 0.08)";
  }

  function debugEdgeStroke(node) {
    if (node.isCurrent) return "#38bdf8";
    if (node.onPromptPath) return "#22c55e";
    if (node.onCurrentPath) return "#7dd3fc";
    return "#475569";
  }

  function debugEdgeStrokeWidth(node) {
    if (node.isCurrent || node.onPromptPath) return 2.4;
    return 1.5;
  }

  function debugEdgeLabelLines(node) {
    return debugEdgeLabelMetrics(node).lines;
  }

  function renderDebugEdge(sceneLayer, edge) {
    const parentRightX = edge.parent.x + edge.parent.width;
    const childLeftX = edge.child.x;
    const midX = parentRightX + (childLeftX - parentRightX) / 2;
    const path = debugSvgEl("path", {
      class: "debug-edge-path",
      d: `M ${parentRightX} ${edge.parent.centerY} H ${midX} V ${edge.child.centerY} H ${childLeftX}`,
      stroke: debugEdgeStroke(edge.node),
      "stroke-width": debugEdgeStrokeWidth(edge.node),
      fill: "none",
      "stroke-linecap": "round",
      "stroke-linejoin": "round",
    });
    sceneLayer.appendChild(path);

    const labelMetrics = debugEdgeLabelMetrics(edge.node);
    if (!labelMetrics.lines.length) {
      return;
    }
    const { lines, width: labelWidth, height: labelHeight } = labelMetrics;
    const labelX = midX - labelWidth / 2;
    const labelY = (edge.parent.centerY + edge.child.centerY) / 2 - labelHeight / 2;

    const labelGroup = debugSvgEl("g", { class: "debug-edge-label-group" });

    labelGroup.appendChild(
      debugSvgEl("rect", {
        class: "debug-edge-label-box",
        x: labelX,
        y: labelY,
        width: labelWidth,
        height: labelHeight,
        rx: 7,
        ry: 7,
      }),
    );

    lines.forEach((line, index) => {
      const text = debugSvgEl("text", {
        class: `debug-edge-label ${index === 0 ? "primary" : "secondary"}`,
        x: labelX + labelWidth / 2,
        y: labelY + DEBUG_EDGE_LABEL_PADDING_Y + 10 + index * DEBUG_EDGE_LABEL_LINE_HEIGHT,
        "text-anchor": "middle",
      });
      text.textContent = line;
      labelGroup.appendChild(text);
    });

    sceneLayer.appendChild(labelGroup);
  }

  function renderDebugNode(sceneLayer, placedNode) {
    const group = debugSvgEl("g", { class: "debug-node-group" });
    const title = debugSvgEl("title");
    title.textContent = debugNodeTitle(placedNode.node);
    group.appendChild(title);

    group.appendChild(
      debugSvgEl("rect", {
        class: "debug-node-box",
        x: placedNode.x,
        y: placedNode.y,
        width: placedNode.width,
        height: placedNode.height,
        rx: 10,
        ry: 10,
        fill: debugNodeFill(placedNode.node),
        stroke: debugNodeStroke(placedNode.node),
        "stroke-width": placedNode.node.isCurrent ? 1.8 : 1.2,
      }),
    );

    const label = debugSvgEl("text", {
      class: "debug-node-label",
      x: placedNode.x + placedNode.width / 2,
      y: placedNode.y + DEBUG_NODE_HEIGHT / 2 + 0.5,
      "text-anchor": "middle",
    });
    label.textContent = debugCompactNodeLabel(placedNode.node);
    group.appendChild(label);

    sceneLayer.appendChild(group);
  }

  function debugViewportFocusNode(scene) {
    return (
      scene.nodes.find((node) => node.node && node.node.isCurrent) ||
      scene.nodes.find((node) => node.node && node.node.isAnchor) ||
      scene.nodes[0] ||
      null
    );
  }

  function focusDebugWeightViewport(scene) {
    const focusNode = debugViewportFocusNode(scene);
    if (!focusNode) {
      return;
    }

    requestAnimationFrame(() => {
      const leftMargin = 18;
      debugWeightTree.scrollLeft = Math.max(0, focusNode.x - leftMargin);
    });
  }

  function renderDebugWeightTree() {
    const debug = state.debugTree;
    if (!debug) {
      hideDebugWeightTree();
      return;
    }

    debugWeightPanel.hidden = false;
    const debugTitle = typeof debug.title === "string" && debug.title ? debug.title : "Selection / performance visualizer";
    debugWeightTree.innerHTML = "";

    if (typeof debug.error === "string" && debug.error) {
      const error = document.createElement("div");
      error.className = "debug-weight-error";
      error.textContent = debug.error;
      debugWeightTree.appendChild(error);
      return;
    }

    if (!debug.tree) {
      const empty = document.createElement("div");
      empty.className = "debug-weight-empty";
      empty.textContent = "No debug tree available.";
      debugWeightTree.appendChild(empty);
      return;
    }

    const layout = measureDebugTreeLayout(debug.tree);
    const scene = { nodes: [], edges: [] };
    placeDebugTreeLayout(layout, DEBUG_CANVAS_PADDING, DEBUG_CANVAS_PADDING, scene);

    const svgWidth = layout.width + DEBUG_CANVAS_PADDING * 2;
    const svgHeight = layout.height + DEBUG_CANVAS_PADDING * 2;
    const svg = debugSvgEl("svg", {
      class: "debug-weight-svg",
      viewBox: `0 0 ${svgWidth} ${svgHeight}`,
      width: svgWidth,
      height: svgHeight,
      role: "img",
      "aria-label": debugTitle,
    });

    const edgeLayer = debugSvgEl("g", { class: "debug-edge-layer" });
    scene.edges.forEach((edge) => renderDebugEdge(edgeLayer, edge));
    svg.appendChild(edgeLayer);

    const nodeLayer = debugSvgEl("g", { class: "debug-node-layer" });
    scene.nodes.forEach((node) => renderDebugNode(nodeLayer, node));
    svg.appendChild(nodeLayer);

    debugWeightTree.appendChild(svg);
    focusDebugWeightViewport(scene);
  }

  function renderReviewComment() {
    if (!isReviewMode()) {
      commentPanel.hidden = true;
      commentText.textContent = "";
      commentText.classList.remove("empty");
      return;
    }

    const ctx = getReviewContext();
    if (!ctx) {
      commentPanel.hidden = true;
      commentText.textContent = "";
      commentText.classList.remove("empty");
      return;
    }

    const nags = formatNodeNags(ctx.node);
    const comment = ctx.node && typeof ctx.node.comment === "string" ? ctx.node.comment.trim() : "";
    const sections = [];
    if (nags) {
      sections.push(`NAGs: ${nags}`);
    }
    if (comment) {
      sections.push(comment);
    }
    commentPanel.hidden = false;
    commentText.textContent = sections.join("\n\n") || "No PGN comment or NAGs for this node.";
    commentText.classList.toggle("empty", sections.length === 0);
  }

  function applySrState(sr) {
    state.active = !!sr.active;
    state.mode = sr.mode || "idle";
    state.currentSpecId = typeof sr.currentSpecId === "string" ? sr.currentSpecId : null;
    state.review = sr.review || null;
    state.reviewShowsAlternatives = !!sr.reviewShowsAlternatives;
    state.debugTree = sr.debugTree || null;
    state.searchMove = sr.searchMove || null;
    state.startRange = Number.isInteger(sr.startRange) ? sr.startRange : null;
    state.bookmarkQueued = !!sr.bookmarkQueued;
    state.guessMoveBookmarkTarget = sr.guessMoveBookmarkTarget || null;
    state.moveBookmarkPending = false;
    state.pendingAlternative = sr.pendingAlternative || null;
    if (!state.active) {
      state.historyOpen = false;
      state.historyLoading = false;
      state.bookmarksLoading = false;
      state.hardestMovesLoading = false;
      state.progressOpen = false;
      state.progressLoading = false;
    }
    resetReviewNavigation();

    refreshButtons();
    refreshReviewTreeOptions();
    renderReviewTree();
    renderDebugWeightTree();
    renderReviewComment();
    renderSearchMove();
    renderHistory();
    renderProgress();
  }

  function applyReviewNavigation(review) {
    if (!isReviewMode() || !state.review || !state.review.tree) return;
    if (!review || !Array.isArray(review.currentPath) || !Array.isArray(review.viewRootPath)) {
      throw new Error("Invalid review navigation payload");
    }

    state.review.currentPath = clonePath(review.currentPath);
    state.review.viewRootPath = clonePath(review.viewRootPath);
    state.moveBookmarkPending = false;
    if (Object.prototype.hasOwnProperty.call(review, "dbStatsRequestId")) {
      state.review.dbStatsRequestId = review.dbStatsRequestId;
    }
    if (Object.prototype.hasOwnProperty.call(review, "dbStats")) {
      state.review.dbStats = review.dbStats || null;
    }
    if (Object.prototype.hasOwnProperty.call(review, "debugTree")) {
      state.debugTree = review.debugTree || null;
    }

    refreshButtons();
    refreshReviewTreeOptions();
    renderReviewTree();
    renderDebugWeightTree();
    renderReviewComment();
    renderSearchMove();
    acknowledgeReviewNavigation(state.review.currentPath);
  }

  function handleFlip() {
    onFlipBoard();
  }

  function handleReset() {
    onResetBoard();
  }

  document.addEventListener("keydown", handleKeyNavigation);
  window.addEventListener("resize", () => {
    if (!isReviewMode()) return;
    queueTreeViewportSync();
  });
  searchMoveOverlay.addEventListener("click", closeSearchMoveOverlay);
  searchMovePanel.addEventListener("click", (event) => event.stopPropagation());
  searchMoveManualBtn.addEventListener("click", promptSearchMoveNotation);
  searchMoveCloseBtn.addEventListener("click", closeSearchMoveOverlay);
  historyOverlay.addEventListener("click", closeHistoryOverlay);
  historyPanel.addEventListener("click", (event) => event.stopPropagation());
  historyHardestMovesBtn.addEventListener("click", () => {
    if (historyHardestMovesBtn.disabled) return;
    closeProgressOverlay();
    state.historyMode = "hardestMoves";
    state.historyOpen = true;
    state.hardestMovesLoading = true;
    renderHistory();
    send({ type: "sr_hardest_moves" });
  });
  historyCloseBtn.addEventListener("click", closeHistoryOverlay);
  progressOverlay.addEventListener("click", closeProgressOverlay);
  progressPanel.addEventListener("click", (event) => event.stopPropagation());
  progressCloseBtn.addEventListener("click", closeProgressOverlay);

  srNewBtn.addEventListener("click", () => send({ type: "sr_new" }));
  srHistoryBtn.addEventListener("click", () => {
    closeProgressOverlay();
    state.historyMode = "history";
    state.historyOpen = true;
    state.historyLoading = true;
    renderHistory();
    send({ type: "sr_history" });
  });
  srBookmarksBtn.addEventListener("click", () => {
    closeProgressOverlay();
    state.historyMode = "bookmarks";
    state.historyOpen = true;
    state.bookmarksLoading = true;
    renderHistory();
    send({ type: "sr_bookmarks" });
  });
  srProgressBtn.addEventListener("click", () => {
    closeHistoryOverlay();
    state.progressOpen = true;
    state.progressLoading = true;
    renderProgress();
    send({ type: "sr_progress" });
  });
  srHintBtn.addEventListener("click", () => send({ type: "sr_hint" }));
  srStudyFromHereBtn.addEventListener("click", () => {
    const startRange = promptStudyStartRange();
    if (startRange === null) return;
    send({ type: "sr_study_from_here", start_range: startRange });
  });
  srGuessGiveUpBtn.addEventListener("click", () => send({ type: "sr_give_up" }));
  srGuessFinishBtn.addEventListener("click", () => send({ type: "sr_finish_prompt" }));
  srGuessFinishNewBtn.addEventListener("click", () => send({ type: "sr_finish_prompt_new" }));
  srGuessBookmarkBtn.addEventListener("click", () => {
    if (srGuessBookmarkBtn.disabled) return;
    srGuessBookmarkBtn.disabled = true;
    send({ type: "sr_bookmark_current_prompt" });
  });
  srGuessSkipBtn.addEventListener("click", () => {
    if (srGuessSkipBtn.disabled) return;
    showSkipToast();
    send({ type: "sr_skip_move" });
  });
  srAcceptAlternativeBtn.addEventListener("click", () => {
    if (srAcceptAlternativeBtn.disabled) return;
    send({ type: "sr_accept_alternative" });
  });
  srGuessBlacklistBtn.addEventListener("click", () => send({ type: "sr_blacklist_move" }));
  srGuessBlacklistLineBtn.addEventListener("click", () => send({ type: "sr_blacklist_line_prompt" }));
  srShowAlternativesInput.addEventListener("change", () => {
    send({ type: "sr_review_show_alternatives", enabled: srShowAlternativesInput.checked });
  });
  srSearchMoveBtn.addEventListener("click", () => {
    state.searchMoveDismissed = false;
    renderSearchMove();
    send({ type: "sr_search_move" });
  });
  srMarkedMovesBtn.addEventListener("click", () => {
    state.searchMoveDismissed = false;
    renderSearchMove();
    send({ type: "sr_marked_moves" });
  });
  srBookmarkedMovesBtn.addEventListener("click", () => {
    state.searchMoveDismissed = false;
    renderSearchMove();
    send({ type: "sr_bookmarked_moves" });
  });
  function toggleCurrentMoveBookmark(button) {
    const target = currentMoveBookmarkTarget();
    if (!target || button.disabled) return;
    state.moveBookmarkPending = true;
    refreshButtons();
    if (target.bookmarked) {
      send({
        type: "sr_unmark_move",
        mark: "bookmark",
        fen: target.fen,
        uci: target.uci,
      });
      return;
    }
    send({
      type: "sr_bookmark_move",
      fen: target.fen,
      uci: target.uci,
    });
  }

  srGuessBookmarkMoveBtn.addEventListener("click", () => toggleCurrentMoveBookmark(srGuessBookmarkMoveBtn));
  srBookmarkMoveBtn.addEventListener("click", () => toggleCurrentMoveBookmark(srBookmarkMoveBtn));

  refreshButtons();

  return {
    applySrState,
    applyHistory,
    applyBookmarks,
    applyHardestMoves,
    applyProgress,
    applyReviewNavigation,
    refreshBoardState: refreshAnalyzeLichessLink,
    isReviewMode,
    handleFlip,
    handleReset,
    getMoveDests,
    getReviewNextUcis,
    handleReviewMove,
  };
}
