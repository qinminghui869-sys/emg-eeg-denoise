/**
 * Read L3 observation parquet in the browser (hyparquet, Snappy included).
 * Folder import looks for the wrist EMG files and, when present, EEG in the same directory.
 */
import { parquetRead } from "./vendor/hyparquet.js";

const META_COLUMNS = new Set([
  "timestamp",
  "time",
  "t",
  "episode_index",
  "episode",
  "index",
  "frame_index",
]);

const EEG_MARKERS = ["fp1", "fp2", "fz", "cz", "pz", "o1", "o2", "c3", "c4"];

const OBSERVATION_ROLES = {
  "observation.left_wrist_emg.parquet": "left",
  "observation.right_wrist_emg.parquet": "right",
  "observation.eeg.parquet": "eeg",
};

export async function readParquetTable(buffer) {
  const order = [];
  const parts = new Map();
  await parquetRead({
    file: buffer,
    onChunk(chunk) {
      const name = chunk.columnName;
      if (!parts.has(name)) {
        order.push(name);
        parts.set(name, []);
      }
      parts.get(name).push(chunk.columnData);
    },
  });
  if (!order.length) throw new Error("parquet 中没有列");
  return {
    columns: order.map((name) => ({ name, values: concatColumn(parts.get(name)) })),
  };
}

export function streamFromTable(table, filename, roleHint = null) {
  const names = table.columns.map((col) => col.name);
  const role = roleHint || classifyStream(filename, names);
  const byName = new Map(table.columns.map((col) => [col.name, col.values]));
  const timeName = names.find((name) => META_COLUMNS.has(name.toLowerCase()) && /time|^t$/i.test(name));
  const timeCol = timeName ? byName.get(timeName) : null;
  if (!timeCol || timeCol.length < 2) {
    throw new Error(`${filename} 需要 timestamp 列`);
  }
  const time = relativeTime(timeCol);
  const signalCols = table.columns.filter((col) => {
    if (META_COLUMNS.has(col.name.toLowerCase())) return false;
    return isNumericColumn(col.values);
  });
  if (!signalCols.length) throw new Error(`${filename} 没有数值通道`);
  const n = time.length;
  const channels = signalCols.map((col) => toF32(col.values, n));
  const prefix = role === "right" ? "R" : role === "left" ? "L" : "";
  const channelNames = signalCols.map((col, i) => displayName(col.name, prefix, i));
  const fs = sampleRateHz(time);
  return {
    role,
    block: {
      kind: role === "eeg" ? "eeg" : "emg",
      names: channelNames,
      channels,
      mean: channels.map(mean),
      std: channels.map(std),
      time,
      fs_hz: fs,
      duration_s: time[n - 1] - time[0],
      filename,
    },
  };
}

export function datasetFromSides(source, sides) {
  const blocks = Object.values(sides);
  if (!blocks.length) throw new Error("没有可显示的信号");
  const duration = Math.max(...blocks.map((block) => block.duration_s));
  return {
    source,
    source_path: "",
    duration_s: duration,
    unit: "",
    sides,
  };
}

/** Group a directory listing by parent path. Only the three observation names are kept. */
export function pickEpisodeGroups(fileList) {
  const groups = new Map();
  for (const file of fileList) {
    const rel = file.webkitRelativePath || file.name;
    const base = rel.split("/").pop();
    const role = OBSERVATION_ROLES[base.toLowerCase()];
    if (!role) continue;
    const dir = rel.slice(0, Math.max(0, rel.length - base.length)).replace(/\/$/, "");
    if (!groups.has(dir)) groups.set(dir, { dir, files: {} });
    const group = groups.get(dir);
    if (!group.files[role]) group.files[role] = file;
  }
  return [...groups.values()].sort((a, b) => scoreGroup(b) - scoreGroup(a) || a.dir.localeCompare(b.dir));
}

export function classifyStream(filename, columnNames) {
  const base = String(filename).split(/[/\\]/).pop().toLowerCase();
  if (base.includes("left_wrist_emg")) return "left";
  if (base.includes("right_wrist_emg")) return "right";
  if (base.includes(".eeg.") || base.endsWith("eeg.parquet") || base.includes("observation.eeg")) {
    return "eeg";
  }
  const lower = columnNames.map((name) => name.toLowerCase());
  const hits = EEG_MARKERS.filter((name) => lower.includes(name)).length;
  if (hits >= 3) return "eeg";
  if (lower.some((name) => /^dim_\d+$/.test(name))) {
    return base.includes("right") ? "right" : "left";
  }
  return "eeg";
}

function scoreGroup(group) {
  return (group.files.left ? 4 : 0) + (group.files.right ? 4 : 0) + (group.files.eeg ? 1 : 0);
}

function displayName(column, prefix, index) {
  const dim = /^dim_(\d+)$/.exec(column);
  if (dim && prefix) return `${prefix}${dim[1]}`;
  return column || `${prefix}${index}`;
}

function concatColumn(parts) {
  if (parts.length === 1) return parts[0];
  const n = parts.reduce((sum, part) => sum + part.length, 0);
  const out = new Array(n);
  let offset = 0;
  for (const part of parts) {
    for (let i = 0; i < part.length; i++) out[offset++] = part[i];
  }
  return out;
}

function isNumericColumn(values) {
  for (let i = 0; i < values.length; i++) {
    const value = values[i];
    if (value == null) continue;
    return typeof value === "number" || typeof value === "bigint";
  }
  return false;
}

function relativeTime(values) {
  const out = new Float64Array(values.length);
  let t0 = NaN;
  for (let i = 0; i < values.length; i++) {
    const value = typeof values[i] === "bigint" ? Number(values[i]) : Number(values[i]);
    out[i] = value;
    if (!Number.isFinite(t0) && Number.isFinite(value)) t0 = value;
  }
  if (!Number.isFinite(t0)) throw new Error("timestamp 无效");
  for (let i = 0; i < out.length; i++) out[i] -= t0;
  return out;
}

function sampleRateHz(time) {
  const n = time.length;
  if (n < 2) return 0;
  // Adjacent intervals, not a multi-sample average: gaps would otherwise pull fs down.
  const stride = Math.max(1, Math.floor((n - 1) / 8000));
  const diffs = [];
  for (let i = stride; i < n; i += stride) {
    const dt = time[i] - time[i - 1];
    if (dt > 0) diffs.push(dt);
  }
  if (!diffs.length) return 0;
  diffs.sort((a, b) => a - b);
  const mid = diffs[Math.floor(diffs.length / 2)];
  return mid > 0 ? 1 / mid : 0;
}

function toF32(values, n) {
  const out = new Float32Array(n);
  const len = Math.min(n, values.length);
  for (let i = 0; i < len; i++) {
    const value = values[i];
    out[i] = typeof value === "bigint" ? Number(value) : Number(value);
  }
  return out;
}

function mean(arr) {
  let sum = 0;
  for (let i = 0; i < arr.length; i++) sum += arr[i];
  return arr.length ? sum / arr.length : 0;
}

function std(arr) {
  const m = mean(arr);
  let sum = 0;
  for (let i = 0; i < arr.length; i++) {
    const d = arr[i] - m;
    sum += d * d;
  }
  return Math.sqrt(sum / (arr.length || 1));
}
