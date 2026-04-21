// import { log } from "./board.js";

export function createPgnViewerUi({ send, onFlipBoard, onResetBoard }) {
  // log("Creating PGN Viewer UI");
  const srNewBtn = document.getElementById("srNew");
  const srGiveUpBtn = document.getElementById("srGiveUp");
  const srPrevBtn = document.getElementById("srPrev");
  const srHintBtn = document.getElementById("srHint");
  const srSearchMoveBtn = document.getElementById("srSearchMove");

  const treePanel = document.getElementById("treePanel");
  const treeContainer = document.getElementById("variation-tree");

  const searchMovePanel = document.getElementById("searchMovePanel");
  const searchMoveTitle = document.getElementById("searchMoveTitle");
  const searchMoveResults = document.getElementById("searchMoveResults");

  const state = {
    active: false,
    mode: "idle", // idle | guess | review
    review: null,
    searchMove: null,
  };

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

  function handleReviewMove(uci) {
    const ctx = getReviewContext();
    if (!ctx) return false;

    const children = Array.isArray(ctx.node.children) ? ctx.node.children : [];
    const idx = children.findIndex((child) => child && child.uci === uci);
    if (idx < 0) return false;

    send({ type: "sr_goto", path: [...ctx.currentPath, idx] });
    return true;
  }

  function refreshButtons() {
    const active = !!state.active;
    srNewBtn.disabled = !active;
    srGiveUpBtn.disabled = !active || state.mode !== "guess";
    srPrevBtn.disabled = !active;
    srSearchMoveBtn.disabled = !active || state.mode !== "review";
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

    const currentPath = Array.isArray(state.review.currentPath) ? state.review.currentPath : [];
    const newPath = getArrowNavigationPath(state.review.tree, currentPath, direction);
    if (JSON.stringify(newPath) !== JSON.stringify(currentPath)) {
      event.preventDefault();
      send({ type: 'sr_goto', path: newPath });
    }
  }

  function commonPrefixLength(a, b) {
  let i = 0;
  while (i < a.length && i < b.length && a[i] === b[i]) i++;
  return i;
  }

  const MAX_FORWARD_PLIES = 2;

  function renderTreeNode(node, currentPath, nodePath = []) {
    const isCurrent = JSON.stringify(nodePath) === JSON.stringify(currentPath);
    const div = document.createElement("div");
    div.className = `tree-node ${isCurrent ? 'current' : ''}`;
    div.dataset.path = JSON.stringify(nodePath);

    if (node.san) {
      // This is a move node
      const moveSpan = document.createElement("span");
      moveSpan.className = "tree-move";
      moveSpan.textContent = `${node.moveNumber}${node.color === 'white' ? '.' : '...'} ${node.san}`;
      moveSpan.addEventListener("click", (event) => {
        event.stopPropagation();
        send({ type: "sr_goto", path: nodePath });
      });
      div.appendChild(moveSpan);
    } else {
      // This is a position node (root)
      const posSpan = document.createElement("span");
      posSpan.className = "tree-position";
      posSpan.textContent = `Position (ply ${node.ply})`;
      div.appendChild(posSpan);
    }

    const cpl = commonPrefixLength(nodePath, currentPath || []);
    const depthFromCurrent = nodePath.length - cpl;

    const isAncestor = nodePath.length <= (currentPath || []).length && cpl === nodePath.length;

    let shouldRenderChildren = true;

    if (!isAncestor) {
      // we are at or ahead of current node → limit forward depth
      if (depthFromCurrent >= MAX_FORWARD_PLIES) {
        shouldRenderChildren = false;
      }
    }

    if (node.children && node.children.length > 0 && shouldRenderChildren) {
      const childrenDiv = document.createElement("div");
      childrenDiv.className = "tree-children";
      for (let i = 0; i < node.children.length; i++) {
        const child = node.children[i];
        childrenDiv.appendChild(renderTreeNode(child, currentPath, [...nodePath, i]));
      }
      div.appendChild(childrenDiv);
    }

    return div;
  }

  function applySrState(sr) {
    // log("[DEBUG] applySrState called with sr:");
    state.active = !!sr.active;
    state.mode = sr.mode || "idle";
    state.review = sr.review || null;
    state.searchMove = sr.searchMove || null;

    refreshButtons();

    if (isReviewMode() && state.review && state.review.tree) {
      treePanel.style.display = "block";
      treeContainer.innerHTML = "";
      const treeRoot = renderTreeNode(state.review.tree, state.review.currentPath || []);
      treeContainer.appendChild(treeRoot);
    } else {
      treePanel.style.display = "none";
      treeContainer.innerHTML = "";
    }

    const hasSearch = isReviewMode() && state.searchMove && Array.isArray(state.searchMove.results);
    if (!hasSearch) {
      searchMovePanel.style.display = "none";
      searchMoveTitle.textContent = "Search move";
      searchMoveResults.innerHTML = "";
      return;
    }

    const results = state.searchMove.results || [];
    const query = state.searchMove.query || {};
    const queryMove = typeof query.move === "string" ? query.move : "";
    const count = typeof state.searchMove.count === "number" ? state.searchMove.count : results.length;

    searchMovePanel.style.display = "block";
    searchMoveTitle.textContent = queryMove ? `Search move: ${queryMove} (${count})` : `Search move (${count})`;
    searchMoveResults.innerHTML = "";

    const currentPath = Array.isArray(state.review.currentPath) ? state.review.currentPath : [];

    if (!results.length) {
      const empty = document.createElement("div");
      empty.className = "search-empty";
      empty.textContent = "No occurrences found";
      searchMoveResults.appendChild(empty);
      return;
    }

    for (const item of results) {
      const path = Array.isArray(item.path) ? item.path : [];
      const isCurrent = JSON.stringify(path) === JSON.stringify(currentPath);

      const div = document.createElement("div");
      div.className = `search-item ${isCurrent ? "current" : ""}`;
      div.addEventListener("click", (event) => {
        event.stopPropagation();
        send({ type: "sr_goto", path });
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

      const endDots = document.createElement("span");
      endDots.className = "search-ellipsis";
      endDots.textContent = "…";
      div.appendChild(endDots);

      searchMoveResults.appendChild(div);
    }
  }

  function handleFlip() {
    onFlipBoard();
  }

  function handleReset() {
    onResetBoard();
  }

  document.addEventListener("keydown", handleKeyNavigation);

  srNewBtn.addEventListener("click", () => send({ type: "sr_new" }));
  srGiveUpBtn.addEventListener("click", () => send({ type: "sr_give_up" }));
  srPrevBtn.addEventListener("click", () => send({ type: "sr_prev" }));
  srHintBtn.addEventListener("click", () => send({ type: "sr_hint" }));
  srSearchMoveBtn.addEventListener("click", () => send({ type: "sr_search_move" }));

  refreshButtons();

  return {
    applySrState,
    isReviewMode,
    handleFlip,
    handleReset,
    getMoveDests,
    getReviewNextUcis,
    handleReviewMove,
  };
}
