const pptxgen = require("pptxgenjs");
const React = require("react");
const ReactDOMServer = require("react-dom/server");
const sharp = require("sharp");
const {
  FaBolt, FaShieldAlt, FaBrain, FaChartLine, FaExchangeAlt,
  FaLayerGroup, FaCogs, FaRocket, FaCheckCircle, FaArrowRight,
  FaDatabase, FaWifi, FaFilter, FaBell
} = require("react-icons/fa");

function renderIconSvg(IconComponent, color = "#000000", size = 256) {
  return ReactDOMServer.renderToStaticMarkup(
    React.createElement(IconComponent, { color, size: String(size) })
  );
}

async function iconToBase64Png(IconComponent, color, size = 256) {
  const svg = renderIconSvg(IconComponent, color, size);
  const pngBuffer = await sharp(Buffer.from(svg)).png().toBuffer();
  return "image/png;base64," + pngBuffer.toString("base64");
}

// Fresh shadow factory (pptxgenjs mutates objects)
const makeShadow = () => ({ type: "outer", blur: 8, offset: 3, angle: 135, color: "000000", opacity: 0.12 });

async function buildDeck() {
  let pres = new pptxgen();
  pres.layout = "LAYOUT_16x9";
  pres.author = "DIAMOND v2";
  pres.title = "DIAMOND v2 — Kalshi Prediction Engine";

  // Colors — Midnight Executive with cyan accent (matches dashboard aesthetic)
  const BG_DARK = "0A0F1C";
  const BG_CARD = "141B2D";
  const CYAN = "06B6D4";
  const VIOLET = "8B5CF6";
  const WHITE = "FFFFFF";
  const GRAY = "94A3B8";
  const GREEN = "10B981";
  const AMBER = "F59E0B";
  const RED = "EF4444";

  // Pre-render icons
  const iconBolt = await iconToBase64Png(FaBolt, "#06B6D4");
  const iconShield = await iconToBase64Png(FaShieldAlt, "#06B6D4");
  const iconBrain = await iconToBase64Png(FaBrain, "#06B6D4");
  const iconChart = await iconToBase64Png(FaChartLine, "#06B6D4");
  const iconExchange = await iconToBase64Png(FaExchangeAlt, "#06B6D4");
  const iconLayers = await iconToBase64Png(FaLayerGroup, "#06B6D4");
  const iconCogs = await iconToBase64Png(FaCogs, "#06B6D4");
  const iconRocket = await iconToBase64Png(FaRocket, "#FFFFFF");
  const iconCheck = await iconToBase64Png(FaCheckCircle, "#10B981");
  const iconArrow = await iconToBase64Png(FaArrowRight, "#06B6D4");
  const iconDB = await iconToBase64Png(FaDatabase, "#06B6D4");
  const iconWifi = await iconToBase64Png(FaWifi, "#06B6D4");
  const iconFilter = await iconToBase64Png(FaFilter, "#06B6D4");
  const iconBell = await iconToBase64Png(FaBell, "#06B6D4");

  // ═══════════════════════════════════════════════════════════════
  // SLIDE 1: Title
  // ═══════════════════════════════════════════════════════════════
  let s1 = pres.addSlide();
  s1.background = { color: BG_DARK };

  // Accent bar at top
  s1.addShape(pres.shapes.RECTANGLE, { x: 0, y: 0, w: 10, h: 0.06, fill: { color: CYAN } });

  // Diamond icon
  s1.addText("DIAMOND", {
    x: 0.8, y: 1.2, w: 8.4, h: 0.8,
    fontSize: 16, fontFace: "Arial", color: CYAN, charSpacing: 8, bold: true, margin: 0
  });

  s1.addText("Kalshi Prediction Engine", {
    x: 0.8, y: 1.9, w: 8.4, h: 1.2,
    fontSize: 42, fontFace: "Georgia", color: WHITE, bold: true, margin: 0
  });

  s1.addText("Real-time anomaly detection + automated trading on prediction markets", {
    x: 0.8, y: 3.2, w: 7, h: 0.6,
    fontSize: 16, fontFace: "Calibri", color: GRAY, margin: 0
  });

  // Version tag
  s1.addShape(pres.shapes.RECTANGLE, {
    x: 0.8, y: 4.2, w: 1.4, h: 0.4,
    fill: { color: CYAN, transparency: 85 },
  });
  s1.addText("v2.0", {
    x: 0.8, y: 4.2, w: 1.4, h: 0.4,
    fontSize: 13, fontFace: "Calibri", color: CYAN, bold: true, align: "center", valign: "middle", margin: 0
  });

  s1.addText("March 2026", {
    x: 2.4, y: 4.2, w: 2, h: 0.4,
    fontSize: 13, fontFace: "Calibri", color: GRAY, valign: "middle", margin: 0
  });

  // ═══════════════════════════════════════════════════════════════
  // SLIDE 2: Architecture / Workflow
  // ═══════════════════════════════════════════════════════════════
  let s2 = pres.addSlide();
  s2.background = { color: BG_DARK };
  s2.addShape(pres.shapes.RECTANGLE, { x: 0, y: 0, w: 10, h: 0.06, fill: { color: CYAN } });

  s2.addText("System Architecture", {
    x: 0.8, y: 0.3, w: 8.4, h: 0.7,
    fontSize: 32, fontFace: "Georgia", color: WHITE, bold: true, margin: 0
  });

  // Flow diagram — horizontal pipeline
  const flowY = 1.4;
  const boxH = 1.3;
  const boxW = 1.6;
  const gap = 0.25;

  const stages = [
    { label: "Kalshi\nWebSocket", icon: iconWifi, sub: "Real-time trades" },
    { label: "Stream\nProcessor", icon: iconCogs, sub: "Async pipeline" },
    { label: "10-Feature\nEngine", icon: iconBrain, sub: "Anomaly scoring" },
    { label: "Conviction\nTracker", icon: iconExchange, sub: "Event grouping" },
    { label: "Smart\nExecution", icon: iconChart, sub: "Book-aware pricing" },
  ];

  let curX = 0.5;
  for (let i = 0; i < stages.length; i++) {
    const st = stages[i];
    // Card background
    s2.addShape(pres.shapes.RECTANGLE, {
      x: curX, y: flowY, w: boxW, h: boxH,
      fill: { color: BG_CARD },
      shadow: makeShadow()
    });
    // Cyan top border
    s2.addShape(pres.shapes.RECTANGLE, {
      x: curX, y: flowY, w: boxW, h: 0.04, fill: { color: CYAN }
    });
    // Icon
    s2.addImage({ data: st.icon, x: curX + boxW/2 - 0.17, y: flowY + 0.15, w: 0.34, h: 0.34 });
    // Label
    s2.addText(st.label, {
      x: curX, y: flowY + 0.5, w: boxW, h: 0.5,
      fontSize: 11, fontFace: "Calibri", color: WHITE, bold: true, align: "center", valign: "middle", margin: 0
    });
    // Sub
    s2.addText(st.sub, {
      x: curX, y: flowY + 0.95, w: boxW, h: 0.3,
      fontSize: 9, fontFace: "Calibri", color: GRAY, align: "center", margin: 0
    });

    // Arrow between boxes
    if (i < stages.length - 1) {
      s2.addImage({ data: iconArrow, x: curX + boxW + 0.02, y: flowY + boxH/2 - 0.1, w: 0.2, h: 0.2 });
    }
    curX += boxW + gap;
  }

  // Bottom row — supporting systems
  const bottomY = 3.2;
  const bottomBoxes = [
    { label: "SQLite Store", icon: iconDB, sub: "trades, anomalies, positions" },
    { label: "Portfolio Intelligence", icon: iconShield, sub: "category limits, event caps, burst throttle" },
    { label: "Self-Learning", icon: iconBrain, sub: "feature attribution, adaptive weights" },
    { label: "Pushover Alerts", icon: iconBell, sub: "real-time notifications" },
  ];

  curX = 0.5;
  const bw = 2.1;
  for (let i = 0; i < bottomBoxes.length; i++) {
    const bb = bottomBoxes[i];
    s2.addShape(pres.shapes.RECTANGLE, {
      x: curX, y: bottomY, w: bw, h: 1.1,
      fill: { color: BG_CARD },
      shadow: makeShadow()
    });
    s2.addImage({ data: bb.icon, x: curX + 0.15, y: bottomY + 0.15, w: 0.28, h: 0.28 });
    s2.addText(bb.label, {
      x: curX + 0.5, y: bottomY + 0.12, w: bw - 0.6, h: 0.35,
      fontSize: 11, fontFace: "Calibri", color: WHITE, bold: true, valign: "middle", margin: 0
    });
    s2.addText(bb.sub, {
      x: curX + 0.5, y: bottomY + 0.5, w: bw - 0.6, h: 0.45,
      fontSize: 9, fontFace: "Calibri", color: GRAY, margin: 0
    });
    curX += bw + 0.2;
  }

  // Connector lines
  s2.addShape(pres.shapes.LINE, {
    x: 5, y: flowY + boxH, w: 0, h: bottomY - flowY - boxH,
    line: { color: CYAN, width: 1, dashType: "dash" }
  });

  s2.addText("24/7 on Oracle Cloud Infrastructure", {
    x: 0.8, y: 4.7, w: 8, h: 0.4,
    fontSize: 11, fontFace: "Calibri", color: GRAY, italic: true, margin: 0
  });

  // ═══════════════════════════════════════════════════════════════
  // SLIDE 3: 10-Feature Detection Engine
  // ═══════════════════════════════════════════════════════════════
  let s3 = pres.addSlide();
  s3.background = { color: BG_DARK };
  s3.addShape(pres.shapes.RECTANGLE, { x: 0, y: 0, w: 10, h: 0.06, fill: { color: CYAN } });

  s3.addText("10-Feature Detection Engine", {
    x: 0.8, y: 0.3, w: 8.4, h: 0.7,
    fontSize: 32, fontFace: "Georgia", color: WHITE, bold: true, margin: 0
  });

  // Two columns: Original 6 + New 4
  const features = [
    // Original
    { name: "Trade Size Z-Score", wt: "12%", fire: "94%", cat: "original" },
    { name: "Volume Spike Ratio", wt: "10%", fire: "99%", cat: "original" },
    { name: "Order Book Imbalance", wt: "12%", fire: "10%", cat: "original" },
    { name: "Taker Side Skew", wt: "20%", fire: "70%", cat: "original" },
    { name: "Price Impact", wt: "5%", fire: "2%", cat: "original" },
    { name: "Cross-Market Correlation", wt: "3%", fire: "0.4%", cat: "original" },
    // New
    { name: "Sweep Detection", wt: "15%", fire: "16%", cat: "new" },
    { name: "Trade Velocity", wt: "12%", fire: "28%", cat: "new" },
    { name: "Size Concentration", wt: "5%", fire: "7%", cat: "new" },
    { name: "Book Pressure Delta", wt: "6%", fire: "new", cat: "new" },
  ];

  // Column headers
  s3.addText("ORIGINAL FEATURES", {
    x: 0.5, y: 1.1, w: 4.3, h: 0.3,
    fontSize: 10, fontFace: "Calibri", color: GRAY, charSpacing: 3, margin: 0
  });
  s3.addText("ADVANCED FEATURES (NEW)", {
    x: 5.2, y: 1.1, w: 4.3, h: 0.3,
    fontSize: 10, fontFace: "Calibri", color: CYAN, charSpacing: 3, margin: 0
  });

  let leftY = 1.5;
  let rightY = 1.5;
  const rowH = 0.58;

  for (const f of features) {
    const isNew = f.cat === "new";
    const x = isNew ? 5.2 : 0.5;
    const y = isNew ? rightY : leftY;
    const cardW = 4.3;

    s3.addShape(pres.shapes.RECTANGLE, {
      x: x, y: y, w: cardW, h: rowH - 0.08,
      fill: { color: BG_CARD },
    });
    // Left accent
    s3.addShape(pres.shapes.RECTANGLE, {
      x: x, y: y, w: 0.05, h: rowH - 0.08,
      fill: { color: isNew ? CYAN : VIOLET },
    });

    s3.addText(f.name, {
      x: x + 0.15, y: y, w: 2.5, h: rowH - 0.08,
      fontSize: 11, fontFace: "Calibri", color: WHITE, bold: true, valign: "middle", margin: 0
    });

    s3.addText(`wt: ${f.wt}`, {
      x: x + 2.7, y: y, w: 0.7, h: rowH - 0.08,
      fontSize: 9, fontFace: "Calibri", color: CYAN, valign: "middle", align: "right", margin: 0
    });

    s3.addText(`fire: ${f.fire}`, {
      x: x + 3.5, y: y, w: 0.7, h: rowH - 0.08,
      fontSize: 9, fontFace: "Calibri", color: GRAY, valign: "middle", align: "right", margin: 0
    });

    if (isNew) rightY += rowH;
    else leftY += rowH;
  }

  // Key insight callout
  s3.addShape(pres.shapes.RECTANGLE, {
    x: 0.5, y: 4.8, w: 9, h: 0.5,
    fill: { color: CYAN, transparency: 90 },
  });
  s3.addText("Weights shifted from always-firing baseline features toward selective, high-conviction signals", {
    x: 0.8, y: 4.8, w: 8.5, h: 0.5,
    fontSize: 11, fontFace: "Calibri", color: CYAN, italic: true, valign: "middle", margin: 0
  });

  // ═══════════════════════════════════════════════════════════════
  // SLIDE 4: Conviction System
  // ═══════════════════════════════════════════════════════════════
  let s4 = pres.addSlide();
  s4.background = { color: BG_DARK };
  s4.addShape(pres.shapes.RECTANGLE, { x: 0, y: 0, w: 10, h: 0.06, fill: { color: CYAN } });

  s4.addText("Conviction System", {
    x: 0.8, y: 0.3, w: 8.4, h: 0.7,
    fontSize: 32, fontFace: "Georgia", color: WHITE, bold: true, margin: 0
  });

  s4.addText("Event-aware trade management with time-decayed signal aggregation", {
    x: 0.8, y: 0.95, w: 8, h: 0.4,
    fontSize: 14, fontFace: "Calibri", color: GRAY, margin: 0
  });

  // Formula
  s4.addShape(pres.shapes.RECTANGLE, {
    x: 1.5, y: 1.6, w: 7, h: 0.7,
    fill: { color: BG_CARD },
    shadow: makeShadow()
  });
  s4.addText("conviction = \u03A3 ( score\u1D62 \u00D7 e^(-age\u1D62 / half_life) )", {
    x: 1.5, y: 1.6, w: 7, h: 0.7,
    fontSize: 20, fontFace: "Consolas", color: CYAN, align: "center", valign: "middle", margin: 0
  });

  // Three decision cards
  const decisions = [
    { title: "ALLOW", desc: "No opposing signals\nin this event", color: GREEN },
    { title: "BLOCK", desc: "Opposing side has\nstronger conviction", color: AMBER },
    { title: "FLIP", desc: "New side exceeds old\nby flip threshold", color: RED },
  ];

  let dx = 0.8;
  for (const d of decisions) {
    s4.addShape(pres.shapes.RECTANGLE, {
      x: dx, y: 2.7, w: 2.7, h: 1.3,
      fill: { color: BG_CARD },
      shadow: makeShadow()
    });
    s4.addShape(pres.shapes.RECTANGLE, {
      x: dx, y: 2.7, w: 2.7, h: 0.05, fill: { color: d.color }
    });
    s4.addText(d.title, {
      x: dx, y: 2.85, w: 2.7, h: 0.4,
      fontSize: 18, fontFace: "Calibri", color: d.color, bold: true, align: "center", margin: 0
    });
    s4.addText(d.desc, {
      x: dx + 0.2, y: 3.3, w: 2.3, h: 0.6,
      fontSize: 11, fontFace: "Calibri", color: GRAY, align: "center", margin: 0
    });
    dx += 3.0;
  }

  // Key properties
  s4.addText([
    { text: "Persistence vs Intensity: ", options: { bold: true, color: WHITE, breakLine: false } },
    { text: "Three 0.65 alerts \u2248 one 0.90 alert (at same recency)", options: { color: GRAY, breakLine: true } },
    { text: "Recency wins: ", options: { bold: true, color: WHITE, breakLine: false } },
    { text: "Fresh intensity beats stale persistence as signals decay", options: { color: GRAY, breakLine: true } },
    { text: "Survives restarts: ", options: { bold: true, color: WHITE, breakLine: false } },
    { text: "Signals persisted to SQLite, restored on startup", options: { color: GRAY } },
  ], {
    x: 0.8, y: 4.3, w: 8.4, h: 1.0,
    fontSize: 12, fontFace: "Calibri", margin: 0, paraSpaceAfter: 4
  });

  // ═══════════════════════════════════════════════════════════════
  // SLIDE 5: Progress & Roadmap
  // ═══════════════════════════════════════════════════════════════
  let s5 = pres.addSlide();
  s5.background = { color: BG_DARK };
  s5.addShape(pres.shapes.RECTANGLE, { x: 0, y: 0, w: 10, h: 0.06, fill: { color: CYAN } });

  s5.addText("Progress & Roadmap", {
    x: 0.8, y: 0.3, w: 8.4, h: 0.7,
    fontSize: 32, fontFace: "Georgia", color: WHITE, bold: true, margin: 0
  });

  // Progress bars
  const progress = [
    { label: "Signal Detection", pct: 75, sub: "10 features, tuned weights" },
    { label: "Execution Engine", pct: 60, sub: "Book-aware pricing, adaptive tiers" },
    { label: "Risk Management", pct: 70, sub: "Kill switch, category/event/burst limits" },
    { label: "Conviction System", pct: 80, sub: "Event grouping, decay, persistence" },
    { label: "Self-Learning", pct: 30, sub: "Built, awaiting settlement data" },
    { label: "Proven P&L", pct: 0, sub: "Critical gap \u2014 fills just started" },
  ];

  let py = 1.2;
  for (const p of progress) {
    // Label
    s5.addText(p.label, {
      x: 0.8, y: py, w: 2.5, h: 0.3,
      fontSize: 12, fontFace: "Calibri", color: WHITE, bold: true, valign: "middle", margin: 0
    });
    // Pct
    s5.addText(`${p.pct}%`, {
      x: 3.3, y: py, w: 0.6, h: 0.3,
      fontSize: 12, fontFace: "Consolas", color: p.pct >= 70 ? GREEN : p.pct >= 40 ? AMBER : RED,
      valign: "middle", align: "right", margin: 0
    });
    // Bar background
    s5.addShape(pres.shapes.RECTANGLE, {
      x: 4.1, y: py + 0.06, w: 5.1, h: 0.18,
      fill: { color: BG_CARD },
    });
    // Bar fill
    if (p.pct > 0) {
      s5.addShape(pres.shapes.RECTANGLE, {
        x: 4.1, y: py + 0.06, w: 5.1 * p.pct / 100, h: 0.18,
        fill: { color: p.pct >= 70 ? GREEN : p.pct >= 40 ? AMBER : RED },
      });
    }
    // Sub text
    s5.addText(p.sub, {
      x: 4.1, y: py + 0.26, w: 5, h: 0.25,
      fontSize: 9, fontFace: "Calibri", color: GRAY, margin: 0
    });
    py += 0.62;
  }

  // Roadmap section
  s5.addText("NEXT STEPS", {
    x: 0.8, y: 4.15, w: 8, h: 0.3,
    fontSize: 10, fontFace: "Calibri", color: CYAN, charSpacing: 3, margin: 0
  });

  const roadmap = [
    { time: "Days", desc: "Verify fills, first settlement data" },
    { time: "1-2 weeks", desc: "Feature attribution, win rate by pattern" },
    { time: "2-4 weeks", desc: "Adaptive weights, market-specific models" },
    { time: "Long-term", desc: "Kelly sizing, external data, ML models" },
  ];

  let rx = 0.8;
  for (const r of roadmap) {
    s5.addShape(pres.shapes.RECTANGLE, {
      x: rx, y: 4.5, w: 2.1, h: 0.75,
      fill: { color: BG_CARD },
    });
    s5.addText(r.time, {
      x: rx, y: 4.5, w: 2.1, h: 0.35,
      fontSize: 11, fontFace: "Calibri", color: CYAN, bold: true, align: "center", valign: "middle", margin: 0
    });
    s5.addText(r.desc, {
      x: rx + 0.1, y: 4.85, w: 1.9, h: 0.35,
      fontSize: 9, fontFace: "Calibri", color: GRAY, align: "center", margin: 0
    });
    rx += 2.25;
  }

  // Save
  const outPath = "/Users/perryjenkins/Documents/trdng/kalshi-diamond/DIAMOND_v2_Deck.pptx";
  await pres.writeFile({ fileName: outPath });
  console.log("Saved to:", outPath);
}

buildDeck().catch(console.error);
