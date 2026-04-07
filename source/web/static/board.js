import { Chessground } from "https://unpkg.com/chessground@9.2.1/dist/chessground.min.js";

const boardEl = document.getElementById("board");
const logEl = document.getElementById("log");
const fenEl = document.getElementById("fen");
const msgEl = document.getElementById("message");
const statusEl = document.getElementById("status");

const flipBtn = document.getElementById("flip");
const resetBtn = document.getElementById("reset");
const srNewBtn = document.getElementById("srNew");
const srContinueBtn = document.getElementById("srContinue");
const setFenBtn = document.getElementById("setFen");
const clearLogBtn = document.getElementById("clearLog");

function log(line) {
  const ts = new Date().toLocaleTimeString();
  logEl.textContent = `[${ts}] ${line}\n` + logEl.textContent;
}

function setStatus(text, ok) {
  statusEl.textContent = text;
  statusEl.classList.toggle("ok", !!ok);
}

const ground = Chessground(boardEl, {
  coordinates: true,
  highlight: { lastMove: true, check: true },
  animation: { enabled: true, duration: 180 },
  drawable: { enabled: false, visible: true },
  movable: {
    free: false,
    color: "both",
    dests: new Map(),
    showDests: true,
    events: {
      after: (orig, dest) => {
        const uci = maybePromote(orig, dest);
        send({ type: "move", uci });
      },
    },
  },
});

function maybePromote(orig, dest) {
  const piece = getPieceAt(orig);
  if (!piece || piece.role !== "pawn") return `${orig}${dest}`;
  const rank = dest[1];
  if (rank !== "1" && rank !== "8") return `${orig}${dest}`;
  const choice = (prompt("Promotion (q/r/b/n):", "q") || "q").trim().toLowerCase();
  const promo = ["q", "r", "b", "n"].includes(choice) ? choice : "q";
  return `${orig}${dest}${promo}`;
}

function getPieceAt(square) {
  // chessground has used both objects and Maps for pieces over time.
  const pieces = ground.state && ground.state.pieces;
  if (!pieces) return null;
  if (typeof pieces.get === "function") return pieces.get(square) || null;
  return pieces[square] || null;
}

function applyState(state) {
  fenEl.value = state.fen || "";
  msgEl.textContent = state.message || "";

  ground.set({
    fen: state.fen,
    orientation: state.orientation || "white",
    turnColor: state.turn || "white",
    lastMove: state.lastMove || undefined,
    movable: {
      free: false,
      color: state.turn || "both",
      dests: toDests(state.dests),
      showDests: true,
      events: {
        after: (orig, dest) => {
          const uci = maybePromote(orig, dest);
          send({ type: "move", uci });
        },
      },
    },
  });

  const shapes = [];
  for (const a of state.arrows || []) {
    if (a && a.orig && a.dest) shapes.push({ orig: a.orig, dest: a.dest, brush: a.color || "green" });
  }
  for (const c of state.circles || []) {
    if (c && c.square) shapes.push({ orig: c.square, brush: c.color || "green" });
  }
  ground.setAutoShapes(shapes);
}

function toDests(dests) {
  if (!dests) return new Map();
  if (dests instanceof Map) return dests;
  const m = new Map();
  for (const [orig, list] of Object.entries(dests)) {
    if (Array.isArray(list)) m.set(orig, list);
  }
  return m;
}

async function fetchState() {
  const res = await fetch("/api/state");
  const state = await res.json();
  applyState(state);
}

let ws = null;
let sendQueue = [];

function send(obj) {
  const msg = JSON.stringify(obj);
  if (!ws || ws.readyState !== WebSocket.OPEN) {
    sendQueue.push(msg);
    return;
  }
  ws.send(msg);
}

function connect() {
  const proto = location.protocol === "https:" ? "wss:" : "ws:";
  ws = new WebSocket(`${proto}//${location.host}/ws`);
  setStatus("connecting…", false);

  ws.onopen = () => {
    setStatus("connected", true);
    log("ws connected");
    for (const msg of sendQueue) ws.send(msg);
    sendQueue = [];
  };

  ws.onclose = () => {
    setStatus("disconnected", false);
    log("ws disconnected; retrying in 1s");
    setTimeout(connect, 1000);
  };

  ws.onerror = (event) => {
    const msg = `WebSocket error: ${event}`;
    log(msg);
    console.error(msg, event);
  };

  ws.onmessage = async (ev) => {
    let msg;
    try {
      msg = JSON.parse(ev.data);
    } catch {
      return;
    }

    if (msg.type === "state") {
      applyState(msg.state);
      return;
    }

    if (msg.type === "error") {
      log(`error: ${msg.message}`);
      // Log full details to browser console for debugging
      console.error("Full error details:", msg.message);
      await fetchState(); // resync
      return;
    }
  };
}

flipBtn.addEventListener("click", () => ground.toggleOrientation());

resetBtn.addEventListener("click", () => send({ type: "set", fen: "startpos" }));

srNewBtn.addEventListener("click", () => send({ type: "sr_new" }));
srContinueBtn.addEventListener("click", () => send({ type: "sr_continue" }));

setFenBtn.addEventListener("click", () => {
  const fen = (fenEl.value || "").trim();
  if (!fen) return;
  send({ type: "set", fen });
});

clearLogBtn.addEventListener("click", () => {
  logEl.textContent = "";
});

await fetchState();
connect();
