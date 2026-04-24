// import { log } from "./board.js";

export function createPgnViewerUi({ send, onFlipBoard, onResetBoard }) {
  // log("Creating PGN Viewer UI");
  const srNewBtn = document.getElementById("srNew");
  const srPrevBtn = document.getElementById("srPrev");
  const srHintBtn = document.getElementById("srHint");
  const srSearchMoveBtn = document.getElementById("srSearchMove");

  const treePanel = document.getElementById("treePanel");
  const guessActions = document.getElementById("guessActions");
  const srGuessGiveUpBtn = document.getElementById("srGuessGiveUp");
  const srGuessFinishBtn = document.getElementById("srGuessFinish");
  const srGuessTooEasyBtn = document.getElementById("srGuessTooEasy");
  const treeContainer = document.getElementById("variation-tree");

  const searchMoveOverlay = document.getElementById("searchMoveOverlay");
  const searchMovePanel = document.getElementById("searchMovePanel");
  const searchMoveTitle = document.getElementById("searchMoveTitle");
  const searchMoveResults = document.getElementById("searchMoveResults");
  const searchMoveCloseBtn = document.getElementById("searchMoveClose");

  const state = {
    active: false,
    mode: "idle", // idle | guess | review
    review: null,
    searchMove: null,
    searchMoveDismissed: false,
  };

  const reviewNav = {
    requestedPath: null,
    queuedPath: null,
  };

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

  function refreshButtons() {
    const active = !!state.active;
    const isGuess = active && state.mode === "guess";
    srNewBtn.disabled = !active;
    srPrevBtn.disabled = !active;
    srHintBtn.disabled = !isGuess;
    srSearchMoveBtn.disabled = !active || state.mode !== "review";
    srGuessGiveUpBtn.disabled = !isGuess;
    srGuessFinishBtn.disabled = !isGuess;
    srGuessTooEasyBtn.disabled = !isGuess;
  }

  function closeSearchMoveOverlay() {
    state.searchMoveDismissed = true;
    searchMoveOverlay.hidden = true;
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

    const results = state.searchMove.results || [];
    const query = state.searchMove.query || {};
    const queryMove = typeof query.move === "string" ? query.move : "";
    const count = typeof state.searchMove.count === "number" ? state.searchMove.count : results.length;

    searchMoveTitle.textContent = queryMove ? `Search move: ${queryMove} (${count})` : `Search move (${count})`;
    searchMoveResults.innerHTML = "";

    const currentPath = state.review && Array.isArray(state.review.currentPath) ? state.review.currentPath : [];

    if (!results.length) {
      const empty = document.createElement("div");
      empty.className = "search-empty";
      empty.textContent = "No occurrences found";
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

      const startDots = document.createElement("span");
      startDots.className = "search-ellipsis";
      startDots.textContent = "…";
      div.appendChild(startDots);

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

      searchMoveResults.appendChild(div);
    }

    searchMoveOverlay.hidden = state.searchMoveDismissed;
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

  function handleKeyNavigation(event) {
    if (event.key === "Escape" && !searchMoveOverlay.hidden) {
      closeSearchMoveOverlay();
      return;
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

    const panelRect = treePanel.getBoundingClientRect();
    const rowRect = currentRow.getBoundingClientRect();
    const margin = Math.max(
      TREE_VIEWPORT_MIN_MARGIN_PX,
      Math.floor(treePanel.clientHeight * TREE_VIEWPORT_MARGIN_RATIO),
    );
    const visibleTop = panelRect.top + margin;
    const visibleBottom = panelRect.bottom - margin;

    if (rowRect.top < visibleTop) {
      treePanel.scrollTop -= visibleTop - rowRect.top;
      return;
    }

    if (rowRect.bottom > visibleBottom) {
      treePanel.scrollTop += rowRect.bottom - visibleBottom;
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

  function renderTreeNode(node, nodePath = [], basePath = [], globalCurrentPath = []) {
    const globalPath = [...basePath, ...nodePath];
    const normalizedCurrentPath = Array.isArray(globalCurrentPath) ? globalCurrentPath : [];
    const isCurrent = pathsEqual(globalPath, globalCurrentPath);
    const pathRelation = getPathRelation(globalPath, normalizedCurrentPath);
    const div = document.createElement("div");
    div.className = `tree-node ${pathRelation} ${isCurrent ? 'current' : ''}`.trim();
    div.dataset.path = JSON.stringify(globalPath);

    const row = document.createElement("div");
    row.className = "tree-row";
    div.appendChild(row);

    if (node.san) {
      // This is a move node
      const moveSpan = document.createElement("span");
      moveSpan.className = "tree-move";
      moveSpan.textContent = `${node.moveNumber}${node.color === 'white' ? '.' : '...'} ${node.san}`;
      row.addEventListener("click", (event) => {
        event.stopPropagation();
        queueReviewNavigation(globalPath);
      });
      row.appendChild(moveSpan);
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
      treePanel.style.display = "block";
      guessActions.hidden = false;
      treeContainer.innerHTML = "";
      return;
    }

    if (isReviewMode() && state.review && state.review.tree) {
      treePanel.style.display = "block";
      guessActions.hidden = true;
      treeContainer.innerHTML = "";
      const tree = state.review.tree;
      const globalCurrentPath = Array.isArray(state.review.currentPath) ? state.review.currentPath : [];
      const viewRootPath = Array.isArray(state.review.viewRootPath) ? state.review.viewRootPath : [];
      const viewRootNode = findNodeAtPath(tree, viewRootPath) || tree;
      const treeRoot = renderTreeNode(viewRootNode, [], viewRootPath, globalCurrentPath);
      treeContainer.appendChild(treeRoot);
      queueTreeViewportSync();
      return;
    }

    treePanel.style.display = "none";
    guessActions.hidden = true;
    treeContainer.innerHTML = "";
  }

  function applySrState(sr) {
    state.active = !!sr.active;
    state.mode = sr.mode || "idle";
    state.review = sr.review || null;
    state.searchMove = sr.searchMove || null;
    resetReviewNavigation();

    refreshButtons();
    renderReviewTree();
    renderSearchMove();
  }

  function applyReviewNavigation(review) {
    if (!isReviewMode() || !state.review || !state.review.tree) return;
    if (!review || !Array.isArray(review.currentPath) || !Array.isArray(review.viewRootPath)) {
      throw new Error("Invalid review navigation payload");
    }

    state.review.currentPath = clonePath(review.currentPath);
    state.review.viewRootPath = clonePath(review.viewRootPath);

    renderReviewTree();
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
  searchMoveCloseBtn.addEventListener("click", closeSearchMoveOverlay);

  srNewBtn.addEventListener("click", () => send({ type: "sr_new" }));
  srPrevBtn.addEventListener("click", () => send({ type: "sr_prev" }));
  srHintBtn.addEventListener("click", () => send({ type: "sr_hint" }));
  srGuessGiveUpBtn.addEventListener("click", () => send({ type: "sr_give_up" }));
  srGuessFinishBtn.addEventListener("click", () => send({ type: "sr_finish_prompt" }));
  srGuessTooEasyBtn.addEventListener("click", () => send({ type: "sr_finish_prompt", ease: 0.5 }));
  srSearchMoveBtn.addEventListener("click", () => {
    state.searchMoveDismissed = false;
    renderSearchMove();
    send({ type: "sr_search_move" });
  });

  refreshButtons();

  return {
    applySrState,
    applyReviewNavigation,
    isReviewMode,
    handleFlip,
    handleReset,
    getMoveDests,
    getReviewNextUcis,
    handleReviewMove,
  };
}
