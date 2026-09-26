/**
 * Audio normalisation for the Google Meet bridge: Web Audio float samples (already resampled to
 * 16 kHz mono by the AudioContext) -> 16-bit little-endian PCM in 20 ms frames, the format the
 * backend media WebSocket announces as {"encoding":"linear16","sample_rate":16000}.
 * Nothing here stores audio; frames exist only until they are sent.
 */

export const SAMPLE_RATE = 16000;
export const FRAME_MS = 20;
export const SAMPLES_PER_FRAME = (SAMPLE_RATE * FRAME_MS) / 1000; // 320 samples = 640 bytes

/** Float [-1, 1] -> signed 16-bit (clipped). */
export function floatToPcm16(input: Float32Array): Int16Array {
  const out = new Int16Array(input.length);
  for (let i = 0; i < input.length; i++) {
    const s = Math.max(-1, Math.min(1, input[i] ?? 0));
    out[i] = s < 0 ? Math.round(s * 0x8000) : Math.round(s * 0x7fff);
  }
  return out;
}

/** Collects PCM samples and emits exact 20 ms frames (as little-endian bytes). */
export class PcmFramer {
  private buffer = new Int16Array(SAMPLES_PER_FRAME);
  private filled = 0;

  constructor(private readonly onFrame: (frame: Uint8Array) => void) {}

  push(samples: Int16Array): void {
    let offset = 0;
    while (offset < samples.length) {
      const take = Math.min(SAMPLES_PER_FRAME - this.filled, samples.length - offset);
      this.buffer.set(samples.subarray(offset, offset + take), this.filled);
      this.filled += take;
      offset += take;
      if (this.filled === SAMPLES_PER_FRAME) {
        this.onFrame(toLittleEndianBytes(this.buffer));
        this.buffer = new Int16Array(SAMPLES_PER_FRAME);
        this.filled = 0;
      }
    }
  }

  /** Drop a partial frame (end of stream). */
  reset(): void {
    this.filled = 0;
  }
}

export function toLittleEndianBytes(samples: Int16Array): Uint8Array {
  const bytes = new Uint8Array(samples.length * 2);
  const view = new DataView(bytes.buffer);
  for (let i = 0; i < samples.length; i++) view.setInt16(i * 2, samples[i] ?? 0, true);
  return bytes;
}

export function toBase64(bytes: Uint8Array): string {
  let binary = "";
  for (let i = 0; i < bytes.length; i++) binary += String.fromCharCode(bytes[i] ?? 0);
  return btoa(binary);
}

/** AudioWorklet processor: forwards each 128-sample render quantum (mono mix of its input). */
export const WORKLET_SOURCE = `
class MeetPcmTap extends AudioWorkletProcessor {
  process(inputs) {
    const input = inputs[0];
    if (input && input.length > 0) {
      const channel = input[0];
      if (channel && channel.length) this.port.postMessage(channel.slice(0));
    }
    return true;
  }
}
registerProcessor("meet-pcm-tap", MeetPcmTap);
`;
