const memo = new WeakMap();
export const MPH_PER_MPS = 2.2369363;

function pixels(frame) {
  if (!memo.has(frame)) {
    const bytes = atob(frame.pixels);
    memo.set(frame, Uint8Array.from(bytes, byte => byte.charCodeAt(0)));
  }
  return memo.get(frame);
}

function interpolate(frame, [lon, lat], channel) {
  const position = ((lon % 360) + 360) % 360 * frame.width / 360;
  const row = (90 - Math.min(90, Math.max(-90, lat))) * (frame.height - 1) / 180;
  const x = Math.floor(position), y = Math.floor(row);
  const east = (x + 1) % frame.width, south = Math.min(y + 1, frame.height - 1);
  const data = pixels(frame);
  const cell = (col, row) => channel(data, row * frame.width + col);
  const blend = (a, b, f) => a + (b - a) * f;
  return blend(blend(cell(x, y), cell(east, y), position - x),
    blend(cell(x, south), cell(east, south), position - x), row - y);
}

export function radarCell(lon, lat) {
  const x = Math.floor((lon + 130) * 4), y = Math.floor((55 - lat) * 4);
  return x >= 0 && x < 280 && y >= 0 && y < 140 ? y * 280 + x : null;
}

export function pointValue(frame, point) {
  if (!frame) return null;
  if (frame.format === 'u16le-0.01mm-mrms') {
    const cell = radarCell(...point);
    if (cell == null) return null;
    const data = pixels(frame);
    const packed = data[cell * 2] + 256 * data[cell * 2 + 1];
    return packed === 65535 ? null : packed / 100;
  }
  if (frame.format === 'u16le-0.01m-ocean') {
    const [lon, lat] = point;
    const col = Math.round(((lon % 360) + 360) % 360 * frame.width / 360) % frame.width;
    const row = Math.round((90 - Math.min(90, Math.max(-90, lat))) * (frame.height - 1) / 180);
    const data = pixels(frame);
    const offset = (row * frame.width + col) * 2;
    const packed = data[offset] + 256 * data[offset + 1];
    return packed === 65535 ? null : packed / 100;
  }
  if (frame.format === 'u8-percent') return interpolate(frame, point, (data, index) => data[index]);
  if (frame.format === 'u16le-0.01mm') return interpolate(frame, point, (data, index) =>
    (data[index * 2] + 256 * data[index * 2 + 1]) / 100);
  return interpolate(frame, point, (data, index) => (data[index * 2] + 256 * data[index * 2 + 1]) / 10 - 256);
}

export function pointWind(frame, point) {
  if (!frame) return null;
  return [0, 2].map(channel => interpolate(frame, point, (data, index) => {
    const offset = index * 4 + channel;
    const packed = data[offset] + 256 * data[offset + 1];
    return (packed < 32768 ? packed : packed - 65536) / 10;
  }));
}
