import { geoGraticule10, geoOrthographic, geoPath } from 'd3-geo';
import { feature, mesh } from 'topojson-client';
import worldAtlas from 'world-atlas/countries-110m.json';
import { MPH_PER_MPS, pointWind, radarCell } from './probe.js';

const land = feature(worldAtlas, worldAtlas.objects.land);
const boundaries = mesh(worldAtlas, worldAtlas.objects.countries, (a, b) => a !== b);
const graticule = geoGraticule10();
const radians = Math.PI / 180;

function destination(lon, lat, bearing) {
  const angularStep = radians * 1.3;
  const phi = lat * radians;
  const nextLat = Math.asin(Math.sin(phi) * Math.cos(angularStep)
    + Math.cos(phi) * Math.sin(angularStep) * Math.cos(bearing));
  const offset = Math.atan2(Math.sin(bearing) * Math.sin(angularStep) * Math.cos(phi),
    Math.cos(angularStep) - Math.sin(phi) * Math.sin(nextLat));
  return [lon + offset / radians, nextLat / radians];
}

function flowArrows(ctx, frame, projection, facing, view) {
  const phi = view * radians;
  ctx.save();
  ctx.lineWidth = 1;
  ctx.lineCap = 'round';
  ctx.strokeStyle = '#f6ffeece';
  ctx.shadowBlur = 2;
  ctx.shadowColor = '#122b35';
  ctx.beginPath();
  for (let lat = -70; lat <= 70; lat += 14) {
    const latitude = lat * radians;
    for (let lon = -180; lon < 180; lon += 15) {
      const front = Math.sin(phi) * Math.sin(latitude)
        + Math.cos(phi) * Math.cos(latitude) * Math.cos((lon - facing) * radians);
      if (front < .33) continue;
      const [u, v] = pointWind(frame, [lon, lat]);
      const speed = Math.hypot(u, v) * MPH_PER_MPS;
      if (speed < 1) continue;
      const [x, y] = projection([lon, lat]);
      const [ex, ey] = projection(destination(lon, lat, Math.atan2(u, v)));
      const length = Math.hypot(ex - x, ey - y);
      if (!length) continue;
      const dx = (ex - x) / length, dy = (ey - y) / length;
      const reach = Math.min(15, 4 + speed * .33);
      const tx = x + dx * reach, ty = y + dy * reach;
      ctx.moveTo(x, y); ctx.lineTo(tx, ty);
      ctx.lineTo(tx - dx * 3 + dy * 2, ty - dy * 3 - dx * 2);
      ctx.moveTo(tx, ty); ctx.lineTo(tx - dx * 3 - dy * 2, ty - dy * 3 + dx * 2);
    }
  }
  ctx.stroke();
  ctx.restore();
}

const vertex = `
attribute vec2 position;
void main() { gl_Position = vec4(position, 0.0, 1.0); }
`;
const fragment = `
precision highp float;
uniform sampler2D grid;
uniform sampler2D clouds;
uniform sampler2D precip;
uniform sampler2D waves;
uniform sampler2D radar;
uniform vec2 centre;
uniform float radius;
uniform vec2 facing;
uniform bool showWeather;
uniform bool showClouds;
uniform bool showPrecipitation;
uniform bool showWaves;
uniform bool showRadar;
const float pi = 3.141592653589793;
float packedValue(sampler2D field, vec2 cell) {
  vec4 sampleValue = texture2D(field, (cell + 0.5) / vec2(360.0, 181.0));
  return (sampleValue.r + 256.0 * sampleValue.g) * 255.0;
}
float sampleGrid(sampler2D field, float lon, float lat) {
  float x = mod(lon * 180.0 / pi + 360.0, 360.0);
  float y = clamp(90.0 - lat * 180.0 / pi, 0.0, 180.0);
  vec2 a = floor(vec2(x, y));
  vec2 f = vec2(x, y) - a;
  float nextX = mod(a.x + 1.0, 360.0);
  float nextY = min(a.y + 1.0, 180.0);
  return mix(mix(packedValue(field, a), packedValue(field, vec2(nextX, a.y)), f.x),
    mix(packedValue(field, vec2(a.x, nextY)), packedValue(field, vec2(nextX, nextY)), f.x), f.y);
}
float cloudFraction(float lon, float lat) {
  float x = mod(lon * 180.0 / pi + 360.5, 360.0);
  float y = clamp(90.0 - lat * 180.0 / pi, 0.0, 180.0) + 0.5;
  return texture2D(clouds, vec2(x, y) / vec2(360.0, 181.0)).r * 2.55;
}
vec3 palette(float temp) {
  vec3 a = vec3(0.34, 0.31, 0.65);
  vec3 b = vec3(0.20, 0.47, 0.79);
  vec3 c = vec3(0.19, 0.68, 0.72);
  vec3 d = vec3(0.79, 0.80, 0.45);
  vec3 e = vec3(0.94, 0.54, 0.29);
  vec3 f = vec3(0.79, 0.26, 0.30);
  if (temp < 0.0) return mix(a, b, smoothstep(-40.0, 0.0, temp));
  if (temp < 40.0) return mix(b, c, smoothstep(0.0, 40.0, temp));
  if (temp < 64.0) return mix(c, d, smoothstep(40.0, 64.0, temp));
  if (temp < 88.0) return mix(d, e, smoothstep(64.0, 88.0, temp));
  return mix(e, f, smoothstep(88.0, 120.0, temp));
}
void main() {
  vec2 xy = (gl_FragCoord.xy - centre) / radius;
  float d = dot(xy, xy);
  if (d > 1.0) {
    float halo = exp(-max(0.0, sqrt(d)-1.0)*30.0) * 0.25;
    gl_FragColor = vec4(0.33, 0.68, 0.80, halo);
    return;
  }
  float z = sqrt(1.0 - d);
  float lat = asin(clamp(xy.y*cos(facing.y)+z*sin(facing.y), -1.0, 1.0));
  float lon = facing.x + atan(xy.x, z*cos(facing.y)-xy.y*sin(facing.y));
  vec3 base = vec3(0.19, 0.32, 0.38);
  if (showWeather) {
    float temp = sampleGrid(grid, lon, lat) * 0.1 - 256.0;
    base = palette(temp);
    float contour = abs(fract(temp / 12.0) - 0.5);
    base *= 1.0 - 0.10 * smoothstep(0.45, 0.5, contour) * z;
  }
  if (showClouds) base = mix(base, vec3(0.90, 0.97, 0.98), 0.69*cloudFraction(lon, lat));
  if (showWaves) {
    float x = mod(floor(lon * 180.0 / pi + 360.5), 360.0);
    float y = clamp(floor(90.5 - lat * 180.0 / pi), 0.0, 180.0);
    float height = packedValue(waves, vec2(x, y));
    if (height < 65534.5) {
      float scale = clamp(height / 800.0, 0.0, 1.0);
      base = mix(base, mix(vec3(0.20, 0.52, 0.61), vec3(0.82, 0.52, 0.78), scale), 0.94);
    }
  }
  if (showPrecipitation) {
    float mm = sampleGrid(precip, lon, lat) * 0.01;
    float scale = min(1.0, log(1.0 + mm) / log(26.0));
    vec3 water = mix(vec3(0.53, 0.91, 0.97), vec3(0.25, 0.40, 0.94), scale);
    base = mix(base, water, 0.92 * smoothstep(0.01, 1.0, mm));
  }
  if (showRadar) {
    float lonDeg = mod(lon * 180.0 / pi + 180.0, 360.0) - 180.0;
    vec2 cell = floor(4.0 * vec2(lonDeg + 130.0, 55.0 - lat * 180.0 / pi));
    if (cell.x >= 0.0 && cell.x < 280.0 && cell.y >= 0.0 && cell.y < 140.0) {
      vec4 radarValue = texture2D(radar, (cell + 0.5) / vec2(280.0, 140.0));
      float radarCode = (radarValue.r + 256.0 * radarValue.g) * 255.0;
      if (radarCode < 65534.5) {
        float mm = radarCode * 0.01;
        float scale = min(1.0, log(1.0 + mm) / log(26.0));
        vec3 estimates = mix(vec3(0.39, 0.84, 0.72), vec3(0.98, 0.55, 0.29), scale);
        base = mix(base, estimates, 0.96 * smoothstep(0.02, 1.0, mm));
      }
    }
  }
  float light = 0.56 + 0.31*z + 0.16*dot(xy, vec2(-0.5, 0.75));
  vec3 rim = vec3(0.31, 0.55, 0.65) * (1.0 - smoothstep(0.0, 0.24, z)) * 0.35;
  gl_FragColor = vec4(base * light + rim, 1.0);
}
`;

function compile(gl, type, source) {
  const shader = gl.createShader(type);
  gl.shaderSource(shader, source);
  gl.compileShader(shader);
  if (!gl.getShaderParameter(shader, gl.COMPILE_STATUS)) {
    throw new Error(gl.getShaderInfoLog(shader));
  }
  return shader;
}

function makeThermal(canvas) {
  const gl = canvas.getContext('webgl', { antialias: false, premultipliedAlpha: false });
  if (!gl) return null;
  const program = gl.createProgram();
  gl.attachShader(program, compile(gl, gl.VERTEX_SHADER, vertex));
  gl.attachShader(program, compile(gl, gl.FRAGMENT_SHADER, fragment));
  gl.linkProgram(program);
  if (!gl.getProgramParameter(program, gl.LINK_STATUS)) throw new Error(gl.getProgramInfoLog(program));
  gl.useProgram(program);
  const buffer = gl.createBuffer();
  gl.bindBuffer(gl.ARRAY_BUFFER, buffer);
  gl.bufferData(gl.ARRAY_BUFFER, new Float32Array([-1, -1, 1, -1, -1, 1, 1, 1]), gl.STATIC_DRAW);
  const position = gl.getAttribLocation(program, 'position');
  gl.enableVertexAttribArray(position);
  gl.vertexAttribPointer(position, 2, gl.FLOAT, false, 0, 0);
  const uniforms = Object.fromEntries(
    ['grid', 'clouds', 'precip', 'waves', 'radar', 'centre', 'radius', 'facing', 'showWeather', 'showClouds', 'showPrecipitation', 'showWaves', 'showRadar']
      .map(name => [name, gl.getUniformLocation(program, name)]),
  );
  function texture(unit, slot, filter) {
    gl.activeTexture(gl.TEXTURE0 + unit);
    gl.bindTexture(gl.TEXTURE_2D, gl.createTexture());
    for (const dimension of [gl.TEXTURE_WRAP_S, gl.TEXTURE_WRAP_T]) gl.texParameteri(gl.TEXTURE_2D, dimension, gl.CLAMP_TO_EDGE);
    for (const key of [gl.TEXTURE_MIN_FILTER, gl.TEXTURE_MAG_FILTER]) gl.texParameteri(gl.TEXTURE_2D, key, filter);
    gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA, 1, 1, 0, gl.RGBA, gl.UNSIGNED_BYTE, new Uint8Array(4));
    gl.uniform1i(slot, unit);
  }
  texture(0, uniforms.grid, gl.NEAREST);
  texture(1, uniforms.clouds, gl.LINEAR);
  texture(2, uniforms.precip, gl.NEAREST);
  texture(3, uniforms.waves, gl.NEAREST);
  texture(4, uniforms.radar, gl.NEAREST);
  return {
    upload(frame, unit) {
      const isCloud = unit === 1;
      const bytes = atob(frame.pixels);
      const pixels = new Uint8Array(frame.width * frame.height * 4);
      for (let i = 0; i < frame.width * frame.height; i++) {
        pixels[i * 4] = bytes.charCodeAt(isCloud ? i : i * 2);
        if (!isCloud) pixels[i * 4 + 1] = bytes.charCodeAt(i * 2 + 1);
        pixels[i * 4 + 3] = 255;
      }
      gl.activeTexture(gl.TEXTURE0 + unit);
      gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA, frame.width, frame.height, 0, gl.RGBA, gl.UNSIGNED_BYTE, pixels);
    },
    draw(centre, radius, lon, lat, withWeather, withClouds, withPrecipitation, withWaves, withRadar) {
      gl.viewport(0, 0, canvas.width, canvas.height);
      gl.uniform2f(uniforms.centre, centre[0], canvas.height - centre[1]);
      gl.uniform1f(uniforms.radius, radius);
      gl.uniform2f(uniforms.facing, lon * radians, lat * radians);
      gl.uniform1i(uniforms.showWeather, Number(withWeather));
      gl.uniform1i(uniforms.showClouds, Number(withClouds));
      gl.uniform1i(uniforms.showPrecipitation, Number(withPrecipitation));
      gl.uniform1i(uniforms.showWaves, Number(withWaves));
      gl.uniform1i(uniforms.showRadar, Number(withRadar));
      gl.drawArrays(gl.TRIANGLE_STRIP, 0, 4);
    },
  };
}

function fallbackSphere(ctx, centre, radius, projection, frame, cloudFrame, precipFrame, waveFrame, radarFrame) {
  ctx.save();
  ctx.beginPath();
  ctx.arc(...centre, radius, 0, Math.PI * 2);
  ctx.fillStyle = '#294553';
  ctx.fill();
  ctx.clip();
  if (frame || cloudFrame || precipFrame || waveFrame || radarFrame) {
    const bytes = frame ? atob(frame.pixels) : null;
    const cloudBytes = cloudFrame ? atob(cloudFrame.pixels) : null;
    const precipBytes = precipFrame ? atob(precipFrame.pixels) : null;
    const waveBytes = waveFrame ? atob(waveFrame.pixels) : null;
    const radarBytes = radarFrame ? atob(radarFrame.pixels) : null;
    const square = ctx.createImageData(Math.ceil(radius * 2), Math.ceil(radius * 2));
    const colors = [[-40, 76, 70, 145], [0, 45, 105, 179], [40, 44, 155, 165],
      [64, 177, 180, 99], [88, 207, 117, 62], [120, 174, 58, 68]];
    const sea = [48, 81, 96];
    const lightBlue = [135, 232, 247], deepBlue = [64, 102, 240];
    const lowWave = [51, 133, 156], highWave = [209, 133, 199];
    const lowRadar = [99, 213, 184], highRadar = [250, 140, 74];
    for (let y = 0; y < square.height; y++) {
      for (let x = 0; x < square.width; x++) {
        const dx = x - radius, dy = y - radius;
        if (dx * dx + dy * dy >= radius * radius) continue;
        const [lon, lat] = projection.invert([centre[0] + dx, centre[1] + dy]);
        const row = Math.max(0, Math.min(180, Math.round(90 - lat)));
        const col = Math.round(lon + 360) % 360;
        const index = (row * 360 + col) * 2;
        const value = bytes ? (bytes.charCodeAt(index) + 256 * bytes.charCodeAt(index + 1)) / 10 - 256 : 32;
        const upper = Math.max(1, colors.findIndex(stop => stop[0] >= Math.min(value, 120)));
        const first = colors[upper - 1], second = colors[upper];
        const f = Math.max(0, Math.min(1, (value - first[0]) / (second[0] - first[0])));
        const shade = 0.68 + 0.32 * Math.sqrt(1 - (dx * dx + dy * dy) / (radius * radius));
        const pixel = (y * square.width + x) * 4;
        const cover = cloudBytes ? cloudBytes.charCodeAt(index / 2) * .0069 : 0;
        const mm = precipBytes ? (precipBytes.charCodeAt(index) + 256 * precipBytes.charCodeAt(index + 1)) * .01 : 0;
        const mix = Math.min(1, Math.log1p(mm) / Math.log(26));
        const amount = Math.min(.92, Math.max(0, mm - .01));
        const wave = waveBytes ? waveBytes.charCodeAt(index) + 256 * waveBytes.charCodeAt(index + 1) : 65535;
        const waveFraction = Math.min(1, wave / 800);
        const radarIndex = radarBytes ? radarCell(lon, lat) : null;
        const radarPacked = radarIndex == null ? 65535 : radarBytes.charCodeAt(radarIndex * 2) + 256 * radarBytes.charCodeAt(radarIndex * 2 + 1);
        const observed = radarPacked === 65535 ? 0 : radarPacked * .01;
        const radarMix = Math.min(1, Math.log1p(observed) / Math.log(26));
        const radarStrength = Math.min(.96, Math.max(0, observed - .02));
        for (let channel = 0; channel < 3; channel++) {
          const base = bytes ? first[channel + 1] + f * (second[channel + 1] - first[channel + 1]) : sea[channel];
          const cloudy = base * (1 - cover) + 245 * cover;
          const wet = wave === 65535 ? cloudy : .06 * cloudy + .94 * (lowWave[channel] + waveFraction * (highWave[channel] - lowWave[channel]));
          const color = lightBlue[channel] + mix * (deepBlue[channel] - lightBlue[channel]);
          const water = wet * (1 - amount) + color * amount;
          const radar = lowRadar[channel] + radarMix * (highRadar[channel] - lowRadar[channel]);
          square.data[pixel + channel] = (water * (1 - radarStrength) + radar * radarStrength) * shade;
        }
        square.data[pixel + 3] = 255;
      }
    }
    const temp = document.createElement('canvas');
    temp.width = square.width;
    temp.height = square.height;
    temp.getContext('2d').putImageData(square, 0, 0);
    ctx.drawImage(temp, centre[0] - radius, centre[1] - radius);
  }
  ctx.restore();
}

export function makeGlobe({ onPin, onProbe }) {
  const host = document.querySelector('#world');
  const canvas = document.querySelector('#coast');
  const thermalCanvas = document.querySelector('#thermal');
  const ctx = canvas.getContext('2d');
  const pins = document.querySelector('#pins');
  const readout = document.querySelector('#geo-readout');
  const message = document.querySelector('#globe-message');
  const probeMarker = document.querySelector('#probe-marker');
  let thermal;
  try { thermal = makeThermal(thermalCanvas); } catch (error) { console.warn('WebGL globe fallback', error); }
  let lon = -73, lat = 29, zoom = 1;
  let frame = null, uploaded = null, weather = true, markets = true, events = [], selected = '';
  let focusedStation = null;
  let cloudFrame = null, uploadedCloud = null, clouds = false;
  let windFrame = null, wind = false;
  let precipFrame = null, uploadedPrecip = null, precipitation = false;
  let waveFrame = null, uploadedWave = null, waves = false;
  let radarFrame = null, uploadedRadar = null, radar = false;
  let marineReports = [];
  let drag = null, pending = 0, probe = null, globeShape = null;

  function draw() {
    pending = 0;
    const { width, height } = host.getBoundingClientRect();
    if (!width || !height) return;
    const ratio = Math.min(2, window.devicePixelRatio || 1);
    const w = Math.round(width * ratio), h = Math.round(height * ratio);
    for (const el of [canvas, thermalCanvas]) {
      if (el.width !== w || el.height !== h) { el.width = w; el.height = h; }
    }
    const centre = [width * .5, height * .5];
    const radius = Math.min(width * .425, height * .447) * zoom;
    const projection = geoOrthographic().translate(centre).scale(radius).rotate([-lon, -lat, 0]);
    globeShape = { centre, radius, projection };
    const showWeather = weather && Boolean(frame);
    const showClouds = clouds && Boolean(cloudFrame);
    const showPrecipitation = precipitation && Boolean(precipFrame);
    const showWaves = waves && Boolean(waveFrame);
    const showRadar = radar && Boolean(radarFrame);
    ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
    ctx.clearRect(0, 0, width, height);
    if (thermal) {
      if (showWeather && uploaded !== frame) {
        thermal.upload(frame, 0);
        uploaded = frame;
      }
      if (showClouds && uploadedCloud !== cloudFrame) {
        thermal.upload(cloudFrame, 1);
        uploadedCloud = cloudFrame;
      }
      if (showPrecipitation && uploadedPrecip !== precipFrame) {
        thermal.upload(precipFrame, 2);
        uploadedPrecip = precipFrame;
      }
      if (showWaves && uploadedWave !== waveFrame) {
        thermal.upload(waveFrame, 3);
        uploadedWave = waveFrame;
      }
      if (showRadar && uploadedRadar !== radarFrame) {
        thermal.upload(radarFrame, 4);
        uploadedRadar = radarFrame;
      }
      thermal.draw(centre.map(n => n * ratio), radius * ratio, lon, lat, showWeather, showClouds, showPrecipitation, showWaves, showRadar);
    } else {
      fallbackSphere(ctx, centre, radius, projection, showWeather ? frame : null, showClouds ? cloudFrame : null,
        showPrecipitation ? precipFrame : null, showWaves ? waveFrame : null, showRadar ? radarFrame : null);
    }
    const path = geoPath(projection, ctx);
    ctx.beginPath(); path(graticule);
    ctx.strokeStyle = 'rgba(221,245,235,.17)'; ctx.lineWidth = .55; ctx.stroke();
    ctx.beginPath(); path(land);
    ctx.fillStyle = showWeather || showPrecipitation ? 'rgba(9,29,33,.15)' : 'rgba(128,172,158,.25)'; ctx.fill();
    ctx.strokeStyle = 'rgba(217,243,227,.61)'; ctx.lineWidth = .72; ctx.stroke();
    ctx.beginPath(); path(boundaries);
    ctx.strokeStyle = 'rgba(214,233,224,.38)'; ctx.lineWidth = .53; ctx.stroke();
    ctx.beginPath(); ctx.arc(...centre, radius, 0, Math.PI * 2);
    ctx.strokeStyle = 'rgba(155,217,221,.49)'; ctx.lineWidth = .8; ctx.stroke();
    if (wind && windFrame) flowArrows(ctx, windFrame, projection, lon, lat);
    if (marineReports.length) {
      const facing = lat * radians;
      ctx.beginPath();
      for (const station of marineReports) {
        const latitude = station.lat * radians;
        const distance = (station.lon - lon) * radians;
        if (Math.sin(facing) * Math.sin(latitude) + Math.cos(facing) * Math.cos(latitude) * Math.cos(distance) < .015) continue;
        const [x, y] = projection([station.lon, station.lat]);
        ctx.moveTo(x + 2, y);
        ctx.arc(x, y, 2, 0, 2 * Math.PI);
      }
      ctx.fillStyle = '#9becf4';
      ctx.strokeStyle = '#163444';
      ctx.lineWidth = .65;
      ctx.fill();
      ctx.stroke();
    }

    const buttons = new Map([...pins.children].map(el => [el.dataset.id, el]));
    const placedLabels = [];
    const visibleEvents = markets ? events.slice().sort((a, b) => Number(b.id === selected) - Number(a.id === selected)
      || (b.edge ?? -Infinity) - (a.edge ?? -Infinity)) : [];
    for (const event of visibleEvents) {
      if (!Number.isFinite(event.lon) || !Number.isFinite(event.lat)) continue;
      const longitude = (event.lon - lon) * radians;
      const latitude = event.lat * radians;
      const view = lat * radians;
      if (Math.sin(view) * Math.sin(latitude) + Math.cos(view) * Math.cos(latitude) * Math.cos(longitude) < .04) continue;
      const point = projection([event.lon, event.lat]);
      if (point[0] < 12 || point[0] > width - 12 || point[1] < 12 || point[1] > height - 12) continue;
      let pin = buttons.get(event.id);
      if (!pin) {
        pin = document.createElement('button');
        pin.type = 'button';
        pin.dataset.id = event.id;
        pin.innerHTML = '<span class="pin-dot"></span><span class="pin-label"></span>';
        pin.addEventListener('click', () => onPin(pin.dataset.id));
        pins.append(pin);
      }
      pin.className = 'pin ' + (event.edge == null ? 'unknown' : event.edge > 0 ? 'positive' : 'negative');
      pin.classList.toggle('active', event.id === selected);
      pin.style.transform = `translate(${point[0]}px,${point[1]}px)`;
      const label = event.edge == null ? event.reason || 'insufficient evidence' : 'best net EV $' + event.edge.toFixed(2);
      const name = event.station_name || event.station;
      pin.setAttribute('aria-label', `${name} (${event.station}), ${label}`);
      pin.setAttribute('aria-pressed', String(event.id === selected));
      pin.title = `${name} · ${label}`;
      const text = event.id === selected ? name : event.station;
      const widthHint = 16 + text.length * 7;
      const left = point[0] + widthHint > width;
      pin.classList.toggle('pin-left', left);
      const origin = left ? point[0] - widthHint : point[0];
      const overlaid = placedLabels.some(other => Math.abs(other.y - point[1]) < 20 &&
        origin < other.x + other.width && origin + widthHint > other.x);
      pin.classList.toggle('pin-dense', overlaid);
      pin.querySelector('.pin-label').textContent = text;
      if (!overlaid) placedLabels.push({ x: origin, y: point[1], width: widthHint });
      buttons.delete(event.id);
    }
    for (const old of buttons.values()) old.remove();
    const probePoint = probe && projection(probe);
    const visible = probe && (Math.sin(lat * radians) * Math.sin(probe[1] * radians)
      + Math.cos(lat * radians) * Math.cos(probe[1] * radians) * Math.cos((probe[0] - lon) * radians)) > 0;
    probeMarker.hidden = !visible;
    if (visible) probeMarker.style.transform = `translate(${probePoint[0]}px,${probePoint[1]}px)`;
    readout.textContent = `${Math.abs(lat).toFixed(0)}°${lat >= 0 ? 'N' : 'S'}  /  ${Math.abs(lon).toFixed(0)}°${lon >= 0 ? 'E' : 'W'}`;
    const missing = [weather && !frame ? 'temperature' : '', clouds && !cloudFrame ? 'cloud cover' : '',
      wind && !windFrame ? 'wind' : '', precipitation && !precipFrame ? 'precipitation' : '',
      waves && !waveFrame ? 'wave height' : '', radar && !radarFrame ? 'radar estimate' : ''].filter(Boolean);
    message.hidden = !missing.length;
    message.textContent = 'No cached ' + missing.join(' or ') + ' grid at this time';
  }

  function redraw() { if (!pending) pending = requestAnimationFrame(draw); }
  function rotate(dx, dy) {
    lon = ((lon + dx + 540) % 360) - 180;
    lat = Math.max(-85, Math.min(85, lat + dy));
    redraw();
  }
  function magnify(amount) { zoom = Math.max(.65, Math.min(2.5, zoom * amount)); redraw(); }
  function setProbe(point) {
    probe = point;
    onProbe(point);
    redraw();
  }
  host.addEventListener('pointerdown', event => {
    if (event.target.closest('button') || event.button !== 0) return;
    host.setPointerCapture(event.pointerId);
    drag = { x: event.clientX, y: event.clientY, travel: 0 };
    host.classList.add('dragging');
  });
  host.addEventListener('pointermove', event => {
    if (!drag) return;
    const dx = drag.x - event.clientX, dy = event.clientY - drag.y;
    drag = { x: event.clientX, y: event.clientY, travel: drag.travel + Math.hypot(dx, dy) };
    if (drag.travel > 4) rotate(dx * .36 / zoom, dy * .36 / zoom);
  });
  host.addEventListener('pointerup', event => {
    if (drag && drag.travel <= 4 && globeShape) {
      const bounds = host.getBoundingClientRect();
      const point = [event.clientX - bounds.left, event.clientY - bounds.top];
      const { centre, radius, projection } = globeShape;
      if (Math.hypot(point[0] - centre[0], point[1] - centre[1]) <= radius) {
        setProbe(projection.invert(point));
      }
    }
  });
  for (const type of ['pointerup', 'pointercancel', 'lostpointercapture']) {
    host.addEventListener(type, () => { drag = null; host.classList.remove('dragging'); });
  }
  host.addEventListener('keydown', event => {
    if (event.key === 'Enter' && event.target === host) { event.preventDefault(); setProbe([lon, lat]); }
    const move = { ArrowLeft: [-9, 0], ArrowRight: [9, 0], ArrowUp: [0, 9], ArrowDown: [0, -9] }[event.key];
    if (move && event.target === host) { event.preventDefault(); rotate(...move); }
  });
  document.querySelector('#zoom-in').addEventListener('click', () => magnify(1.22));
  document.querySelector('#zoom-out').addEventListener('click', () => magnify(1 / 1.22));
  function centerOn(event) {
    lon = event ? event.lon - 1 : -73;
    lat = event ? event.lat - 9 : 29;
    zoom = 1;
    redraw();
  }
  document.querySelector('#center').addEventListener('click', () => {
    centerOn(focusedStation || events.find(item => item.id === selected));
  });
  new ResizeObserver(redraw).observe(host);
  return {
    centerOn,
    layers(showWeather, showMarkets, showClouds, showWind, showPrecipitation, showWaves, showRadar) {
      weather = showWeather;
      markets = showMarkets;
      clouds = showClouds;
      wind = showWind;
      precipitation = showPrecipitation;
      waves = showWaves;
      radar = showRadar;
      redraw();
    },
    stations(value, choice, focus) { events = value; selected = choice; focusedStation = focus; redraw(); },
    temperature(value) { frame = value; redraw(); },
    cloudCover(value) { cloudFrame = value; redraw(); },
    wind(value) { windFrame = value; redraw(); },
    precipitation(value) { precipFrame = value; redraw(); },
    waves(value) { waveFrame = value; redraw(); },
    radar(value) { radarFrame = value; redraw(); },
    marine(value) { marineReports = value; redraw(); },
    inspect(point) { [lon, lat] = point; setProbe(point); },
    clearProbe() { probe = null; redraw(); },
  };
}
