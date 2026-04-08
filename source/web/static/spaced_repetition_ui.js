export function createSpacedRepetitionUi({ send }) {
  const srNewBtn = document.getElementById("srNew");
  const srContinueBtn = document.getElementById("srContinue");
  const srGiveUpBtn = document.getElementById("srGiveUp");

  const movesHintEl = document.getElementById("movesHint");
  const moveListEl = document.getElementById("moveTree");
  const movesPrevBtn = document.getElementById("movesPrev");
  const movesNextBtn = document.getElementById("movesNext");

  const state = {
    active: false,
    mode: "idle", // idle | guess | review
    tree: null,
    currentPath: null,
  };

  let treeCache = null;
  const moveElsByKey = new Map();
  const movePathByKey = new Map();

  function pathKey(path) {
    return Array.isArray(path) ? path.join(",") : "";
  }

  function pathsEqual(a, b) {
    if (!Array.isArray(a) || !Array.isArray(b)) return false;
    if (a.length !== b.length) return false;
    for (let i = 0; i < a.length; i++) if (a[i] !== b[i]) return false;
    return true;
  }

  function isPrefix(prefix, full) {
    if (!Array.isArray(prefix) || !Array.isArray(full)) return false;
    if (prefix.length > full.length) return false;
    for (let i = 0; i < prefix.length; i++) if (prefix[i] !== full[i]) return false;
    return true;
  }

  function clearMoveList() {
    moveListEl.textContent = "";
    moveElsByKey.clear();
    movePathByKey.clear();
  }

  function formatPrefix(node) {
    const ply = typeof node.ply === "number" ? node.ply : null;
    const num = typeof node.moveNumber === "number" ? node.moveNumber : null;
    if (ply == null || num == null) return "";
    return ply % 2 === 1 ? `${num}.` : `${num}...`;
  }

  function flattenTree(positionNode, branchDepth, out) {
    const children = Array.isArray(positionNode.children) ? positionNode.children : [];
    if (children.length === 0) return;

    const main = children[0];
    out.push({ node: main, branchDepth });

    for (let i = 1; i < children.length; i++) {
      flattenVariation(children[i], branchDepth + 1, out);
    }

    flattenTree(main, branchDepth, out);
  }

  function flattenVariation(moveNode, branchDepth, out) {
    out.push({ node: moveNode, branchDepth });
    flattenTree(moveNode, branchDepth, out);
  }

  function renderMoveList(tree) {
    clearMoveList();
    if (!tree) return;

    const root = document.createElement("div");
    root.className = "move-item root";
    root.textContent = "Start";
    root.addEventListener("click", () => send({ type: "sr_goto", path: [] }));
    moveListEl.appendChild(root);
    moveElsByKey.set(pathKey([]), root);
    movePathByKey.set(pathKey([]), []);

    const items = [];
    flattenTree(tree, 0, items);

    for (const { node, branchDepth } of items) {
      const path = Array.isArray(node.path) ? node.path : [];
      const key = pathKey(path);

      const el = document.createElement("div");
      el.className = "move-item";
      el.style.paddingLeft = `${10 + branchDepth * 16}px`;

      const prefixEl = document.createElement("span");
      prefixEl.className = "move-prefix";
      prefixEl.textContent = formatPrefix(node);

      const sanEl = document.createElement("span");
      sanEl.className = "move-san";
      sanEl.textContent = node.san || "";

      el.appendChild(prefixEl);
      el.appendChild(sanEl);
      el.addEventListener("click", () => send({ type: "sr_goto", path }));

      moveListEl.appendChild(el);
      moveElsByKey.set(key, el);
      movePathByKey.set(key, path);
    }

    updateMoveHighlights({ scrollToCurrent: true });
  }

  function updateMoveHighlights({ scrollToCurrent }) {
    const current = state.currentPath;
    let currentEl = null;
    for (const [key, el] of moveElsByKey) {
      const path = movePathByKey.get(key) || [];
      const isCurrent = pathsEqual(path, current);
      el.classList.toggle("current", isCurrent);
      el.classList.toggle("onpath", isPrefix(path, current));
      if (isCurrent) currentEl = el;
    }
    if (scrollToCurrent && currentEl) currentEl.scrollIntoView({ block: "nearest" });
  }

  function getTreeNode(tree, path) {
    let node = tree;
    if (!Array.isArray(path)) return node;
    for (const idx of path) {
      if (!node || !Array.isArray(node.children)) return null;
      if (typeof idx !== "number" || idx < 0 || idx >= node.children.length) return null;
      node = node.children[idx];
    }
    return node;
  }

  function goToPath(path) {
    send({ type: "sr_goto", path });
  }

  function goParent() {
    const cur = Array.isArray(state.currentPath) ? state.currentPath : [];
    if (cur.length === 0) return;
    goToPath(cur.slice(0, -1));
  }

  function goMainlineChild() {
    if (!treeCache) return;
    const cur = Array.isArray(state.currentPath) ? state.currentPath : [];
    const node = getTreeNode(treeCache, cur);
    if (!node || !Array.isArray(node.children) || node.children.length === 0) return;
    goToPath([...cur, 0]);
  }

  function goSibling(delta) {
    if (!treeCache) return;
    const cur = Array.isArray(state.currentPath) ? state.currentPath : [];
    if (cur.length === 0) return;

    const parentPath = cur.slice(0, -1);
    const idx = cur[cur.length - 1];
    const parent = getTreeNode(treeCache, parentPath);
    if (!parent || !Array.isArray(parent.children)) return;

    const nextIdx = idx + delta;
    if (nextIdx < 0 || nextIdx >= parent.children.length) return;
    goToPath([...parentPath, nextIdx]);
  }

  function goFirst() {
    goToPath([]);
  }

  function goLastMainline() {
    if (!treeCache) return;
    const path = [];
    let node = treeCache;
    while (node && Array.isArray(node.children) && node.children.length > 0) {
      path.push(0);
      node = node.children[0];
    }
    goToPath(path);
  }

  function isTextInput(el) {
    if (!el) return false;
    const tag = el.tagName ? el.tagName.toLowerCase() : "";
    return tag === "input" || tag === "textarea" || !!el.isContentEditable;
  }

  function onKeyDown(ev) {
    if (!state.active || state.mode !== "review") return;
    if (isTextInput(ev.target)) return;

    if (ev.key === "ArrowLeft") {
      ev.preventDefault();
      goParent();
      return;
    }
    if (ev.key === "ArrowRight") {
      ev.preventDefault();
      goMainlineChild();
      return;
    }
    if (ev.key === "ArrowUp") {
      ev.preventDefault();
      goSibling(-1);
      return;
    }
    if (ev.key === "ArrowDown") {
      ev.preventDefault();
      goSibling(1);
      return;
    }
    if (ev.key === "Home") {
      ev.preventDefault();
      goFirst();
      return;
    }
    if (ev.key === "End") {
      ev.preventDefault();
      goLastMainline();
    }
  }

  function refreshUi() {
    const active = !!state.active;
    srNewBtn.disabled = !active;
    srContinueBtn.disabled = !active || state.mode !== "guess";
    srGiveUpBtn.disabled = !active || state.mode !== "guess";

    const inReview = active && state.mode === "review";
    movesPrevBtn.disabled = !inReview;
    movesNextBtn.disabled = !inReview;

    if (!active) {
      movesHintEl.textContent = "Start spaced repetition to browse variations.";
      moveListEl.style.display = "none";
      return;
    }

    if (state.mode === "guess") {
      movesHintEl.textContent = "Hidden while guessing. Finish the line or click Give up.";
      moveListEl.style.display = "none";
      return;
    }

    if (state.mode !== "review") {
      movesHintEl.textContent = "";
      moveListEl.style.display = "none";
      return;
    }

    movesHintEl.textContent = "Use \u2190/\u2192 to move, \u2191/\u2193 to switch variations (Home/End also work).";
    moveListEl.style.display = "block";

    if (treeCache !== state.tree) {
      treeCache = state.tree;
      renderMoveList(treeCache);
    } else {
      updateMoveHighlights({ scrollToCurrent: true });
    }
  }

  function applySrState(update) {
    if (!update || typeof update !== "object") return;

    if (typeof update.active === "boolean") state.active = update.active;
    if (typeof update.mode === "string") state.mode = update.mode;
    if (Object.prototype.hasOwnProperty.call(update, "tree")) state.tree = update.tree;
    if (Object.prototype.hasOwnProperty.call(update, "currentPath")) state.currentPath = update.currentPath;

    if (Object.prototype.hasOwnProperty.call(update, "tree") && update.tree === null) {
      treeCache = null;
      clearMoveList();
    }

    refreshUi();
  }

  srNewBtn.addEventListener("click", () => send({ type: "sr_new" }));
  srContinueBtn.addEventListener("click", () => send({ type: "sr_continue" }));
  srGiveUpBtn.addEventListener("click", () => send({ type: "sr_give_up" }));

  movesPrevBtn.addEventListener("click", () => goParent());
  movesNextBtn.addEventListener("click", () => goMainlineChild());

  document.addEventListener("keydown", onKeyDown);

  refreshUi();

  return { applySrState };
}

