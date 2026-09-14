// A frame is a big-endian uint32 length followed by PNG bytes; zero is a heartbeat.
export async function* readFrames(reader) {
  let chunk = new Uint8Array(), offset = 0;
  async function exact(length) {
    const result = new Uint8Array(length);
    let used = 0;
    while (used < length) {
      if (offset === chunk.length) {
        const next = await reader.read();
        if (next.done) throw new Error('화면 연결이 종료됐습니다.');
        chunk = next.value; offset = 0;
      }
      const count = Math.min(length - used, chunk.length - offset);
      result.set(chunk.subarray(offset, offset + count), used);
      offset += count; used += count;
    }
    return result;
  }
  while (true) {
    const length = new DataView((await exact(4)).buffer).getUint32(0);
    if (length > 16777216) throw new Error('화면 데이터가 너무 큽니다.');
    if (length) yield await exact(length);
  }
}
