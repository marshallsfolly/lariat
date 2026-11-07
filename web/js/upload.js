// Images from the phone for the agents. Pick (camera or library) or paste one,
// shrink it to at most 2048 px (re-encoding also drops its metadata, GPS
// included), upload it; the server answers with the path to give the agent.

import { api, at } from './api.js';

const MAX_EDGE = 2048;

/** The picker; call it straight from the tap. Resolves to the chosen files ([] if cancelled). */
export function pickImages() {
  return new Promise((resolve) => {
    const input = document.createElement('input');
    input.type = 'file';
    input.accept = 'image/*';
    input.multiple = true;
    input.hidden = true;
    const done = (files) => {
      input.remove();
      resolve(files);
    };
    input.addEventListener('change', () => done([...input.files]), { once: true });
    input.addEventListener('cancel', () => done([]), { once: true });
    document.body.append(input);
    input.click();
  });
}

/** What the clipboard holds: images (as blobs) and/or text. One permission prompt on iOS. */
export async function readClipboard() {
  if (navigator.clipboard?.read) {
    const images = [];
    let text = null;
    for (const item of await navigator.clipboard.read()) {
      const image = item.types.find((t) => t.startsWith('image/'));
      if (image) images.push(await item.getType(image));
      else if (item.types.includes('text/plain') && text === null) text = await (await item.getType('text/plain')).text();
    }
    return { images, text };
  }
  return { images: [], text: await navigator.clipboard.readText() };
}

async function shrink(file) {
  let bitmap;
  try {
    bitmap = await createImageBitmap(file, { imageOrientation: 'from-image' });
  } catch {
    return file; // this browser cannot decode it (HEIC, say): send it as it is
  }
  const scale = Math.min(1, MAX_EDGE / Math.max(bitmap.width, bitmap.height));
  const canvas = document.createElement('canvas');
  canvas.width = Math.round(bitmap.width * scale);
  canvas.height = Math.round(bitmap.height * scale);
  canvas.getContext('2d').drawImage(bitmap, 0, 0, canvas.width, canvas.height);
  bitmap.close?.();
  const lossless = file.type === 'image/png' || file.type === 'image/gif'; // screenshots stay sharp
  const blob = await new Promise((resolve) => canvas.toBlob(resolve, lossless ? 'image/png' : 'image/jpeg', 0.86));
  return blob || file;
}

/** Shrink and upload each image to machine `m` (null: this one); resolves to their paths there. */
export async function uploadImages(files, m = null) {
  const paths = [];
  for (const file of files) {
    const res = await api(at(m, '/api/upload'), { method: 'POST', body: await shrink(file) });
    paths.push(res.path);
  }
  return paths;
}

/** Paths as the agent should see them typed: quoted only when they need it. */
export const asArgs = (paths) => paths.map((p) => (/\s/.test(p) ? `"${p}"` : p)).join(' ');
