const pptxgen = require("pptxgenjs");

let pres = new pptxgen();
pres.layout = "LAYOUT_16x9";
pres.author = "DIAMOND";
pres.title = "DIAMOND — Status Report";

// Color palette — dark terminal aesthetic matching the dashboard
const BG = "0A0E17";
const PANEL = "141B2D";
const CYAN = "00E5FF";
const VIOLET = "8B5CF6";
const GREEN = "10B981";
const RED = "EF4444";
const AMBER = "F59E0B";
const TEXT = "E2E8F0";
const TEXT_DIM = "64748B";
const WHITE = "FFFFFF";

// ── Slide 1: Title ──────────────────────────────────────────────────────────
let s1 = pres.addSlide();
s1.background = { color: BG };

// Subtle top accent line (cyan → violet feel)
s1.addShape(pres.shapes.RECTANGLE, { x: 0, y: 0, w: 10, h: 0.04, fill: { color: CYAN } });

// Diamond icon placeholder — geometric shape
s1.addShape(pres.shapes.RECTANGLE, {
  x: 0.7, y: 1.2, w: 0.6, h: 0.6,
  fill: { color: CYAN, transparency: 20 },
  line: { color: CYAN, width: 1.5 },
  rotate: 45,
});

s1.addText("DIAMOND", {
  x: 1.6, y: 1.0, w: 7, h: 0.7,
  fontSize: 44, fontFace: "Calibri", bold: true, color: WHITE,
  charSpacing: 8, margin: 0,
});

s1.addText("Kalshi Prediction Market Anomaly Detection & Live Trading", {
  x: 1.6, y: 1.7, w: 7.5, h: 0.5,
  fontSize: 14, fontFace: "Calibri Light", color: TEXT_DIM, margin: 0,
});

// Divider line
s1.addShape(pres.shapes.LINE, {
  x: 1.6, y: 2.4, w: 3, h: 0,
  line: { color: CYAN, width: 1, dashType: "dash" },
});

s1.addText("Status Report — March 2026", {
  x: 1.6, y: 2.7, w: 5, h: 0.4,
  fontSize: 12, fontFace: "Calibri", color: TEXT_DIM, margin: 0,
});

// ── Slide 2: Architecture ────────────────────────────────────────────────────
let s2 = pres.addSlide();
s2.background = { color: BG };
s2.addShape(pres.shapes.RECTANGLE, { x: 0, y: 0, w: 10, h: 0.04, fill: { color: CYAN } });

s2.addText("System Architecture", {
  x: 0.5, y: 0.3, w: 9, h: 0.6,
  fontSize: 28, fontFace: "Calibri", bold: true, color: WHITE, margin: 0,
});

// Flow diagram using boxes and arrows
const boxH = 0.55;
const boxStyle = (color) => ({
  fill: { color: PANEL },
  line: { color: color, width: 1.5 },
});
const labelStyle = (size = 10) => ({
  fontSize: size, fontFace: "Calibri", color: TEXT, align: "center", valign: "middle", margin: 0,
});

// Row 1: Data ingestion
s2.addShape(pres.shapes.RECTANGLE, { x: 0.3, y: 1.2, w: 2.0, h: boxH, ...boxStyle(CYAN) });
s2.addText("Kalshi WebSocket", { x: 0.3, y: 1.2, w: 2.0, h: boxH, ...labelStyle() });

s2.addText(">>>", { x: 2.35, y: 1.2, w: 0.5, h: boxH, fontSize: 12, color: TEXT_DIM, align: "center", valign: "middle" });

s2.addShape(pres.shapes.RECTANGLE, { x: 2.8, y: 1.2, w: 2.0, h: boxH, ...boxStyle(CYAN) });
s2.addText("Stream Processor", { x: 2.8, y: 1.2, w: 2.0, h: boxH, ...labelStyle() });

s2.addText(">>>", { x: 4.85, y: 1.2, w: 0.5, h: boxH, fontSize: 12, color: TEXT_DIM, align: "center", valign: "middle" });

s2.addShape(pres.shapes.RECTANGLE, { x: 5.3, y: 1.2, w: 2.2, h: boxH, ...boxStyle(VIOLET) });
s2.addText("10-Feature Engine", { x: 5.3, y: 1.2, w: 2.2, h: boxH, ...labelStyle() });

s2.addText(">>>", { x: 7.55, y: 1.2, w: 0.5, h: boxH, fontSize: 12, color: TEXT_DIM, align: "center", valign: "middle" });

s2.addShape(pres.shapes.RECTANGLE, { x: 8.0, y: 1.2, w: 1.7, h: boxH, ...boxStyle(GREEN) });
s2.addText("Alert Engine", { x: 8.0, y: 1.2, w: 1.7, h: boxH, ...labelStyle() });

// Row 2: Intelligence layer
s2.addShape(pres.shapes.RECTANGLE, { x: 1.0, y: 2.2, w: 2.2, h: boxH, ...boxStyle(VIOLET) });
s2.addText("Conviction System", { x: 1.0, y: 2.2, w: 2.2, h: boxH, ...labelStyle() });

s2.addShape(pres.shapes.RECTANGLE, { x: 3.8, y: 2.2, w: 2.4, h: boxH, ...boxStyle(VIOLET) });
s2.addText("Portfolio Intelligence", { x: 3.8, y: 2.2, w: 2.4, h: boxH, ...labelStyle() });

s2.addShape(pres.shapes.RECTANGLE, { x: 6.8, y: 2.2, w: 2.4, h: boxH, ...boxStyle(AMBER) });
s2.addText("Self-Learning Pipeline", { x: 6.8, y: 2.2, w: 2.4, h: boxH, ...labelStyle() });

// Row 3: Execution
s2.addShape(pres.shapes.RECTANGLE, { x: 2.5, y: 3.2, w: 2.5, h: boxH, ...boxStyle(GREEN) });
s2.addText("Live Trading Engine", { x: 2.5, y: 3.2, w: 2.5, h: boxH, ...labelStyle() });

s2.addShape(pres.shapes.RECTANGLE, { x: 5.5, y: 3.2, w: 2.0, h: boxH, ...boxStyle(GREEN) });
s2.addText("Kalshi REST API", { x: 5.5, y: 3.2, w: 2.0, h: boxH, ...labelStyle() });

// Arrows between rows
s2.addText("v", { x: 6.2, y: 1.78, w: 0.5, h: 0.4, fontSize: 14, color: TEXT_DIM, align: "center", valign: "middle" });
s2.addText("v", { x: 4.5, y: 2.78, w: 0.5, h: 0.4, fontSize: 14, color: TEXT_DIM, align: "center", valign: "middle" });
s2.addText(">>>", { x: 4.95, y: 3.2, w: 0.5, h: boxH, fontSize: 12, color: TEXT_DIM, align: "center", valign: "middle" });

// Feature list
s2.addText("10 Detection Features", {
  x: 0.5, y: 4.0, w: 3.5, h: 0.4,
  fontSize: 11, fontFace: "Calibri", bold: true, color: CYAN, margin: 0,
});
s2.addText([
  { text: "Volume Spike  |  Trade Z-Score  |  Taker Skew", options: { breakLine: true, fontSize: 9, color: TEXT_DIM } },
  { text: "Trade Velocity  |  Sweep Detection  |  Book Imbalance", options: { breakLine: true, fontSize: 9, color: TEXT_DIM } },
  { text: "Size Concentration  |  Price Impact  |  Book Delta  |  X-Mkt Corr", options: { fontSize: 9, color: TEXT_DIM } },
], { x: 0.5, y: 4.35, w: 5.5, h: 0.8, fontFace: "Calibri", margin: 0 });

// Key tech
s2.addText("Tech Stack", {
  x: 6.5, y: 4.0, w: 3, h: 0.4,
  fontSize: 11, fontFace: "Calibri", bold: true, color: CYAN, margin: 0,
});
s2.addText([
  { text: "Python 3.13  |  asyncio  |  WebSocket", options: { breakLine: true, fontSize: 9, color: TEXT_DIM } },
  { text: "SQLite  |  aiohttp  |  Dash + Plotly", options: { breakLine: true, fontSize: 9, color: TEXT_DIM } },
  { text: "OCI Compute  |  24/7 Live Trading", options: { fontSize: 9, color: TEXT_DIM } },
], { x: 6.5, y: 4.35, w: 3.5, h: 0.8, fontFace: "Calibri", margin: 0 });


// ── Slide 3: Live Performance ────────────────────────────────────────────────
let s3 = pres.addSlide();
s3.background = { color: BG };
s3.addShape(pres.shapes.RECTANGLE, { x: 0, y: 0, w: 10, h: 0.04, fill: { color: CYAN } });

s3.addText("Live Performance", {
  x: 0.5, y: 0.3, w: 9, h: 0.6,
  fontSize: 28, fontFace: "Calibri", bold: true, color: WHITE, margin: 0,
});

s3.addText("160 settled trades  |  March 20-24, 2026  |  Real money on Kalshi", {
  x: 0.5, y: 0.85, w: 9, h: 0.3,
  fontSize: 10, fontFace: "Calibri", color: TEXT_DIM, margin: 0,
});

// Stat cards — row 1
const cardW = 2.8;
const cardH = 1.1;
const cardY = 1.4;
const makeShadow = () => ({ type: "outer", blur: 4, offset: 1, angle: 135, color: "000000", opacity: 0.3 });

// Card 1: Total P&L
s3.addShape(pres.shapes.RECTANGLE, { x: 0.5, y: cardY, w: cardW, h: cardH, fill: { color: PANEL }, shadow: makeShadow() });
s3.addText("TOTAL P&L", { x: 0.5, y: cardY + 0.1, w: cardW, h: 0.3, fontSize: 9, fontFace: "Calibri", color: TEXT_DIM, align: "center", margin: 0 });
s3.addText("+$0.92", { x: 0.5, y: cardY + 0.35, w: cardW, h: 0.6, fontSize: 36, fontFace: "Calibri", bold: true, color: GREEN, align: "center", margin: 0 });

// Card 2: Win Rate
s3.addShape(pres.shapes.RECTANGLE, { x: 3.6, y: cardY, w: cardW, h: cardH, fill: { color: PANEL }, shadow: makeShadow() });
s3.addText("WIN RATE", { x: 3.6, y: cardY + 0.1, w: cardW, h: 0.3, fontSize: 9, fontFace: "Calibri", color: TEXT_DIM, align: "center", margin: 0 });
s3.addText("48%", { x: 3.6, y: cardY + 0.35, w: cardW, h: 0.6, fontSize: 36, fontFace: "Calibri", bold: true, color: AMBER, align: "center", margin: 0 });

// Card 3: Avg Win vs Loss
s3.addShape(pres.shapes.RECTANGLE, { x: 6.7, y: cardY, w: cardW, h: cardH, fill: { color: PANEL }, shadow: makeShadow() });
s3.addText("AVG WIN / AVG LOSS", { x: 6.7, y: cardY + 0.1, w: cardW, h: 0.3, fontSize: 9, fontFace: "Calibri", color: TEXT_DIM, align: "center", margin: 0 });
s3.addText("+41¢ / -37¢", { x: 6.7, y: cardY + 0.35, w: cardW, h: 0.6, fontSize: 28, fontFace: "Calibri", bold: true, color: TEXT, align: "center", margin: 0 });

// Category performance table
s3.addText("P&L by Market Category", {
  x: 0.5, y: 2.85, w: 9, h: 0.4,
  fontSize: 13, fontFace: "Calibri", bold: true, color: CYAN, margin: 0,
});

const catData = [
  [
    { text: "Category", options: { bold: true, color: TEXT_DIM, fill: { color: "1A2235" } } },
    { text: "Trades", options: { bold: true, color: TEXT_DIM, fill: { color: "1A2235" } } },
    { text: "P&L", options: { bold: true, color: TEXT_DIM, fill: { color: "1A2235" } } },
    { text: "Win Rate", options: { bold: true, color: TEXT_DIM, fill: { color: "1A2235" } } },
  ],
  [{ text: "NHL", options: { color: GREEN } }, "6", { text: "+$1.67", options: { color: GREEN } }, { text: "67%", options: { color: GREEN } }],
  [{ text: "ATP Tennis", options: { color: GREEN } }, "14", { text: "+$1.27", options: { color: GREEN } }, { text: "57%", options: { color: GREEN } }],
  [{ text: "LIV Golf", options: { color: GREEN } }, "3", { text: "+$1.19", options: { color: GREEN } }, { text: "100%", options: { color: GREEN } }],
  [{ text: "La Liga", options: { color: GREEN } }, "8", { text: "+$1.00", options: { color: GREEN } }, { text: "62%", options: { color: GREEN } }],
  [{ text: "MLB", options: { color: GREEN } }, "2", { text: "+$1.00", options: { color: GREEN } }, { text: "100%", options: { color: GREEN } }],
  [{ text: "NCAA Men BB", options: { color: RED } }, "84", { text: "-$1.16", options: { color: RED } }, { text: "45%", options: { color: RED } }],
  [{ text: "NBA", options: { color: RED } }, "11", { text: "-$1.94", options: { color: RED } }, { text: "45%", options: { color: RED } }],
  [{ text: "NCAA Women BB", options: { color: RED } }, "6", { text: "-$1.76", options: { color: RED } }, { text: "17%", options: { color: RED } }],
];

s3.addTable(catData, {
  x: 0.5, y: 3.2, w: 9, h: 2.2,
  fontSize: 10, fontFace: "Calibri",
  color: TEXT,
  border: { pt: 0.5, color: "1E293B" },
  fill: { color: PANEL },
  colW: [2.5, 1.5, 2.5, 2.5],
  rowH: [0.28, 0.25, 0.25, 0.25, 0.25, 0.25, 0.25, 0.25, 0.25],
  align: "left",
  valign: "middle",
});


// ── Slide 4: Key Insight — Score Distribution ────────────────────────────────
let s4 = pres.addSlide();
s4.background = { color: BG };
s4.addShape(pres.shapes.RECTANGLE, { x: 0, y: 0, w: 10, h: 0.04, fill: { color: CYAN } });

s4.addText("Key Insight: Score vs Profitability", {
  x: 0.5, y: 0.3, w: 9, h: 0.6,
  fontSize: 28, fontFace: "Calibri", bold: true, color: WHITE, margin: 0,
});

s4.addText("Lower-conviction alerts outperform mid-range signals", {
  x: 0.5, y: 0.85, w: 9, h: 0.3,
  fontSize: 11, fontFace: "Calibri", color: TEXT_DIM, margin: 0,
});

// Score bucket chart
s4.addChart(pres.charts.BAR, [{
  name: "P&L (cents)",
  labels: ["0.55-0.59", "0.60-0.64", "0.65-0.69", "0.70-0.79", "0.80+"],
  values: [220, -51, -92, -99, 114],
}], {
  x: 0.5, y: 1.4, w: 5.5, h: 3.5,
  barDir: "col",
  chartColors: [GREEN],
  chartArea: { fill: { color: PANEL }, roundedCorners: true },
  catAxisLabelColor: TEXT_DIM,
  catAxisLabelFontSize: 9,
  valAxisLabelColor: TEXT_DIM,
  valAxisLabelFontSize: 8,
  valGridLine: { color: "1E293B", size: 0.5 },
  catGridLine: { style: "none" },
  showValue: true,
  dataLabelPosition: "outEnd",
  dataLabelColor: TEXT,
  dataLabelFontSize: 10,
  showLegend: false,
  showTitle: false,
  valAxisTitle: "P&L (cents)",
  valAxisTitleColor: TEXT_DIM,
  valAxisTitleFontSize: 8,
});

// Insight callouts
s4.addShape(pres.shapes.RECTANGLE, { x: 6.5, y: 1.5, w: 3.2, h: 1.0, fill: { color: PANEL }, shadow: makeShadow() });
s4.addText([
  { text: "0.55-0.59 bucket", options: { bold: true, color: GREEN, breakLine: true, fontSize: 13 } },
  { text: "91 trades, +$2.20 P&L, 49% win rate", options: { color: TEXT_DIM, breakLine: true, fontSize: 10 } },
  { text: "Best risk-adjusted returns", options: { color: TEXT, fontSize: 10 } },
], { x: 6.7, y: 1.6, w: 2.8, h: 0.9, fontFace: "Calibri", margin: 0 });

s4.addShape(pres.shapes.RECTANGLE, { x: 6.5, y: 2.8, w: 3.2, h: 1.0, fill: { color: PANEL }, shadow: makeShadow() });
s4.addText([
  { text: "0.80+ bucket", options: { bold: true, color: CYAN, breakLine: true, fontSize: 13 } },
  { text: "3 trades, +$1.14 P&L, 100% win rate", options: { color: TEXT_DIM, breakLine: true, fontSize: 10 } },
  { text: "Perfect but rare signals", options: { color: TEXT, fontSize: 10 } },
], { x: 6.7, y: 2.9, w: 2.8, h: 0.9, fontFace: "Calibri", margin: 0 });

s4.addShape(pres.shapes.RECTANGLE, { x: 6.5, y: 4.1, w: 3.2, h: 1.0, fill: { color: PANEL }, shadow: makeShadow() });
s4.addText([
  { text: "Mid-range (0.60-0.79)", options: { bold: true, color: RED, breakLine: true, fontSize: 13 } },
  { text: "66 trades, -$2.42 P&L, 40% win rate", options: { color: TEXT_DIM, breakLine: true, fontSize: 10 } },
  { text: "Possible noise on hyped games", options: { color: TEXT, fontSize: 10 } },
], { x: 6.7, y: 4.2, w: 2.8, h: 0.9, fontFace: "Calibri", margin: 0 });


// ── Slide 5: What's Next ────────────────────────────────────────────────────
let s5 = pres.addSlide();
s5.background = { color: BG };
s5.addShape(pres.shapes.RECTANGLE, { x: 0, y: 0, w: 10, h: 0.04, fill: { color: CYAN } });

s5.addText("Roadmap", {
  x: 0.5, y: 0.3, w: 9, h: 0.6,
  fontSize: 28, fontFace: "Calibri", bold: true, color: WHITE, margin: 0,
});

// Phase boxes
const phaseY = 1.2;
const phaseW = 4.3;
const phaseH = 1.8;

// Phase 1: Now
s5.addShape(pres.shapes.RECTANGLE, { x: 0.5, y: phaseY, w: phaseW, h: phaseH, fill: { color: PANEL }, shadow: makeShadow() });
s5.addShape(pres.shapes.RECTANGLE, { x: 0.5, y: phaseY, w: 0.06, h: phaseH, fill: { color: GREEN } });
s5.addText("NOW", { x: 0.75, y: phaseY + 0.1, w: 1.5, h: 0.3, fontSize: 11, fontFace: "Calibri", bold: true, color: GREEN, margin: 0 });
s5.addText([
  { text: "Collect settlement data (fill bug fixed)", options: { bullet: true, breakLine: true, fontSize: 10, color: TEXT } },
  { text: "Run feature attribution on 160 trades", options: { bullet: true, breakLine: true, fontSize: 10, color: TEXT } },
  { text: "Monitor category-level P&L", options: { bullet: true, breakLine: true, fontSize: 10, color: TEXT } },
  { text: "Skipped trades dashboard live", options: { bullet: true, fontSize: 10, color: TEXT } },
], { x: 0.75, y: phaseY + 0.4, w: 3.8, h: 1.3, fontFace: "Calibri", margin: 0 });

// Phase 2: 2-3 weeks
s5.addShape(pres.shapes.RECTANGLE, { x: 5.2, y: phaseY, w: phaseW, h: phaseH, fill: { color: PANEL }, shadow: makeShadow() });
s5.addShape(pres.shapes.RECTANGLE, { x: 5.2, y: phaseY, w: 0.06, h: phaseH, fill: { color: CYAN } });
s5.addText("2-3 WEEKS", { x: 5.45, y: phaseY + 0.1, w: 2, h: 0.3, fontSize: 11, fontFace: "Calibri", bold: true, color: CYAN, margin: 0 });
s5.addText([
  { text: "Adaptive category weighting (+/-10%)", options: { bullet: true, breakLine: true, fontSize: 10, color: TEXT } },
  { text: "Grid search: half_life, flip_threshold", options: { bullet: true, breakLine: true, fontSize: 10, color: TEXT } },
  { text: "Feature co-occurrence win rates", options: { bullet: true, breakLine: true, fontSize: 10, color: TEXT } },
  { text: "Weekly automated optimization cron", options: { bullet: true, fontSize: 10, color: TEXT } },
], { x: 5.45, y: phaseY + 0.4, w: 3.8, h: 1.3, fontFace: "Calibri", margin: 0 });

// Phase 3: Long-term
const phase3Y = 3.3;
s5.addShape(pres.shapes.RECTANGLE, { x: 0.5, y: phase3Y, w: phaseW, h: phaseH, fill: { color: PANEL }, shadow: makeShadow() });
s5.addShape(pres.shapes.RECTANGLE, { x: 0.5, y: phase3Y, w: 0.06, h: phaseH, fill: { color: VIOLET } });
s5.addText("1-2 MONTHS", { x: 0.75, y: phase3Y + 0.1, w: 2, h: 0.3, fontSize: 11, fontFace: "Calibri", bold: true, color: VIOLET, margin: 0 });
s5.addText([
  { text: "Score-scaled position sizing (Kelly)", options: { bullet: true, breakLine: true, fontSize: 10, color: TEXT } },
  { text: "Market-type-specific models", options: { bullet: true, breakLine: true, fontSize: 10, color: TEXT } },
  { text: "External data (DraftKings odds)", options: { bullet: true, breakLine: true, fontSize: 10, color: TEXT } },
  { text: "ML model trained on settled outcomes", options: { bullet: true, fontSize: 10, color: TEXT } },
], { x: 0.75, y: phase3Y + 0.4, w: 3.8, h: 1.3, fontFace: "Calibri", margin: 0 });

// Progress bar
s5.addShape(pres.shapes.RECTANGLE, { x: 5.2, y: phase3Y, w: phaseW, h: phaseH, fill: { color: PANEL }, shadow: makeShadow() });
s5.addShape(pres.shapes.RECTANGLE, { x: 5.2, y: phase3Y, w: 0.06, h: phaseH, fill: { color: AMBER } });
s5.addText("PROGRESS", { x: 5.45, y: phase3Y + 0.1, w: 2, h: 0.3, fontSize: 11, fontFace: "Calibri", bold: true, color: AMBER, margin: 0 });

// Progress bars
const bars = [
  ["Signal Detection", 75, GREEN],
  ["Execution", 60, CYAN],
  ["Risk Management", 70, CYAN],
  ["Conviction System", 80, GREEN],
  ["Self-Learning", 30, AMBER],
  ["Proven P&L", 5, RED],
];

bars.forEach((b, i) => {
  const barY = phase3Y + 0.45 + i * 0.22;
  s5.addText(b[0], { x: 5.45, y: barY, w: 1.8, h: 0.2, fontSize: 8, fontFace: "Calibri", color: TEXT_DIM, margin: 0 });
  // Background bar
  s5.addShape(pres.shapes.RECTANGLE, { x: 7.3, y: barY + 0.03, w: 1.8, h: 0.14, fill: { color: "1E293B" } });
  // Fill bar
  s5.addShape(pres.shapes.RECTANGLE, { x: 7.3, y: barY + 0.03, w: 1.8 * (b[1] / 100), h: 0.14, fill: { color: b[2] } });
  s5.addText(`${b[1]}%`, { x: 9.15, y: barY, w: 0.5, h: 0.2, fontSize: 7, fontFace: "Calibri", color: TEXT_DIM, margin: 0 });
});


pres.writeFile({ fileName: "/Users/perryjenkins/Documents/trdng/kalshi-diamond/DIAMOND_Status.pptx" })
  .then(() => console.log("DIAMOND_Status.pptx created"))
  .catch(err => console.error(err));
