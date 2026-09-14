import assert from 'node:assert/strict';
import {readFrames} from './dist/stream.js';
function reader(bytes, size) {
  let offset=0;
  return {async read(){if(offset>=bytes.length)return {done:true};const value=bytes.slice(offset,offset+size);offset+=size;return {value};}};
}
const wire=Uint8Array.from([0,0,0,0,0,0,0,3,1,2,3,0,0,0,2,4,5]);
for(const size of [1,3,4,6,100]){
  const frames=readFrames(reader(wire,size));
  assert.deepEqual([...(await frames.next()).value],[1,2,3]);
  assert.deepEqual([...(await frames.next()).value],[4,5]);
  await assert.rejects(frames.next(),/종료/);
}
await assert.rejects(readFrames(reader(Uint8Array.from([1,0,0,1]),4)).next(),/너무 큽니다/);
await assert.rejects(readFrames(reader(wire.slice(0,9),2)).next(),/종료/);
console.log('stream framing: fragmented, combined, heartbeat, truncated, oversized passed');
