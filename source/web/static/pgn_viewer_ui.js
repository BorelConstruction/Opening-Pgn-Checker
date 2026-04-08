export function createPgnViewerUi({ send, onFlipBoard, onResetBoard }) {
  const srNewBtn = document.getElementById("srNew");
  const srContinueBtn = document.getElementById("srContinue");
  const srGiveUpBtn = document.getElementById("srGiveUp");

  const lpvRoot = document.getElementById("lpv");

  const VIEWER_ESM_URLS = [
    // ESM CDNs that rewrite/bundle npm packages for browsers.
    // Keep more than one because some environments block individual CDNs.
    "https://esm.run/lichess-pgn-viewer@2.4.5",
    "https://esm.run/@lichess-org/pgn-viewer@2.4.7",
    "https://esm.sh/lichess-pgn-viewer@2.4.5",
    "https://esm.sh/@lichess-org/pgn-viewer@2.4.7",
  ];

  const VIEWER_SCRIPT_URLS = [
    // Pre-bundled browser build (no import maps required).
    "https://unpkg.com/lichess-pgn-viewer@2.4.5/dist/lichess-pgn-viewer.min.js",
    "https://cdn.jsdelivr.net/npm/lichess-pgn-viewer@2.4.5/dist/lichess-pgn-viewer.min.js",
  ];

  const state = {
    active: false,
    mode: "idle", // idle | guess | review
    review: null,
  };

  let viewer = null;
  let viewerKey = null;
  let viewerFactoryPromise = null;

  function isReviewMode() {
    return state.active && state.mode === "review";
  }

  function refreshButtons() {
    const active = !!state.active;
    srNewBtn.disabled = !active;
    srContinueBtn.disabled = !active || state.mode !== "guess";
    srGiveUpBtn.disabled = !active || state.mode !== "guess";
  }

  function destroyViewer() {
    viewer = null;
    viewerKey = null;
    lpvRoot.textContent = "";
  }

  function resolveViewerFactory(maybe) {
    if (typeof maybe === "function") return maybe;
    if (!maybe || typeof maybe !== "object") return null;
    if (typeof maybe.start === "function") return maybe.start;
    if (typeof maybe.default === "function") return maybe.default;
    if (maybe.default && typeof maybe.default.start === "function") return maybe.default.start;
    return null;
  }

  function getViewerFactoryFromWindow() {
    return (
      resolveViewerFactory(window.LichessPgnViewer) ||
      resolveViewerFactory(window.lichessPgnViewer) ||
      resolveViewerFactory(window.pgnViewer)
    );
  }

  function listKnownGlobals() {
    const entries = [];
    for (const name of ["LichessPgnViewer", "lichessPgnViewer", "pgnViewer"]) {
      const value = window[name];
      const type = value === null ? "null" : typeof value;
      entries.push(`${name}: ${type}`);
    }
    return entries.join(", ");
  }

  function describeImportError(err) {
    if (!err) return "unknown error";
    const msg = err && err.message ? err.message : String(err);
    return msg.replace(/\s+/g, " ").trim();
  }

  function pickFactoryFromModule(mod) {
    const candidates = [
      mod && mod.default,
      mod && mod.start,
      mod && mod.LichessPgnViewer,
      mod && mod.default && mod.default.default,
    ];
    for (const c of candidates) {
      if (typeof c === "function") return c;
    }
    return null;
  }

  function loadScript(url) {
    return new Promise((resolve, reject) => {
      const existing = Array.from(document.scripts).find((s) => s && s.src === url);
      if (existing) {
        if (existing.dataset && existing.dataset.lpvLoaded === "1") {
          resolve();
          return;
        }
        existing.remove();
      }

      const script = document.createElement("script");
      script.src = url;
      script.async = true;
      // Allow richer error details for cross-origin scripts when CORS headers are present.
      script.crossOrigin = "anonymous";
      script.onload = () => {
        script.dataset.lpvLoaded = "1";
        resolve();
      };
      script.onerror = () => reject(new Error(`script load failed: ${url}`));
      document.head.appendChild(script);
    });
  }

  async function loadViewerFactory() {
    const globalFactory = getViewerFactoryFromWindow();
    if (globalFactory) return globalFactory;

    if (viewerFactoryPromise) return viewerFactoryPromise;

    viewerFactoryPromise = (async () => {
      const failures = [];

      for (const url of VIEWER_ESM_URLS) {
        try {
          const mod = await import(url);
          const factory = pickFactoryFromModule(mod);
          if (!factory) {
            const exportsList = mod ? Object.keys(mod).join(", ") : "(no module)";
            throw new Error(`No viewer factory export found. Exports: ${exportsList || "(none)"}`);
          }
          return factory;
        } catch (err) {
          failures.push({ url, err: describeImportError(err) });
        }
      }

      for (const url of VIEWER_SCRIPT_URLS) {
        try {
          await loadScript(url);
          const fromWindow = getViewerFactoryFromWindow();
          if (fromWindow) return fromWindow;
          failures.push({
            url,
            err: `loaded but did not register a global viewer factory (globals: ${listKnownGlobals()})`,
          });
        } catch (err) {
          failures.push({ url, err: describeImportError(err) });
        }
      }

      const lines = failures.map((f) => `- ${f.url}: ${f.err}`);
      const hint = [
        "Troubleshooting:",
        "- Open DevTools → Network and confirm the viewer script/module loads (200).",
        "- Open DevTools → Console and copy the first error (often CSP/adblock/CORS).",
      ].join("\n");
      throw new Error(`Failed to load lichess-pgn-viewer.\n${lines.join("\n")}\n\n${hint}`);
    })();
    viewerFactoryPromise.catch(() => {
      // Allow retry after transient network/CSP issues without requiring a full page reload.
      viewerFactoryPromise = null;
    });

    return viewerFactoryPromise;
  }

  async function ensureViewer(review) {
    const key = `${review.fen}|${review.orientation}|${review.initialPly}|${review.pgn}`;
    if (viewer && viewerKey === key) return;

    destroyViewer();
    viewerKey = key;

    const start = await loadViewerFactory();

    viewer = start(lpvRoot, {
      pgn: review.pgn,
      fen: review.fen,
      orientation: review.orientation,
      initialPly: review.initialPly,
      showPlayers: false,
      showMoves: "right",
      showClocks: false,
      showControls: true,
      keyboardToMove: true,
      scrollToMove: true,
      drawArrows: false,
      menu: {
        getPgn: { enabled: false },
        practiceWithComputer: { enabled: false },
        analysisBoard: { enabled: false },
      },
    });
  }

  async function applySrState(sr) {
    state.active = !!sr.active;
    state.mode = sr.mode || "idle";
    state.review = sr.review || null;

    const inReview = isReviewMode();
    document.body.classList.toggle("sr-review", inReview);

    refreshButtons();

    if (inReview) {
      if (!state.review) {
        destroyViewer();
        lpvRoot.textContent = "No PGN payload for review mode.";
        return;
      }
      lpvRoot.textContent = "Loading PGN viewer\u2026";
      try {
        await ensureViewer(state.review);
      } catch (err) {
        destroyViewer();
        const message = err && err.message ? err.message : String(err);
        lpvRoot.textContent = `PGN viewer error: ${message}\n\nOpen DevTools → Console for full details.`;
        console.error("PGN viewer error:", err);
      }
    } else {
      destroyViewer();
    }
  }

  function handleFlip() {
    if (viewer && isReviewMode()) {
      viewer.flip();
      return;
    }
    onFlipBoard();
  }

  function handleReset() {
    if (viewer && isReviewMode()) {
      viewer.goTo("first");
      return;
    }
    onResetBoard();
  }

  srNewBtn.addEventListener("click", () => send({ type: "sr_new" }));
  srContinueBtn.addEventListener("click", () => send({ type: "sr_continue" }));
  srGiveUpBtn.addEventListener("click", () => send({ type: "sr_give_up" }));

  refreshButtons();

  return {
    applySrState,
    isReviewMode,
    handleFlip,
    handleReset,
  };
}
